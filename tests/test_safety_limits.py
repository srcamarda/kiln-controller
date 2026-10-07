"""
test_safety_limits.py — Limites de segurança do controlador (crítico 3).
Rodar:  python3 -m unittest tests.test_safety_limits -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller as c
from controller import KilnController, ControllerMode, ErrorKind


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_700_000_000 + self.t


CURVE = {
    "name": "teste",
    "soak_minutes": 10,
    "segments": [
        {"rate_per_hour": 100, "target_temp": 600},
        {"rate_per_hour": 150, "target_temp": 1000},
    ],
}


class Base(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.k = KilnController()

    def tick(self, temp, dt=5.0, fault=False):
        self.clock.t += dt
        return self.k.update(temp, fault)

    def assertError(self, kind):
        s = self.k.state
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertEqual(s.error_kind, kind)
        self.assertFalse(s.bank_a or s.bank_b)


class TestOvertemp(Base):

    def test_absoluto_mesmo_parado(self):
        self.tick(25)
        self.tick(c.OVERTEMP_ABS + 1)
        self.assertError(ErrorKind.OVERTEMP)

    def test_relativo_ao_alvo_do_segmento(self):
        self.k.start_curve(CURVE, 25)
        self.tick(30)
        self.tick(600 + c.OVERTEMP_MARGIN + 1)
        self.assertError(ErrorKind.OVERTEMP)

    def test_relativo_preaquecimento(self):
        self.k.start_preheat(200, 25)
        self.tick(30)
        self.tick(200 + c.OVERTEMP_MARGIN + 1)
        self.assertError(ErrorKind.OVERTEMP)

    def test_dentro_da_margem_ok(self):
        self.k.start_preheat(200, 25)
        self.tick(200 + c.OVERTEMP_MARGIN - 1)
        self.assertEqual(self.k.state.mode, ControllerMode.PREHEAT)

    def test_erro_travado(self):
        self.tick(c.OVERTEMP_ABS + 1)
        self.tick(25)
        self.assertError(ErrorKind.OVERTEMP)
        self.k.stop()
        self.tick(25)
        self.assertEqual(self.k.state.mode, ControllerMode.OFF)
        self.assertIsNone(self.k.state.error_kind)


class TestRunaway(Base):

    def test_sobe_com_reles_desligados(self):
        for t in (300, 290, 280):            # resfriando, parado
            self.tick(t)
        self.tick(280 + c.RUNAWAY_RISE + 1)
        self.assertError(ErrorKind.RUNAWAY)

    def test_inercia_pequena_ok(self):
        for t in (500, 510, 515, 505):
            self.tick(t)
        self.assertEqual(self.k.state.mode, ControllerMode.OFF)


class TestStall(Base):

    def test_nao_sobe_com_aquecimento(self):
        self.k.start_preheat(500, 100)
        temp = 100.0
        # 41 min parado em 100 °C (0,1 °C/ciclo de ruído)
        for i in range(int(c.STALL_TIMEOUT_S / 5) + 2):
            self.tick(temp + (i % 2) * 0.1)
            if self.k.state.mode == ControllerMode.ERROR:
                break
        self.assertError(ErrorKind.STALL)

    def test_subida_lenta_ok(self):
        self.k.start_preheat(500, 100)
        temp = 100.0
        # 30 °C/h: 10 °C a cada 20 min < 40 min
        for _ in range(int(2 * 3600 / 5)):
            temp += 30.0 / 3600 * 5
            self.tick(temp)
        self.assertEqual(self.k.state.mode, ControllerMode.PREHEAT)

    def test_no_setpoint_nao_conta(self):
        # Mantendo a temperatura no setpoint (bang-bang) não é stall
        self.k.start_preheat(200, 200)
        for _ in range(int(2 * 3600 / 5)):
            self.tick(199)
        self.assertEqual(self.k.state.mode, ControllerMode.PREHEAT)


class TestTimeout(Base):

    def test_segmento(self):
        self.k.start_curve(CURVE, 25)
        # Segmento 1: (600-25)/100 h = 5,75 h → prazo 2×5,75 h + 1 h = 12,5 h.
        # Sobe só o bastante para não cair em stall, mas nunca chega a 592 °C.
        temp = 25.0
        hours = 0.0
        while hours < 13 and self.k.state.mode != ControllerMode.ERROR:
            temp = min(temp + 0.05, 580.0)
            self.tick(temp, dt=60)
            hours += 60 / 3600
            # mantém o setpoint perto para não acusar stall
            self.k.state._stall_ref = None
        self.assertError(ErrorKind.TIMEOUT)
        self.assertGreater(hours, 12.4)

    def test_preaquecimento(self):
        self.k.start_preheat(200, 200)
        self.tick(199, dt=c.PREHEAT_MAX_S + 1)
        self.assertError(ErrorKind.TIMEOUT)


class TestFluxoNormal(Base):

    def test_curva_completa_sem_erro(self):
        """Forno 'ideal' que acompanha o setpoint conclui sem disparar limites."""
        self.k.start_curve(CURVE, 25)
        temp = 25.0
        for _ in range(int(20 * 3600 / 5)):
            s = self.tick(temp)
            if s.mode == ControllerMode.FINISHED:
                break
            self.assertNotEqual(s.mode, ControllerMode.ERROR, s.error_message)
            gain = (0.6 if s.bank_a else 0) + (0.6 if s.bank_b else 0)
            temp += (gain - 0.05) * 5 if gain else -0.3 * 5
        self.assertEqual(self.k.state.mode, ControllerMode.FINISHED)


if __name__ == "__main__":
    unittest.main()
