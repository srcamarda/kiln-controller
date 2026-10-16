"""
test_sensor_runtime.py — Leitura em execução do MAX31855 (filtro e tolerância a falhas).
Rodar:  python3 -m unittest tests.test_sensor_runtime -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import sensor


class Clock:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDev:
    """Devolve valores de uma fila; Exception na fila é levantada."""
    def __init__(self):
        self.value = 25.0
        self.ref   = 24.0
        self.reads = 0

    @property
    def temperature(self):
        self.reads += 1
        v = self.value
        if isinstance(v, Exception):
            raise v
        return v

    @property
    def reference_temperature(self):
        return self.ref


class Base(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(sensor, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.dev = FakeDev()
        self.s = sensor.HardwareSensor(dev=self.dev)
        self.s.probe(samples=1)

    def step(self, value=None, n=1):
        """Avança n amostras (0,5 s cada) com o valor dado."""
        if value is not None:
            self.dev.value = value
        for _ in range(n):
            self.clock.t += sensor.SAMPLE_INTERVAL_S
            self.s.poll()


class TestTaxaDeLeitura(Base):

    def test_uma_leitura_por_intervalo(self):
        before = self.dev.reads
        for _ in range(50):           # display/controle/Telegram consultando sem parar
            _ = self.s.temperature, self.s.fault
        self.assertEqual(self.dev.reads, before)   # mesmo instante → nenhuma leitura nova
        self.step(n=4)
        self.assertEqual(self.dev.reads, before + 4)


class TestFalhaTransitoria(Base):

    def test_falha_curta_nao_e_fault(self):
        self.step(26.0, n=5)
        self.step(RuntimeError("thermocouple not connected"), n=10)   # 5 s
        self.assertFalse(self.s.fault)
        self.assertAlmostEqual(self.s.temperature, 26.0, delta=1)
        self.step(26.5, n=1)
        self.assertFalse(self.s.fault)

    def test_falha_longa_vira_fault(self):
        self.step(RuntimeError("thermocouple not connected"),
                  n=int(sensor.FAULT_TOLERANCE_S / sensor.SAMPLE_INTERVAL_S) + 2)
        self.assertTrue(self.s.fault)
        self.assertIn("thermocouple", self.s.fault_reason)

    def test_recupera_apos_fault(self):
        self.step(RuntimeError("x"), n=40)
        self.assertTrue(self.s.fault)
        self.step(25.0, n=1)
        self.assertFalse(self.s.fault)

    def test_palavra_zero_conta_como_falha(self):
        self.dev.ref = 0.0
        self.step(0.0, n=40)
        self.assertTrue(self.s.fault)
        self.assertIn("nula", self.s.fault_reason)

    def test_none_conta_como_falha(self):
        self.dev.value = None
        self.step(n=40)
        self.assertTrue(self.s.fault)


class TestFiltroDeSalto(Base):

    def test_pico_isolado_rejeitado(self):
        self.step(500.0, n=5)
        self.step(900.0, n=1)          # espúrio
        self.step(500.0, n=1)
        self.assertAlmostEqual(self.s.temperature, 500.0, delta=0.1)

    def test_novo_patamar_aceito(self):
        """Antes o filtro travava para sempre na leitura velha."""
        self.step(500.0, n=10)
        self.step(540.0, n=sensor.JUMP_RESET_COUNT + 1)
        self.assertAlmostEqual(self.s.temperature, 540.0, delta=0.1)

    def test_ruido_alternado_nao_reseta(self):
        self.step(500.0, n=10)
        for i in range(20):
            self.step(520.0 if i % 2 else 480.0)
        self.assertAlmostEqual(self.s.temperature, 500.0, delta=0.1)

    def test_rampa_normal_acompanha(self):
        t = 500.0
        for _ in range(200):          # 100 s a 10 °C/min
            t += 10 / 60 * sensor.SAMPLE_INTERVAL_S
            self.step(t)
        self.assertAlmostEqual(self.s.temperature, t, delta=1.0)


class TestRuidoN1(Base):
    """N1: leituras rejeitadas não podem congelar a temperatura sem falha."""

    def test_ruido_alternado_em_rampa_acompanha(self):
        """Cenário reproduzido em 16/10: 500 → 620 °C em 20 min com ±20 °C
        alternados. Antes: leitura parada em 500,0 °C e fault=False."""
        self.step(500.0, n=10)
        n = int(20 * 60 / sensor.SAMPLE_INTERVAL_S)
        worst = 0.0
        for i in range(n):
            real = 500.0 + 120.0 * (i + 1) / n
            self.step(real + (20.0 if i % 2 else -20.0))
            self.assertFalse(self.s.fault, f"fault em {i * 0.5:.0f} s")
            if i > 60:
                worst = max(worst, abs(self.s.temperature - real))
        self.assertAlmostEqual(self.s.temperature, 620.0, delta=5.0)
        self.assertLess(worst, 10.0)

    def test_leitura_erratica_vira_fault(self):
        import random
        rnd = random.Random(1)
        self.step(500.0, n=10)
        t0 = self.clock.t
        while not self.s.fault:
            self.step(500.0 + rnd.choice((-1, 1)) * rnd.uniform(80, 400))
            self.assertLess(self.clock.t - t0, sensor.FAULT_TOLERANCE_S + 1.5)
        self.assertIn("instável", self.s.fault_reason)
        self.assertEqual(self.s.temperature, 500.0)     # nada de lixo no histórico

    def test_rejeicoes_inconsistentes_curtas_nao_viram_fault(self):
        self.step(500.0, n=10)
        for v in (900.0, 100.0, 500.0, 500.0):
            self.step(v)
        self.assertFalse(self.s.fault)
        self.assertAlmostEqual(self.s.temperature, 500.0, delta=0.1)

    def test_recupera_depois_da_instabilidade(self):
        import random
        rnd = random.Random(2)
        self.step(500.0, n=10)
        for _ in range(40):
            self.step(500.0 + rnd.choice((-1, 1)) * rnd.uniform(80, 400))
        self.assertTrue(self.s.fault)
        self.step(505.0, n=sensor.JUMP_RESET_COUNT + 1)
        self.assertFalse(self.s.fault)
        self.assertAlmostEqual(self.s.temperature, 505.0, delta=1.0)


class TestSemHistorico(unittest.TestCase):

    def test_sem_leitura_valida_e_fault(self):
        s = sensor.HardwareSensor(dev=FakeDev())   # sem probe
        s._dev.value = RuntimeError("x")
        self.assertTrue(s.fault)


if __name__ == "__main__":
    unittest.main()
