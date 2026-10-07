"""
test_firing_log.py — Registro de queimas (CSV + JSON), inclusive resfriamento.
Rodar:  python3 -m unittest test_firing_log -v
"""

import csv
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import controller as c
import firing_log
import firing_state
from controller import KilnController, ControllerMode, ErrorKind
from firing_log import FiringLog, COLUMNS

CURVE = {"name": "Teste_Log*", "soak_minutes": 10,
         "segments": [{"rate_per_hour": 300, "target_temp": 300}]}


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


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000 + self.t

    def sleep(self, s):
        self.t += s


class FakeSensor:
    """Forno simples: +0,06 °C/s por banco ligado, −0,02 °C/s desligado."""
    def __init__(self):
        self.temp = 25.0
        self.fault = False
        self.fault_reason = ""
        self.fail_count = 0

    @property
    def temperature(self):
        return self.temp

    @property
    def last_raw(self):
        return self.temp

    def step(self, a, b, dt):
        g = (0.06 if a else 0) + (0.06 if b else 0)
        self.temp = self.temp + g * dt if g else max(25.0, self.temp - 0.02 * dt)

    def update(self, a, b):
        pass

    def poll(self):
        pass


class TestQueimaCompleta(unittest.TestCase):

    def setUp(self):
        self.main = _import_main()
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        for mod in (c, firing_log, self.main):
            p = mock.patch.object(mod, "time", self.clock)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(firing_state, "STATE_FILE", os.path.join(self.dir, "firing.json"))
        p.start()
        self.addCleanup(p.stop)

    def _app(self):
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        app.sensor = FakeSensor()
        app.controller = KilnController()
        app.relays = mock.MagicMock()
        app.notifier = mock.MagicMock()
        app.rtc = mock.MagicMock()
        app.rtc.now.side_effect = lambda: datetime.fromtimestamp(self.clock.time())
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
        return app

    def _run(self, app, seconds):
        for _ in range(int(seconds / 5)):
            self.clock.t += 5
            app._control_tick()
            s = app.controller.state
            app.sensor.step(s.bank_a, s.bank_b, 5)

    def _rows(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_queima_concluida_com_resfriamento(self):
        app = self._app()
        app._start_curve()
        path = app.flog.path
        self.assertTrue(path.endswith("_teste-log.csv"))
        self._run(app, 2 * 3600)                      # rampa + patamar
        self.assertEqual(app.controller.state.mode, ControllerMode.FINISHED)
        self._run(app, 5 * 3600)                      # resfriamento até < 100 °C
        self.assertFalse(app.flog.active)

        rows = self._rows(path)
        self.assertEqual(list(rows[0].keys()), COLUMNS)
        events = [r["event"] for r in rows if r["event"]]
        self.assertTrue(any("RAMPING → SOAKING" in e for e in events))
        self.assertTrue(any(e.startswith("FIM: queima concluída") for e in events))
        self.assertEqual(events[-1], "Fim do registro de resfriamento")
        cool = [r for r in rows if r["mode"] == "COOLDOWN"]
        self.assertGreater(len(cool), 30)             # 1 linha/min durante o resfriamento
        ramp = [r for r in rows if r["mode"] == "RAMPING"]
        self.assertTrue(all(r["setpoint_c"] and r["segment"] == "1" for r in ramp))

        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["curve"]["name"], "Teste_Log*")
        self.assertEqual(meta["params"]["HYST_BELOW"], c.HYST_BELOW)
        self.assertEqual(meta["end_reason"], "queima concluída")
        summ = meta["summary"]
        self.assertGreater(summ["max_temp"], 295)
        self.assertGreater(summ["switches_a"], 0)
        self.assertIn("1", summ["segments"])
        # forno simulado só com o banco A abaixo de 600 °C sobe no máximo 216 °C/h
        # (abaixo dos 300 °C/h programados) — exatamente o que o log deve revelar
        self.assertAlmostEqual(summ["segments"]["1"]["actual_rate_c_h"], 216, delta=15)

    def test_erro_registra_motivo(self):
        app = self._app()
        app._start_curve()
        path = app.flog.path
        self._run(app, 600)
        app.sensor.temp = 400                      # > alvo 300 + 50 → sobretemperatura
        self._run(app, 10)
        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertIn("OVERTEMP", meta["end_reason"])

    def test_cancelamento(self):
        app = self._app()
        app._start_curve()
        self._run(app, 300)
        app._stop_run()
        with open(os.path.splitext(app.flog.path)[0] + ".json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["end_reason"], "cancelada pelo operador")


class TestUnidade(unittest.TestCase):

    def test_sem_diretorio_gravavel_nao_quebra(self):
        log = FiringLog("/proc/nao-existe/logs")
        log.start("curve", "x", CURVE, {}, datetime.now(), 25.0)
        log.record({"temp_c": 25.0, "mode": "RAMPING"})
        log.end("fim")
        self.assertFalse(log.active)

    def test_retencao(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(firing_log, "MAX_FIRINGS", 3):
            log = FiringLog(d)
            for i in range(5):
                log.start("curve", f"c{i}", CURVE, {}, datetime(2026, 10, 1, 10, i), 25.0)
                log.close()
        self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".csv")]), 3)


if __name__ == "__main__":
    unittest.main()
