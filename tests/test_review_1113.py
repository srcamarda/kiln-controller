"""
test_review_1113.py — Achado da revisão automática de 13/11/2026:
  M1 — exceção num handler da interface não pode derrubar o loop principal.
  M3 — unidade systemd com hardening, sem PrivateDevices.
  B5 — install.sh recusa usuário inexistente ou root, em vez de assumir "pi".
Rodar:  python3 -m unittest tests.test_review_1113 -v
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from controller import ControllerMode, ErrorKind
from encoder import EncoderEvent
from tests.test_remote_commands import Base, CURVE


class TestErroNaInterfaceM1(Base):

    def boom(self, *a, **k):
        raise RuntimeError("falha de teste")

    def handle(self, event=EncoderEvent.CLICK):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(self.app, "_handle_event", side_effect=self.boom):
            ok = self.app._safe_handle_event(event)
        return ok, out.getvalue()

    def test_excecao_nao_escapa_e_e_registrada(self):
        ok, log = self.handle()
        self.assertFalse(ok)
        self.assertIn("Erro na interface", log)
        self.assertIn("falha de teste", log)
        self.assertIn("Traceback", log)

    def test_mesmo_erro_registrado_uma_vez(self):
        _, log1 = self.handle()
        _, log2 = self.handle()
        self.assertIn("Erro na interface", log1)
        self.assertEqual(log2, "")

    def test_queima_volta_para_tela_de_execucao(self):
        self.app.controller.start_curve(CURVE, 25)
        self.app.ui = self.main.UIState.CANCEL_CONFIRM
        self.handle()
        self.assertEqual(self.app.ui, self.main.UIState.RUNNING)
        self.assertEqual(self.app.controller.state.mode, ControllerMode.RAMPING)

    def test_preaquecimento_volta_para_tela_de_preaquecimento(self):
        self.app.controller.start_preheat(200, 25)
        self.app.ui = self.main.UIState.CANCEL_CONFIRM
        self.handle()
        self.assertEqual(self.app.ui, self.main.UIState.PREHEAT_RUN)

    def test_forno_parado_volta_para_idle(self):
        self.app.ui = self.main.UIState.CURVE_SAVE
        self.handle()
        self.assertEqual(self.app.ui, self.main.UIState.IDLE)

    def test_erro_nao_pula_o_alerta_do_control_tick(self):
        """Em ERRO a tela não é forçada: a transição do _control_tick é que
        desliga os relés e envia o alerta."""
        self.app.controller.start_curve(CURVE, 25)
        self.app.controller.state.mode = ControllerMode.ERROR
        self.app.controller.state.error_kind = ErrorKind.SENSOR
        self.app.ui = self.main.UIState.RUNNING
        self.handle()
        self.assertEqual(self.app.ui, self.main.UIState.RUNNING)

    def test_handler_real_com_falha_no_menu(self):
        """Exceção real dentro de um handler (ação do menu)."""
        self.app.ui = self.main.UIState.MENU
        self.app.menu_sel = 0
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(self.app, "_menu_confirm", side_effect=self.boom):
            ok = self.app._safe_handle_event(EncoderEvent.CLICK)
        self.assertFalse(ok)
        self.assertEqual(self.app.ui, self.main.UIState.IDLE)

    def test_evento_normal_continua_funcionando(self):
        self.app.ui = self.main.UIState.IDLE
        with redirect_stdout(io.StringIO()):
            ok = self.app._safe_handle_event(EncoderEvent.CLICK)
        self.assertTrue(ok)
        self.assertEqual(self.app.ui, self.main.UIState.MENU)


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class TestUnidadeSystemdM3(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(ROOT, "deploy", "kiln-controller.service"),
                  encoding="utf-8") as f:
            self.lines = [l.strip() for l in f
                          if l.strip() and not l.lstrip().startswith("#")]

    def test_hardening_presente(self):
        for d in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=full",
                  "ProtectHome=read-only", "ReadWritePaths=@DIR@"):
            self.assertIn(d, self.lines)

    def test_sem_private_devices(self):
        # Bloquearia GPIO/SPI/I2C e o framebuffer
        self.assertFalse(any(l.startswith("PrivateDevices") for l in self.lines))
        self.assertFalse(any(l.startswith("DevicePolicy") for l in self.lines))


@unittest.skipUnless(os.name == "posix", "install.sh é um script de shell POSIX")
class TestUsuarioInstallB5(unittest.TestCase):
    """Roda o install.sh com um `id` falso que finge ser root. Todos os casos
    saem antes de qualquer alteração no sistema."""

    FAKE_ID = """#!/bin/sh
# id falso: o script roda "como root"; existem só os usuários kiln e root
if [ "$1" = "-u" ] && [ -z "$2" ]; then echo 0; exit 0; fi
if [ "$1" = "-u" ]; then shift; fi
case "$1" in
    kiln) echo 1000; exit 0 ;;
    root) echo 0; exit 0 ;;
    *) echo "id: '$1': no such user" >&2; exit 1 ;;
esac
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        fake = os.path.join(self.tmp, "id")
        with open(fake, "w") as f:
            f.write(self.FAKE_ID)
        os.chmod(fake, 0o755)

    def run_install(self, *args, sudo_user=None):
        env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        env["PATH"] = self.tmp + os.pathsep + env.get("PATH", "")
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        script = os.path.join(ROOT, "deploy", "install.sh")
        return subprocess.run(["sh", script, *args], env=env, capture_output=True,
                              text=True, timeout=10)

    def test_root_direto_sem_usuario_aborta(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOME_DO_USUARIO", r.stderr)

    def test_usuario_inexistente_aborta(self):
        r = self.run_install(sudo_user="pi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("'pi' não existe", r.stderr)

    def test_argumento_inexistente_aborta(self):
        r = self.run_install("fulano", sudo_user="kiln")
        self.assertEqual(r.returncode, 1)
        self.assertIn("'fulano' não existe", r.stderr)

    def test_root_como_usuario_aborta(self):
        r = self.run_install("root")
        self.assertEqual(r.returncode, 1)
        self.assertIn("não deve rodar como root", r.stderr)


if __name__ == "__main__":
    unittest.main()
