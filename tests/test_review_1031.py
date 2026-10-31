"""
test_review_1031.py — Achado da revisão automática de 31/10/2026:
  N7 — leitura do termopar congelada (valor bruto idêntico) com o
       aquecimento ligado vira falha do termopar.
Rodar:  python3 -m unittest tests.test_review_1031 -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import controller
import sensor
from tests.test_sensor_runtime import Base

CURVA = {"name": "t", "id": "t", "soak_minutes": 120,
         "segments": [{"rate_per_hour": 100, "target_temp": 600},
                      {"rate_per_hour": 150, "target_temp": 1220}]}


class TestLeituraCongeladaN7(Base):

    def _run(self, seconds, value_fn, heating=True):
        """Avança `seconds` com uma leitura a cada 0,5 s e um ciclo de
        controle (update dos relés) a cada 5 s."""
        n = int(seconds / sensor.SAMPLE_INTERVAL_S)
        for i in range(n):
            self.step(value_fn(i))
            if i % 10 == 0:
                self.s.update(heating, heating)

    def test_congelada_com_aquecimento_vira_falha(self):
        self._run(sensor.FLATLINE_S - 20, lambda i: 1215.0)
        self.assertFalse(self.s.fault)
        self._run(30, lambda i: 1215.0)
        self.assertTrue(self.s.fault)
        self.assertIn("congelada", self.s.fault_reason)

    def test_parada_sem_aquecimento_nao_e_falha(self):
        # Forno desligado e estável: leitura idêntica por 1 h é normal
        self._run(3600, lambda i: 25.0, heating=False)
        self.assertFalse(self.s.fault)

    def test_variacao_minima_nao_e_falha(self):
        # 0,25 °C (resolução do MAX31855) já basta
        self._run(3600, lambda i: 1215.0 + (0.25 if i % 40 < 20 else 0.0))
        self.assertFalse(self.s.fault)

    def test_recupera_quando_volta_a_variar(self):
        self._run(sensor.FLATLINE_S + 10, lambda i: 1215.0)
        self.assertTrue(self.s.fault)
        self._run(5, lambda i: 1215.25 + 0.25 * (i % 2))
        self.assertFalse(self.s.fault)

    def test_patamar_com_leitura_congelada_vai_para_erro(self):
        """Antes: 120 min de patamar com os dois bancos ligados e nenhum alarme
        (a leitura parada 5 °C abaixo do alvo mantém a histerese ligando)."""
        with mock.patch.object(controller, "time", self.clock):
            k = controller.KilnController()
            self.step(1215.0, n=10)
            k.start_curve(CURVA, self.s.temperature)
            for i in range(int(130 * 60 / sensor.SAMPLE_INTERVAL_S)):
                self.step(1215.0)
                if i % 10 == 0:
                    st = k.update(self.s.temperature, self.s.fault)
                    self.s.update(st.bank_a, st.bank_b)
                    if st.mode in (controller.ControllerMode.ERROR,
                                   controller.ControllerMode.FINISHED):
                        break
            self.assertEqual(st.mode, controller.ControllerMode.ERROR)
            self.assertEqual(st.error_kind, controller.ErrorKind.SENSOR)
            self.assertLess(i * sensor.SAMPLE_INTERVAL_S, sensor.FLATLINE_S + 30)


if __name__ == "__main__":
    unittest.main()
