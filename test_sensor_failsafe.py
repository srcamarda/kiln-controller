"""
test_sensor_failsafe.py — Garante que o sensor falha fechado (sem hardware).
Rodar:  python3 -m unittest test_sensor_failsafe -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import sensor
import relays


class FakeMAX31855:
    def __init__(self, temp=25.0, ref=24.0, exc=None):
        self._temp, self._ref, self._exc = temp, ref, exc

    @property
    def temperature(self):
        if self._exc:
            raise self._exc
        return self._temp

    @property
    def reference_temperature(self):
        return self._ref


def probe(dev):
    return sensor.HardwareSensor(dev=dev).probe(samples=2, interval=0)


class TestProbe(unittest.TestCase):

    def test_leitura_valida(self):
        self.assertEqual(probe(FakeMAX31855(25.0, 24.0)), 25.0)

    def test_termopar_aberto(self):
        with self.assertRaises(sensor.SensorInitError):
            probe(FakeMAX31855(exc=RuntimeError("thermocouple not connected")))

    def test_leitura_none(self):
        with self.assertRaises(sensor.SensorInitError):
            probe(FakeMAX31855(temp=None))

    def test_palavra_spi_zerada(self):
        with self.assertRaises(sensor.SensorInitError):
            probe(FakeMAX31855(0.0, 0.0))

    def test_temperatura_implausivel(self):
        with self.assertRaises(sensor.SensorInitError):
            probe(FakeMAX31855(2047.75, 25.0))

    def test_juncao_fria_implausivel(self):
        with self.assertRaises(sensor.SensorInitError):
            probe(FakeMAX31855(25.0, 127.0))


class TestCreateSensor(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SIM_TEMP", None)

    def tearDown(self):
        self._env.stop()

    def test_sem_bibliotecas_nao_simula(self):
        with mock.patch.object(sensor, "_HW_AVAILABLE", False):
            with self.assertRaises(sensor.SensorInitError):
                sensor.create_sensor()

    def test_sim_temp_explicito(self):
        os.environ["SIM_TEMP"] = "1"
        self.assertIsInstance(sensor.create_sensor(), sensor.SimulatedSensor)

    def test_falha_ao_abrir_spi(self):
        with mock.patch.object(sensor, "_HW_AVAILABLE", True), \
             mock.patch.object(sensor, "HardwareSensor", side_effect=OSError("SPI off")):
            with self.assertRaises(sensor.SensorInitError):
                sensor.create_sensor()

    def test_hardware_ok(self):
        fake = sensor.HardwareSensor(dev=FakeMAX31855(30.0, 26.0))
        with mock.patch.object(sensor, "_HW_AVAILABLE", True), \
             mock.patch.object(sensor, "HardwareSensor", return_value=fake), \
             mock.patch.object(sensor.time, "sleep"):
            self.assertIs(sensor.create_sensor(), fake)


class TestCreateRelays(unittest.TestCase):

    def test_sem_gpio_nao_simula(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIM_RELAYS", None)
            with mock.patch.object(relays, "_GPIO_AVAILABLE", False):
                with self.assertRaises(RuntimeError):
                    relays.create_relays()

    def test_sim_relays_explicito(self):
        with mock.patch.dict(os.environ, {"SIM_RELAYS": "1"}):
            self.assertIsInstance(relays.create_relays(), relays.SimulatedRelays)


if __name__ == "__main__":
    unittest.main()
