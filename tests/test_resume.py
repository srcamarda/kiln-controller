"""
test_resume.py — Persistência e retomada da queima após reinício.
Rodar:  python3 -m unittest tests.test_resume -v
"""

import os
import socket
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

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
        from heartbeat import Heartbeat
        app.hb = Heartbeat("")
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

    # ── M1: hora não confiável ──────────────────────────────────────────

    Y2000 = datetime(2000, 1, 1, 0, 5).timestamp()   # DS3231 zerado (bateria fraca)

    def _assert_retomou_sem_hora(self, app):
        self.assertEqual(app.ui, self.main.UIState.RUNNING)
        txt = app.notifier.send.call_args.args[0]
        self.assertIn("desconhecido", txt)
        self.assertIn("sync_rtc.py", txt)

    def test_estado_guarda_validade_do_relogio(self):
        self._save_running()
        self.assertIs(firing_state.load()["clock_ok"], True)

    def test_relogio_zerado_pouca_queda_retoma(self):
        self._save_running(700)
        self.wall = self.Y2000
        app = self._app(680)
        app._try_resume()
        self._assert_retomou_sem_hora(app)

    def test_relogio_zerado_muita_queda_nao_retoma(self):
        self._save_running(700)
        self.wall = self.Y2000
        app = self._app(700 - self.main.MAX_DROP_UNKNOWN_TIME - 20)
        app._try_resume()
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)
        self.assertIn("desconhecido", app.notifier.send.call_args.args[0])
        self.assertIsNone(firing_state.load())

    def test_relogio_voltou_e_tratado_como_desconhecido(self):
        self._save_running(700)
        self.wall -= 3600                     # ano plausível, mas antes do último registro
        app = self._app(650)
        app._try_resume()
        self._assert_retomou_sem_hora(app)

        self._save_running(700)
        self.wall -= 3600
        app = self._app(500)
        app._try_resume()
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)

    def test_pequeno_atraso_do_relogio_e_tolerado(self):
        self._save_running(700)
        self.wall -= 60
        app = self._app(500)                  # esfriou bastante, mas a hora é confiável
        app._try_resume()
        self.assertEqual(app.ui, self.main.UIState.RUNNING)
        self.assertNotIn("desconhecido", app.notifier.send.call_args.args[0])

    def test_ds3231_perdeu_energia_ignora_tempo_calculado(self):
        self._save_running(700)
        self.wall += self.main.MAX_RESUME_DOWNTIME_S + 3600   # "5 h", mas a hora não vale
        app = self._app(690)
        app.rtc.time_valid.return_value = False
        app._try_resume()
        self._assert_retomou_sem_hora(app)

    def test_relogio_invalido_durante_a_queima(self):
        self._save_running(700, clock_ok=False)
        self.wall += 60
        app = self._app(600)
        app._try_resume()
        self.assertEqual(app.controller.state.error_kind, ErrorKind.INTERRUPTED)

    def test_estado_antigo_sem_campo_clock_ok(self):
        self._save_running(700)
        snap = firing_state.load()
        del snap["clock_ok"]
        firing_state.save(snap)
        self.wall += 90
        app = self._app(690)
        app._try_resume()
        self.assertEqual(app.ui, self.main.UIState.RUNNING)


class TestValidadeRelogio(unittest.TestCase):

    class Dev:
        def __init__(self, dt, lost=False):
            self.dt, self.lost_power, self.fail = dt, lost, False

        @property
        def datetime(self):
            if self.fail:
                raise OSError("i2c")
            return self.dt.timetuple()

    def test_hora_boa(self):
        import rtc
        self.assertTrue(rtc.HardwareRTC(self.Dev(datetime(2026, 10, 10, 9))).time_valid())

    def test_oscilador_parou(self):
        import rtc
        self.assertFalse(rtc.HardwareRTC(self.Dev(datetime(2026, 10, 10, 9), lost=True)).time_valid())

    def test_data_zerada(self):
        import rtc
        self.assertFalse(rtc.HardwareRTC(self.Dev(datetime(2000, 1, 1))).time_valid())

    def test_sem_leitura_boa(self):
        import contextlib, io, rtc
        dev = self.Dev(datetime(2026, 10, 10, 9))
        dev.fail = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(rtc.HardwareRTC(dev).time_valid())

    def test_system_rtc(self):
        import rtc
        with mock.patch.object(rtc.SystemRTC, "now", return_value=datetime(1970, 1, 1, 0, 1)):
            self.assertFalse(rtc.SystemRTC().time_valid())
        with mock.patch.object(rtc.SystemRTC, "now", return_value=datetime(2026, 10, 10)):
            self.assertTrue(rtc.SystemRTC().time_valid())


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
