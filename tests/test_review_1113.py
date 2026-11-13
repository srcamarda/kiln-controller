"""
test_review_1113.py — Achado da revisão automática de 13/11/2026:
  M1 — exceção num handler da interface não pode derrubar o loop principal.
Rodar:  python3 -m unittest tests.test_review_1113 -v
"""

import io
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
