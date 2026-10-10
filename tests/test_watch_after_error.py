"""
test_watch_after_error.py — Vigilância depois de um ERRO e falha de termopar
com o forno parado (achado A2).
Rodar:  python3 -m unittest tests.test_watch_after_error -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller as c
from controller import KilnController, ControllerMode, ErrorKind

CURVE = {"id": "t", "name": "Teste", "soak_minutes": 10,
         "segments": [{"rate_per_hour": 100, "target_temp": 600}]}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000 + self.t


class Base(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.k = KilnController()

    def tick(self, temp, fault=False, dt=5.0):
        self.clock.t += dt
        return self.k.update(temp, fault)

    def ramp(self, start, end, seconds, dt=5.0):
        n = int(seconds / dt)
        s = None
        for i in range(n):
            s = self.tick(start + (end - start) * (i + 1) / n, dt=dt)
        return s


class TestFalhaDoTermoparParado(Base):

    def test_reproducao_do_achado(self):
        """Antes: falha de 15 s parado → ERRO silencioso e 25 → 200 °C sem alerta."""
        self.tick(25)
        s = self.tick(25, fault=True)
        self.assertEqual(s.mode, ControllerMode.OFF)          # não trava em ERRO
        self.assertTrue(s.sensor_blind)
        self.assertEqual((s.alarm_count, s.alarm_kind), (1, "sensor"))

        s = self.tick(25)                                     # leitura voltou
        self.assertFalse(s.sensor_blind)
        self.assertEqual((s.alarm_count, s.alarm_kind), (2, "sensor_ok"))

        s = self.ramp(25, 200, 1800)                          # contator colado
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertEqual(s.error_kind, ErrorKind.RUNAWAY)

    def test_falha_intermitente_nao_inunda_de_avisos(self):
        for _ in range(10):
            self.tick(25, fault=True)
            self.tick(25)
        self.assertLessEqual(self.k.state.alarm_count, 2)

    def test_falha_durante_queima_continua_abortando(self):
        self.k.start_curve(CURVE, 25)
        s = self.tick(30, fault=True)
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertEqual(s.error_kind, ErrorKind.SENSOR)

    def test_falha_em_erro_mantem_reles_desligados(self):
        self.k.start_curve(CURVE, 25)
        self.tick(30, fault=True)
        s = self.tick(30, fault=True)
        self.assertFalse(s.bank_a or s.bank_b)


class TestVigilanciaPosErro(Base):

    def _runaway_error(self):
        self.tick(300)
        s = self.ramp(300, 340, 600)                          # +40 °C com relés desligados
        self.assertEqual(s.error_kind, ErrorKind.RUNAWAY)
        return s.alarm_count

    def test_contator_colado_alerta_de_novo_enquanto_sobe(self):
        n0 = self._runaway_error()
        s = self.ramp(340, 360, 300)                          # 5 min: sem repetir ainda
        self.assertEqual(s.alarm_count, n0)
        s = self.ramp(360, 400, 400)                          # passou de 10 min e subiu
        self.assertEqual(s.alarm_count, n0 + 1)
        self.assertEqual(s.alarm_kind, "watch")
        self.assertIn("relés desligados", s.alarm_message)
        s = self.ramp(400, 450, 700)                          # mais 10 min subindo
        self.assertEqual(s.alarm_count, n0 + 2)
        self.assertEqual(s.mode, ControllerMode.ERROR)        # continua travado
        self.assertFalse(s.bank_a or s.bank_b)

    def test_esfriando_nao_repete(self):
        n0 = self._runaway_error()
        s = self.ramp(340, 300, 3600)
        self.assertEqual(s.alarm_count, n0)

    def test_erro_de_outro_tipo_seguido_de_aquecimento_alerta_na_hora(self):
        self.k.start_curve(CURVE, 25)
        self.tick(400)
        self.k.fail(ErrorKind.STALL, "Temp. não sobe", "x")
        n0 = self.k.state.alarm_count
        s = self.ramp(400, 440, 300)
        self.assertEqual(s.alarm_count, n0 + 1)

    def test_sobretemperatura_absoluta_em_erro(self):
        self.tick(1290)
        s = self.tick(c.OVERTEMP_ABS + 5)
        self.assertEqual(s.error_kind, ErrorKind.OVERTEMP)
        n0 = s.alarm_count
        s = self.ramp(c.OVERTEMP_ABS + 5, c.OVERTEMP_ABS + 20, 660)
        self.assertEqual(s.alarm_count, n0 + 1)

    def test_reconhecer_erro_volta_a_vigiar(self):
        self._runaway_error()
        self.k.stop()
        s = self.ramp(340, 380, 600)                          # continua colado
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertEqual(s.error_kind, ErrorKind.RUNAWAY)


class TestMensagensNoMain(unittest.TestCase):

    def test_alerta_vai_para_o_telegram_e_healthchecks(self):
        from tests.test_firing_log import TestQueimaCompleta
        t = TestQueimaCompleta("test_cancelamento")
        t.setUp()
        try:
            app = t._app()
            app.hb = mock.MagicMock(active=False)
            app.sensor.temp = 300
            app._control_tick()
            app.sensor.temp = 345                            # aquece com tudo desligado
            for _ in range(3):
                t.clock.t += 5
                app._control_tick()
            self.assertEqual(app.controller.state.error_kind, ErrorKind.RUNAWAY)
            app.notifier.send.reset_mock()
            app.hb.reset_mock()
            for temp in range(350, 420, 5):                  # continua subindo por 14 min
                app.sensor.temp = temp
                t.clock.t += 60
                app._control_tick()
            msgs = [cl.args[0] for cl in app.notifier.send.call_args_list]
            self.assertTrue(any("ALERTA" in m for m in msgs), msgs)
            app.hb.fail.assert_called()
        finally:
            t.doCleanups()

    def test_termopar_parado_avisa_sem_healthchecks(self):
        from tests.test_firing_log import TestQueimaCompleta
        t = TestQueimaCompleta("test_cancelamento")
        t.setUp()
        try:
            app = t._app()
            app.hb = mock.MagicMock(active=False)
            app.sensor.fault = True
            app._control_tick()
            self.assertIn("Falha no termopar", app.notifier.send.call_args.args[0])
            app.hb.fail.assert_not_called()
            self.assertNotEqual(app.ui, t.main.UIState.ERROR)
        finally:
            t.doCleanups()


if __name__ == "__main__":
    unittest.main()
