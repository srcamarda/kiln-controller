"""
test_remote_commands.py — Comandos do Telegram executados pelo loop principal.
Rodar:  python3 -m unittest tests.test_remote_commands -v
"""

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import firing_state
import notifier as n
from controller import KilnController, ControllerMode
from firing_log import FiringLog

CURVE = {"id": "t", "name": "Teste", "soak_minutes": 10,
         "segments": [{"rate_per_hour": 100, "target_temp": 600}]}


def _import_main():
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil = types.ModuleType("PIL")
        for name in ("Image", "ImageDraw", "ImageFont"):
            setattr(pil, name, mock.MagicMock())
            sys.modules["PIL." + name] = getattr(pil, name)
        sys.modules["PIL"] = pil
    import main
    return main


class Base(unittest.TestCase):

    def setUp(self):
        self.main = _import_main()
        self.dir = tempfile.mkdtemp()
        p = mock.patch.object(firing_state, "STATE_FILE", os.path.join(self.dir, "f.json"))
        p.start()
        self.addCleanup(p.stop)
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        import queue
        app._commands = queue.Queue()
        app._main_thread = threading.get_ident()
        app.sensor = mock.MagicMock(temperature=300.0, fault=False, last_raw=300.0,
                                    fail_count=0, fault_reason="")
        app.controller = KilnController()
        app.relays = mock.MagicMock()
        app.notifier = mock.MagicMock()
        app.rtc = mock.MagicMock()
        from datetime import datetime
        app.rtc.now.return_value = datetime(2026, 10, 7, 10, 0, 0)
        app.ui = m.UIState.IDLE
        app.run_max_temp = 0.0
        app.run_start_str = ""
        app.preheat_target = 200
        app.last_hourly_announcement = 0
        app._last_save = 0.0
        app._saved_key = None
        app._state_saved = False
        app._resume_times = []
        app.flog = FiringLog(os.path.join(self.dir, "logs"))
        app.curves = [CURVE]
        app.curve_sel = 0
        self.app = app

    def run_remote(self, fn, drain=True, max_s=3.0):
        """Chama fn numa thread 'Telegram' enquanto o 'loop principal' drena a fila."""
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("r", fn()))
        t.start()
        end = time.monotonic() + max_s
        while t.is_alive() and time.monotonic() < end:
            if drain:
                self.app._process_commands()
            time.sleep(0.01)
        t.join(max_s)
        return box.get("r")


class TestStopRemoto(Base):

    def test_stop_executa_no_loop_principal(self):
        self.app._start_curve()
        self.app._persist_firing(300.0, force=True)
        self.assertIsNotNone(firing_state.load())
        seen = {}
        orig = self.app._remote_emergency_stop

        def spy():
            seen["thread"] = threading.get_ident()
            return orig()
        self.app._remote_emergency_stop = spy

        result = self.run_remote(self.app._remote_stop_request)
        self.assertEqual(seen["thread"], threading.get_ident())     # rodou no loop principal
        self.assertIn("interrompida", result)
        self.assertEqual(self.app.controller.state.mode, ControllerMode.OFF)
        self.assertEqual(self.app.ui, self.main.UIState.IDLE)
        self.assertIsNone(firing_state.load())
        self.assertFalse(self.app.flog.active)

    def test_stop_sem_queima(self):
        result = self.run_remote(self.app._remote_stop_request)
        self.assertIn("Nenhuma queima", result)
        self.app.relays.off.assert_called()

    def test_loop_travado_desliga_reles_direto(self):
        self.app._start_curve()
        with mock.patch.object(self.main, "REMOTE_TIMEOUT_S", 0.2):
            result = self.run_remote(self.app._remote_stop_request, drain=False)
        self.assertIn("não respondeu", result)
        self.app.relays.off.assert_called()

    def test_ciclo_de_controle_nao_desfaz_o_stop(self):
        """Antes: o /stop rodava em outra thread e o próximo ciclo podia religar."""
        self.app._start_curve()
        self.run_remote(self.app._remote_stop_request)
        self.app._control_tick()
        s = self.app.controller.state
        self.assertFalse(s.bank_a or s.bank_b)
        self.assertEqual(s.mode, ControllerMode.OFF)


class TestStatusRemoto(Base):

    def test_status_gerado_no_loop(self):
        self.app._start_curve()
        result = self.run_remote(self.app._remote_status)
        self.assertIn("Teste", result)

    def test_status_com_loop_travado(self):
        with mock.patch.object(self.main, "REMOTE_TIMEOUT_S", 0.2):
            result = self.run_remote(self.app._remote_status, drain=False)
        self.assertIn("não respondeu", result)


class TestRespostaNoTelegram(unittest.TestCase):

    def _notifier(self, cb):
        t = n.TelegramNotifier("", [], stop_callback=cb, stop_password="s")
        t.chat_ids = ["1"]
        t.stop_callback = cb
        t.sent = []
        t.send = lambda m, specific_chat_id=None, critical=False: t.sent.append(m)
        t._delete_message = lambda *a: None
        return t

    def _confirm(self, t):
        for text in ("/stop", "s"):
            t._handle_update({"text": text, "chat": {"id": 1}, "from": {"id": 1}, "message_id": 1})

    def test_usa_texto_do_callback(self):
        t = self._notifier(lambda: "Nenhuma queima em andamento — relés confirmados desligados.")
        self._confirm(t)
        self.assertIn("Nenhuma queima", t.sent[-1])

    def test_erro_no_callback_avisa(self):
        def boom():
            raise RuntimeError("falhou")
        t = self._notifier(boom)
        self._confirm(t)
        self.assertIn("FALHA AO EXECUTAR", t.sent[-1])


if __name__ == "__main__":
    unittest.main()
