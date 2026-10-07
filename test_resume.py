"""
test_resume.py — Persistência e retomada da queima após reinício.
Rodar:  python3 -m unittest test_resume -v
"""

import os
import socket
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import controller as c
import firing_state
from controller import KilnController, ControllerMode, ErrorKind

CURVE = {
    "id": "teste",
    "name": "teste",
    "soak_minutes": 20,
    "segments": [
        {"rate_per_hour": 100, "target_temp": 600},
        {"rate_per_hour": 150, "target_temp": 1000},
        {"rate_per_hour": 60,  "target_temp": 1200},
    ],
}


class Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_700_000_000 + self.t


class TestFiringState(unittest.TestCase):

    def test_save_load_clear(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "sub", "firing.json")
        firing_state.save({"mode": "RAMPING", "x": 1}, path)
        self.assertEqual(firing_state.load(path)["x"], 1)
        self.assertFalse(os.path.exists(path + ".tmp"))
        firing_state.clear(path)
        self.assertIsNone(firing_state.load(path))

    def test_arquivo_corrompido(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b'{"mode": "RAMP')
        os.close(fd)
        self.assertIsNone(firing_state.load(path))


class TestControllerResume(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def _snap_at(self, segment_index, mode="RAMPING", **extra):
        k = KilnController()
        k.start_curve(CURVE, 25)
        k.state.segment_index = segment_index
        k.state.mode = ControllerMode[mode]
        self.clock.t += 3600
        if mode == "SOAKING":
            k.state.soak_start_time = self.clock.t - 300   # 5 min de soak feitos
        snap = k.snapshot(700)
        snap.update(extra)
        return snap

    def test_retoma_mesmo_segmento(self):
        snap = self._snap_at(1)
        k = KilnController()
        k.resume(snap, 700, downtime_s=120)
        s = k.state
        self.assertEqual(s.mode, ControllerMode.RAMPING)
        self.assertEqual(s.segment_index, 1)
        self.assertEqual(s.segment_start_temp, 700)
        self.assertIsNotNone(s.segment_deadline)
        self.assertGreaterEqual(k.run_elapsed_seconds(), 3600 + 120)

    def test_volta_segmento_se_esfriou(self):
        snap = self._snap_at(2)          # estava no 3º segmento (1000→1200)
        k = KilnController()
        k.resume(snap, 450)              # esfriou abaixo do alvo do 1º (600)
        self.assertEqual(k.state.segment_index, 0)

    def test_soak_continua_com_tempo_restante(self):
        snap = self._snap_at(2, mode="SOAKING")
        k = KilnController()
        k.resume(snap, 1190)
        self.assertEqual(k.state.mode, ControllerMode.SOAKING)
        self.assertAlmostEqual(k.soak_remaining_seconds(), 20 * 60 - 300, delta=1)

    def test_soak_esfriou_volta_a_rampar(self):
        snap = self._snap_at(2, mode="SOAKING")
        k = KilnController()
        k.resume(snap, 1100)
        self.assertEqual(k.state.mode, ControllerMode.RAMPING)
        self.assertEqual(k.state.segment_index, 2)

    def test_preaquecimento(self):
        k = KilnController()
        k.start_preheat(300, 25)
        self.clock.t += 7200
        snap = k.snapshot(290)
        k2 = KilnController()
        k2.resume(snap, 280)
        self.assertEqual(k2.state.mode, ControllerMode.PREHEAT)
        self.assertEqual(k2.state.preheat_target, 300)
        self.assertAlmostEqual(k2.state.segment_deadline - self.clock.t,
                               c.PREHEAT_MAX_S - 7200, delta=1)

    def test_sem_snapshot_quando_parado(self):
        self.assertIsNone(KilnController().snapshot(25))


# ── Decisão de retomada no main ─────────────────────────────────────────────

def _import_main():
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil = types.ModuleType("PIL")
        for n in ("Image", "ImageDraw", "ImageFont"):
            setattr(pil, n, mock.MagicMock())
            sys.modules["PIL." + n] = getattr(pil, n)
        sys.modules["PIL"] = pil
    import main
    return main


class TestMainResume(unittest.TestCase):

    def setUp(self):
        self.main = _import_main()
        self.dir = tempfile.mkdtemp()
        p = mock.patch.object(firing_state, "STATE_FILE", os.path.join(self.dir, "firing.json"))
        p.start()
        self.addCleanup(p.stop)
        self.wall = 1_800_000_000.0

    def _app(self, temp):
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        app.sensor = mock.MagicMock(temperature=temp, fault=False)
        app.controller = KilnController()
        app.relays = mock.MagicMock()
        app.notifier = mock.MagicMock()
        app.rtc = mock.MagicMock()
        app.rtc.now.side_effect = lambda: datetime.fromtimestamp(self.wall)
        app.ui = m.UIState.IDLE
        app.run_max_temp = 0.0
        app.run_start_str = ""
        app.preheat_target = 200
        app.last_hourly_announcement = 0
        app._last_save = 0.0
        app._saved_key = None
        app._state_saved = False
        app._resume_times = []
        from firing_log import FiringLog
        app.flog = FiringLog(os.path.join(self.dir, "logs"))
        app.curves = [CURVE]
        app.curve_sel = 0
        return app

    def _save_running(self, temp=700, **extra):
        a = self._app(temp)
        a._start_curve()
        a.run_start_str = "08:00"
        a.run_max_temp = temp
        a._persist_firing(temp, force=True)
        if extra:
            snap = firing_state.load()
            snap.update(extra)
            firing_state.save(snap)
        self.assertIsNotNone(firing_state.load())

    def test_retoma(self):
        self._save_running()
        self.wall += 90
        app = self._app(690)
        app._try_resume()
        self.assertEqual(app.ui, self.main.UIState.RUNNING)
        self.assertEqual(app.controller.state.mode, ControllerMode.RAMPING)
        self.assertEqual(app.run_start_str, "08:00")
        self.assertIn("retomada", app.notifier.send.call_args.args[0])
        self.assertEqual(len(firing_state.load()["resume_times"]), 1)
        # o registro da queima continua no mesmo arquivo, com evento de retomada
        log = firing_state.load()["log_file"]
        self.assertTrue(log and os.path.exists(log))
        app.flog.close()
        with open(log, encoding="utf-8") as f:
            self.assertIn("RETOMADA", f.read())

    def test_nao_retoma_apos_muito_tempo(self):
        self._save_running()
        self.wall += self.main.MAX_RESUME_DOWNTIME_S + 60
        app = self._app(100)
        app._try_resume()
        self.assertEqual(app.ui, self.main.UIState.ERROR)
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)
        self.assertIsNone(firing_state.load())

    def test_nao_retoma_reinicios_repetidos(self):
        self._save_running(resume_times=[self.wall - 600, self.wall - 300, self.wall - 60])
        self.wall += 30
        app = self._app(690)
        app._try_resume()
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)

    def test_nao_retoma_se_aqueceu_desligado(self):
        self._save_running(700)
        self.wall += 60
        app = self._app(700 + c.RUNAWAY_RISE + 5)
        app._try_resume()
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)

    def test_cancelar_apaga_estado(self):
        self._save_running()
        app = self._app(700)
        app._try_resume()
        app._stop_run()
        self.assertIsNone(firing_state.load())

    def test_erro_apaga_estado(self):
        self._save_running()
        app = self._app(700)
        app._try_resume()
        app.controller.fail(ErrorKind.STALL, "x", "y")
        app._persist_firing(700)
        self.assertIsNone(firing_state.load())

    def test_sem_arquivo_nao_faz_nada(self):
        app = self._app(25)
        app._try_resume()
        self.assertEqual(app.ui, self.main.UIState.IDLE)


class TestSdNotify(unittest.TestCase):

    def test_envia_para_socket(self):
        import importlib
        d = tempfile.mkdtemp()
        path = os.path.join(d, "notify")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(path)
        self.addCleanup(srv.close)
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
            import sdnotify
            importlib.reload(sdnotify)
            sdnotify.ready()
            sdnotify.watchdog_ping()
        self.assertEqual(srv.recv(64), b"READY=1")
        self.assertEqual(srv.recv(64), b"WATCHDOG=1")
        importlib.reload(sdnotify)

    def test_sem_systemd_nao_faz_nada(self):
        import importlib
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            import sdnotify
            importlib.reload(sdnotify)
            self.assertFalse(sdnotify.notify("READY=1"))


if __name__ == "__main__":
    unittest.main()
