"""
test_review_1023.py — Achados da revisão automática de 23/10/2026:
  N5 — não iniciar queima/pré-aquecimento com o termopar em falha
  N6 — sinais ignorados durante o encerramento
(N3 está em test_sensor_runtime.TestRuidoLargoN3; N4 é do deploy.)
Rodar:  python3 -m unittest tests.test_review_1023 -v
"""

import os
import signal
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from controller import ControllerMode
from tests import test_firing_log


def _Base():
    """Reaproveita o setUp/_app de test_firing_log sem herdar os testes dele."""
    return test_firing_log.TestQueimaCompleta("test_cancelamento")


class TestInicioComTermoparEmFalha(unittest.TestCase):

    def setUp(self):
        self.h = _Base()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.m = self.h.main
        self.app = self.h._app()
        self.app.sensor.fault = True
        self.app.sensor.fault_reason = "leitura instável (variação de 90 °C entre leituras seguidas)"

    def test_curva_nao_inicia(self):
        self.app.ui = self.m.UIState.CONFIRMING
        self.app._start_curve()
        self.assertEqual(self.app.controller.state.mode, ControllerMode.OFF)
        self.assertEqual(self.app.ui, self.m.UIState.MESSAGE)
        self.assertEqual(self.app.msg_title, "TERMOPAR EM FALHA")
        self.assertIn("instável", self.app.msg_text)
        self.assertEqual(self.app.msg_return, self.m.UIState.SELECTING)
        self.assertFalse(self.app.flog.active)
        self.app.notifier.send.assert_not_called()

    def test_preaquecimento_nao_inicia(self):
        self.app.ui = self.m.UIState.PREHEAT_SET
        self.app._start_preheat()
        self.assertEqual(self.app.controller.state.mode, ControllerMode.OFF)
        self.assertEqual(self.app.msg_return, self.m.UIState.PREHEAT_SET)
        self.assertFalse(self.app.flog.active)

    def test_com_termopar_bom_inicia(self):
        self.app.sensor.fault = False
        self.app._start_curve()
        self.assertEqual(self.app.controller.state.mode, ControllerMode.RAMPING)
        self.assertEqual(self.app.ui, self.m.UIState.RUNNING)


class TestSinaisNoEncerramento(unittest.TestCase):

    def test_ignora_sigterm_e_sigint(self):
        h = _Base()
        h.setUp()
        self.addCleanup(h.doCleanups)
        app = h._app()
        calls = []
        with mock.patch.object(h.main.signal, "signal",
                               side_effect=lambda s, a: calls.append((s, a))):
            app._ignore_signals()
        self.assertIn((signal.SIGTERM, signal.SIG_IGN), calls)
        self.assertIn((signal.SIGINT, signal.SIG_IGN), calls)

    def test_run_ignora_sinais_antes_da_limpeza(self):
        h = _Base()
        h.setUp()
        self.addCleanup(h.doCleanups)
        app = h._app()
        order = []
        app.display = mock.MagicMock()
        app._acquire_sensor = mock.MagicMock(side_effect=KeyboardInterrupt)
        app._ignore_signals = lambda: order.append("ignora")
        app._cleanup_hardware = lambda: order.append("limpa")
        with mock.patch.object(h.main, "sdnotify"):
            app.run()
        self.assertEqual(order, ["ignora", "limpa"])

    def test_fora_da_thread_principal_nao_quebra(self):
        import threading
        h = _Base()
        h.setUp()
        self.addCleanup(h.doCleanups)
        app = h._app()
        err = []
        t = threading.Thread(target=lambda: err.append(app._ignore_signals()))
        t.start(); t.join()
        self.assertEqual(err, [None])


if __name__ == "__main__":
    unittest.main()
