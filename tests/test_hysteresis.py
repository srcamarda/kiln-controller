"""
test_hysteresis.py — Histerese com memória e tempos mínimos de comutação.
Rodar:  python3 -m unittest tests.test_hysteresis -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller as c
from controller import KilnController, ControllerMode


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_700_000_000 + self.t


class Base(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.k = KilnController()
        self.k.start_preheat(300, 300)       # setpoint fixo = 300 °C

    def tick(self, temp, dt=5.0):
        self.clock.t += dt
        return self.k.update(temp, False)


class TestHisterese(Base):

    def test_liga_abaixo_da_banda(self):
        s = self.tick(300 - c.HYST_BELOW - 0.5)
        self.assertTrue(s.bank_a)

    def test_desliga_acima_da_banda(self):
        self.tick(290)
        s = self.tick(300 + c.HYST_ABOVE + 0.5, dt=c.MIN_ON_S + 1)
        self.assertFalse(s.bank_a)

    def test_mantem_estado_dentro_da_banda(self):
        # Ligado → continua ligado dentro da banda
        self.tick(290)
        for t in (298, 301, 303, 299):
            self.assertTrue(self.tick(t, dt=c.MIN_ON_S + 1).bank_a)
        # Desligado → continua desligado dentro da banda
        self.tick(310, dt=c.MIN_ON_S + 1)
        for t in (302, 299, 297, 301):
            self.assertFalse(self.tick(t, dt=c.MIN_OFF_S + 1).bank_a)

    def test_ruido_no_setpoint_nao_comuta(self):
        """Antes: cada cruzamento do setpoint comutava o relé."""
        self.tick(290)
        switches, last = 0, True
        for i in range(200):
            s = self.tick(300 + (1.5 if i % 2 else -1.5))
            if s.bank_a != last:
                switches += 1
                last = s.bank_a
        self.assertEqual(switches, 0)


class TestTempoMinimo(Base):

    def test_minimo_ligado(self):
        self.tick(290)                        # liga
        s = self.tick(320, dt=5)              # passou da banda logo em seguida
        self.assertTrue(s.bank_a)             # ainda dentro do tempo mínimo
        s = self.tick(320, dt=c.MIN_ON_S)
        self.assertFalse(s.bank_a)

    def test_minimo_desligado(self):
        self.tick(290)
        self.tick(320, dt=c.MIN_ON_S + 1)     # desliga
        s = self.tick(280, dt=5)
        self.assertFalse(s.bank_a)
        s = self.tick(280, dt=c.MIN_OFF_S)
        self.assertTrue(s.bank_a)

    def test_seguranca_ignora_tempo_minimo(self):
        self.tick(290)                        # acabou de ligar
        s = self.k.update(25.0, True)         # falha de sensor
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertFalse(s.bank_a or s.bank_b)

    def test_sobretemperatura_ignora_tempo_minimo(self):
        self.tick(290)
        s = self.tick(300 + c.OVERTEMP_MARGIN + 1, dt=1)
        self.assertEqual(s.mode, ControllerMode.ERROR)
        self.assertFalse(s.bank_a)

    def test_stop_ignora_tempo_minimo(self):
        self.tick(290)
        self.k.stop()
        self.assertFalse(self.k.state.bank_a)


class TestBancoB(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.k = KilnController()
        curve = {"name": "t", "soak_minutes": 10,
                 "segments": [{"rate_per_hour": 100, "target_temp": 1000}]}
        self.k.start_curve(curve, 700)

    def test_b_liga_acima_de_600_e_respeita_histerese(self):
        self.clock.t += 600              # setpoint ≈ 716,7 °C
        s = self.k.update(700, False)
        self.assertTrue(s.bank_a and s.bank_b)
        self.clock.t += c.MIN_ON_S + 1
        with mock.patch.object(self.k, "_update_rate"):        # rate fora da questão
            s = self.k.update(self.k.state.setpoint + 1, False)   # dentro da banda
        self.assertTrue(s.bank_b)

    def test_supressao_respeita_tempo_minimo(self):
        self.clock.t += 600
        self.k.update(700, False)          # liga A e B
        self.k.state.current_rate = 1000   # subindo rápido demais
        self.k.state.rate_valid   = True
        with mock.patch.object(self.k, "_update_rate"):
            self.clock.t += 5
            s = self.k.update(705, False)
            self.assertTrue(s.bank_b_suppressed)
            self.assertTrue(s.bank_b)      # ainda no tempo mínimo ligado
            self.clock.t += c.MIN_ON_S
            s = self.k.update(705, False)
            self.assertFalse(s.bank_b)


class TestRateMinimo(unittest.TestCase):
    """Logo após iniciar, poucas amostras não podem suprimir o banco B."""

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(c, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.k = KilnController()
        curve = {"name": "t", "soak_minutes": 10,
                 "segments": [{"rate_per_hour": 100, "target_temp": 1000}]}
        self.k.start_curve(curve, 700)

    def test_rate_invalido_no_inicio(self):
        temp = 700.0
        for _ in range(int(c.RATE_MIN_WINDOW / 5) - 1):
            self.clock.t += 5
            temp += 1.0                      # 720 °C/h, bem acima de 120 °C/h
            s = self.k.update(temp, False)
            self.assertFalse(s.rate_valid)
            self.assertFalse(s.bank_b_suppressed)

    def test_rate_valido_depois_da_janela(self):
        temp = 700.0
        for _ in range(int(c.RATE_MIN_WINDOW / 5) + 2):
            self.clock.t += 5
            temp += 1.0
            s = self.k.update(temp, False)
        self.assertTrue(s.rate_valid)
        self.assertTrue(s.bank_b_suppressed)


if __name__ == "__main__":
    unittest.main()
