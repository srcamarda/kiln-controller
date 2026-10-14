"""
test_low_priority.py — Itens de baixa prioridade da revisão de 09/10 (B1, B3, B5, B6).
Rodar:  python3 -m unittest tests.test_low_priority -v
"""

import contextlib
import copy
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import heartbeat as hbmod
from encoder import EncoderEvent
from tests.test_firing_log import _import_main

CW, CCW = EncoderEvent.ROTATE_CW, EncoderEvent.ROTATE_CCW
CLICK, LONG = EncoderEvent.CLICK, EncoderEvent.LONG_PRESS


# ── B1: relays-off.sh sem fallback que solta o pino ─────────────────────────

@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "precisa de sh")
class TestRelaysOffScript(unittest.TestCase):

    def _run(self, tools):
        d = tempfile.mkdtemp()
        log = os.path.join(d, "log")
        for name in tools:
            p = os.path.join(d, name)
            with open(p, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        env = {"PATH": d}
        script = os.path.join(ROOT, "deploy", "relays-off.sh")
        r = subprocess.run([shutil.which("sh"), script], env=env,
                           capture_output=True, text=True)
        calls = open(log).read() if os.path.exists(log) else ""
        return r, calls

    def test_usa_pinctrl(self):
        r, calls = self._run(["pinctrl", "gpioset"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("pinctrl set 13 op dh", calls)
        self.assertIn("pinctrl set 16 op dh", calls)

    def test_so_gpioset_falha_sem_soltar_pino(self):
        r, calls = self._run(["gpioset"])
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("gpioset", calls)
        self.assertIn("raspi-utils", r.stderr)


# ── B3: aviso de grupo sem admins ───────────────────────────────────────────

class TestAvisoAdmins(unittest.TestCase):

    def _start(self, chat_ids, admin_ids):
        import notifier as n
        with mock.patch.object(n.threading, "Thread"), \
             mock.patch("builtins.print") as p:
            n.TelegramNotifier("TOKEN", chat_ids, status_callback=lambda: "",
                               admin_ids=admin_ids)
        return " ".join(str(c.args[0]) for c in p.call_args_list)

    def test_grupo_sem_admins_avisa(self):
        self.assertIn("KILN_TELEGRAM_ADMIN_IDS está vazio", self._start(["-1001"], []))

    def test_grupo_com_admins_nao_avisa(self):
        self.assertNotIn("ADMIN_IDS", self._start(["-1001"], ["111"]))

    def test_privado_nao_avisa(self):
        self.assertNotIn("ADMIN_IDS", self._start(["111"], []))


# ── B5: thread do heartbeat não morre ───────────────────────────────────────

class _Stop(BaseException):
    pass


class TestHeartbeatResiliente(unittest.TestCase):

    def test_excecao_inesperada_nao_encerra_a_thread(self):
        with mock.patch.object(hbmod.threading, "Thread"):
            hb = hbmod.Heartbeat("https://hc-ping.com/x")
        calls = {"n": 0}

        def step():
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("bug")
            raise _Stop()                      # encerra o teste na 2ª volta
        hb._step = step
        out = io.StringIO()
        with mock.patch.object(hbmod.time, "sleep"), contextlib.redirect_stdout(out):
            with self.assertRaises(_Stop):
                hb._worker()
        self.assertEqual(calls["n"], 2)          # continuou depois do erro
        self.assertIn("erro inesperado", out.getvalue())


# ── B6: edição de curvas ────────────────────────────────────────────────────

CURVE = {"id": "c1", "name": "Curva 1", "soak_minutes": 20,
         "segments": [{"rate_per_hour": 100, "target_temp": 600},
                      {"rate_per_hour": 150, "target_temp": 900},
                      {"rate_per_hour": 60,  "target_temp": 1000}]}


class TestEdicaoCurva(unittest.TestCase):

    def setUp(self):
        self.main = _import_main()
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        app.curves = [copy.deepcopy(CURVE)]
        app.edit_sel = 0
        app.ui = m.UIState.CURVE_EDIT_LIST
        app.menu_sel = 3
        self.app = app
        self.saved = []
        p = mock.patch.object(m, "save_curves", side_effect=lambda c: self.saved.append(c))
        p.start()
        self.addCleanup(p.stop)

    def ev(self, *events):
        for e in events:
            self.app._handle_event(e)

    def test_alvo_limitado_pelos_segmentos_seguintes(self):
        self.ev(CLICK)                              # edita a curva
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_EDIT_SEG)
        self.ev(CLICK)                              # campo alvo do seg 1
        self.ev(*[CW] * 200)                        # tenta subir muito
        tmax = self.main.TARGET_MAX - 2 * self.main.TARGET_STEP
        self.assertEqual(self.app.edit_target, tmax)

    def test_seguinte_ajustado_ao_passar_do_alvo(self):
        self.ev(CLICK, CLICK)                       # seg 1, campo alvo (600)
        self.ev(*[CW] * 35)                         # 600 → 950 (acima do seg 2 = 900)
        self.assertEqual(self.app.edit_target, 950)
        self.ev(CLICK)                              # confirma seg 1 → seg 2
        self.assertEqual(self.app.edit_seg_idx, 1)
        self.assertEqual(self.app.edit_target, 960)  # 900 ajustado para > 950
        self.ev(CLICK, CLICK)                       # confirma seg 2 → seg 3 (1000)
        self.assertEqual(self.app.edit_target, 1000)
        self.ev(CLICK, CLICK)                       # confirma seg 3 → patamar
        self.ev(CLICK)                              # patamar → salvar
        self.ev(CLICK)                              # salva
        self.assertEqual(len(self.saved), 1)
        temps = [s["target_temp"] for s in self.saved[0][0]["segments"]]
        self.assertEqual(temps, [950, 960, 1000])

    def test_toque_longo_pede_confirmacao(self):
        self.ev(CLICK, CLICK, CW)                   # editando, alvo 610
        self.ev(LONG)
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_DISCARD)
        self.ev(LONG)                               # "voltar"
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_EDIT_SEG)
        self.assertEqual(self.app.edit_target, 610)  # edição preservada
        self.ev(LONG, CLICK)                        # confirma descartar
        self.assertEqual(self.app.ui, self.main.UIState.MENU)
        self.assertIsNone(self.app.edit_curve)
        self.assertEqual(self.saved, [])

    def test_confirmacao_tambem_no_patamar_e_salvar(self):
        self.ev(CLICK)
        for _ in range(3):
            self.ev(CLICK, CLICK)                   # passa pelos 3 segmentos
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_SOAK)
        self.ev(LONG)
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_DISCARD)
        self.ev(LONG)
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_SOAK)
        self.ev(CLICK)
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_SAVE)
        self.ev(LONG, LONG)
        self.assertEqual(self.app.ui, self.main.UIState.CURVE_SAVE)

    def test_tela_de_confirmacao_renderiza(self):
        try:
            import display
        except ImportError:
            self.skipTest("Pillow não instalado")
        d = display.Display(fb_dev="/nao/existe")
        d._fb = mock.MagicMock()
        d.show_discard_confirm(is_new=True)
        d.show_discard_confirm(is_new=False)
        self.assertEqual(d._fb.write.call_count, 2)


if __name__ == "__main__":
    unittest.main()
