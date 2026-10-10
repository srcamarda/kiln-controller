"""
test_hot_start.py — Iniciar curva/pré-aquecimento com o forno já quente (achado A1).
Rodar:  python3 -m unittest tests.test_hot_start -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller as c
from controller import KilnController, ControllerMode, ErrorKind
from curves_manager import load_curves

CONE6 = {"id": "c6", "name": "Cone 6", "soak_minutes": 30,
         "segments": [{"rate_per_hour": 60, "target_temp": 120},
                      {"rate_per_hour": 150, "target_temp": 540},
                      {"rate_per_hour": 100, "target_temp": 620},
                      {"rate_per_hour": 150, "target_temp": 1075},
                      {"rate_per_hour": 80, "target_temp": 1220}]}


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

    def run_for(self, seconds, temp_fn):
        s = None
        for i in range(int(seconds / 5)):
            self.clock.t += 5
            s = self.k.update(temp_fn(i), False)
            if s.mode == ControllerMode.ERROR:
                break
        return s


class TestCurvaComFornoQuente(Base):

    def test_cone6_a_200_pula_o_primeiro_segmento(self):
        """Reprodução do A1: antes dava ERRO de sobretemperatura na hora."""
        self.k.start_curve(CONE6, 200)
        s = self.k.state
        self.assertEqual(s.segment_index, 1)
        self.assertEqual(s.skipped_segments, 1)
        s = self.run_for(120, lambda i: 200 + i * 0.1)
        self.assertEqual(s.mode, ControllerMode.RAMPING)
        self.assertIsNone(s.error_kind)

    def test_pula_varios_segmentos(self):
        self.k.start_curve(CONE6, 600)
        self.assertEqual(self.k.state.segment_index, 2)     # 120 e 540 já atingidos

    def test_frio_comeca_do_inicio(self):
        self.k.start_curve(CONE6, 25)
        self.assertEqual(self.k.state.segment_index, 0)
        self.assertEqual(self.k.state.skipped_segments, 0)

    def test_perto_do_alvo_final_vai_para_o_patamar(self):
        self.k.start_curve(CONE6, 1215)
        self.assertEqual(self.k.state.segment_index, 4)
        s = self.run_for(10, lambda i: 1215)
        self.assertEqual(s.mode, ControllerMode.SOAKING)

    def test_acima_do_alvo_final_recusa(self):
        with self.assertRaises(ValueError) as cm:
            self.k.start_curve(CONE6, 1230)
        self.assertIn("Espere esfriar", str(cm.exception))
        self.assertEqual(self.k.state.mode, ControllerMode.OFF)

    def test_todas_as_curvas_apos_preaquecer_a_200(self):
        curves, _ = load_curves()
        for curve in curves:
            with self.subTest(curva=curve["name"]):
                k = KilnController()
                with mock.patch.object(c, "time", self.clock):
                    k.start_curve(curve, 200)
                    self.clock.t += 5
                    s = k.update(200, False)
                self.assertNotEqual(s.mode, ControllerMode.ERROR, s.error_message)

    def test_sobretemperatura_real_continua_detectada(self):
        self.k.start_curve(CONE6, 200)                        # segmento 2, alvo 540
        s = self.run_for(10, lambda i: 540 + c.OVERTEMP_MARGIN + 5)
        self.assertEqual(s.error_kind, ErrorKind.OVERTEMP)


class TestPreaquecimentoComFornoQuente(Base):

    def test_comeca_acima_do_alvo_e_esfria(self):
        """Reprodução do A1: pré-aquecer a 200 °C com o forno a 400 °C dava ERRO."""
        self.k.start_preheat(200, 400)
        s = self.run_for(3600, lambda i: max(150, 400 - i * 0.5))
        self.assertEqual(s.mode, ControllerMode.PREHEAT)
        self.assertEqual(self.k.state.preheat_ceiling, 0.0)   # teto some ao atingir o alvo

    def test_subida_acima_do_teto_ainda_e_erro(self):
        self.k.start_preheat(200, 400)
        s = self.run_for(60, lambda i: 400 + c.OVERTEMP_MARGIN + 5)
        self.assertEqual(s.error_kind, ErrorKind.OVERTEMP)

    def test_teto_acompanha_a_queda(self):
        self.k.start_preheat(200, 400)
        self.run_for(300, lambda i: 400 - i)                 # esfriou para ~341 °C
        self.assertLess(self.k.state.preheat_ceiling, 345)
        s = self.run_for(10, lambda i: 341 + c.OVERTEMP_MARGIN + 10)   # subiu de novo
        self.assertEqual(s.error_kind, ErrorKind.OVERTEMP)

    def test_reduzir_alvo_durante_preaquecimento(self):
        self.k.start_preheat(500, 25)
        self.run_for(10, lambda i: 480)
        self.k.set_preheat_target(300)                        # forno a 480, alvo 300
        s = self.run_for(60, lambda i: 480 - i)
        self.assertEqual(s.mode, ControllerMode.PREHEAT)

    def test_retomada_de_preaquecimento_acima_do_alvo(self):
        self.k.start_preheat(200, 25)
        self.clock.t += 600
        snap = self.k.snapshot(190)
        k2 = KilnController()
        k2.resume(snap, 260)
        self.k = k2
        s = self.run_for(60, lambda i: 260)
        self.assertEqual(s.mode, ControllerMode.PREHEAT)


class TestInterface(unittest.TestCase):

    def test_tela_forno_quente(self):
        from tests.test_firing_log import TestQueimaCompleta, CURVE
        t = TestQueimaCompleta("test_cancelamento")
        t.setUp()
        try:
            app = t._app()
            app.sensor.temp = 1000                            # acima do alvo final (300)
            app._start_curve()
            self.assertEqual(app.ui, t.main.UIState.MESSAGE)
            self.assertEqual(app.msg_title, "FORNO QUENTE")
            self.assertEqual(app.controller.state.mode, ControllerMode.OFF)
            app.notifier.send.assert_not_called()
        finally:
            t.doCleanups()

    def test_aviso_de_segmentos_pulados(self):
        from tests.test_firing_log import TestQueimaCompleta
        import tests.test_firing_log as tf
        t = TestQueimaCompleta("test_cancelamento")
        t.setUp()
        old = dict(tf.CURVE)
        tf.CURVE.update({"segments": [{"rate_per_hour": 100, "target_temp": 150},
                                      {"rate_per_hour": 100, "target_temp": 300}]})
        try:
            app = t._app()
            app.sensor.temp = 200
            app._start_curve()
            self.assertEqual(app.ui, t.main.UIState.RUNNING)
            self.assertIn("começando no 2", app.notifier.send.call_args.args[0])
        finally:
            tf.CURVE.clear(); tf.CURVE.update(old)
            t.doCleanups()


if __name__ == "__main__":
    unittest.main()
