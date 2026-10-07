"""
test_simulation.py — Todas as curvas do curves.json numa simulação realista
(passo de 5 s, ~7 °C/min com os dois bancos, ruído de ±1,5 °C na leitura).
Garante que concluem sem disparar limites de segurança e sem sobressinal
excessivo. A versão "visual" (tabela) é tools/simulate_curves.py.

Rodar:  python3 -m unittest tests.test_simulation -v
"""

import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller as c
from controller import KilnController, ControllerMode
from curves_manager import load_curves

STEP_S      = 5
GAIN_A      = 0.06     # °C/s só banco A
GAIN_AB     = 0.12     # °C/s A + B
LOSS        = 0.02     # °C/s desligado
NOISE_C     = 1.5
MAX_HOURS   = 30
MAX_SOAK_OVERSHOOT_C = 10.0


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000 + self.t


def simulate(curve, seed=1):
    clock, rng = Clock(), random.Random(seed)
    with mock.patch.object(c, "time", clock):
        k = KilnController()
        temp = 25.0
        k.start_curve(curve, temp)
        soak_max_over = 0.0
        for _ in range(int(MAX_HOURS * 3600 / STEP_S)):
            clock.t += STEP_S
            s = k.update(temp + rng.uniform(-NOISE_C, NOISE_C), False)
            if s.mode == ControllerMode.SOAKING:
                soak_max_over = max(soak_max_over, temp - s.setpoint)
            if s.mode in (ControllerMode.FINISHED, ControllerMode.ERROR):
                break
            gain = GAIN_AB if (s.bank_a and s.bank_b) else GAIN_A if s.bank_a else 0
            temp = temp + gain * STEP_S if gain else max(25.0, temp - LOSS * STEP_S)
        return s, temp, soak_max_over, k.run_elapsed_seconds()


class TestCurvasDoArquivo(unittest.TestCase):

    def test_todas_concluem(self):
        curves, warnings = load_curves()
        self.assertEqual(warnings, [])
        for curve in curves:
            with self.subTest(curva=curve["name"]):
                s, temp, over, elapsed = simulate(curve)
                self.assertEqual(s.mode, ControllerMode.FINISHED, s.error_message)
                final = curve["segments"][-1]["target_temp"]
                self.assertAlmostEqual(temp, final, delta=10)
                self.assertLessEqual(over, MAX_SOAK_OVERSHOOT_C)
                self.assertLess(elapsed, MAX_HOURS * 3600)


class TestTempoDecorrido(unittest.TestCase):

    def test_ajuste_de_relogio_nao_afeta_duracao(self):
        clock = Clock()
        with mock.patch.object(c, "time", clock):
            k = KilnController()
            k.start_preheat(200, 25)
            clock.t += 3600
            before = k.run_elapsed_seconds()
            # relógio de parede pula 1 dia (NTP/RTC) — monotonic não muda
            clock.time = lambda: 1_800_000_000 + clock.t + 86400
            self.assertAlmostEqual(k.run_elapsed_seconds(), before)
            self.assertAlmostEqual(before, 3600)


if __name__ == "__main__":
    unittest.main()
