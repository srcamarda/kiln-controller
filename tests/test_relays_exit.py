"""
test_relays_exit.py — Relés desligados ao encerrar o programa (achado A3).
Rodar:  python3 -m unittest tests.test_relays_exit -v
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

FAKE_GPIOZERO = '''
import atexit, os
_LOG = os.environ["RELAY_TEST_LOG"]
def _log(msg):
    with open(_LOG, "a") as f:
        f.write(msg + "\\n")
class OutputDevice:
    def __init__(self, pin, active_high=True, initial_value=False):
        self.pin = pin
        self._v = initial_value
        _DEVICES.append(self)
    @property
    def value(self): return self._v
    @value.setter
    def value(self, v):
        self._v = v
        _log(f"value {self.pin} {int(bool(v))}")
    def close(self):
        _log(f"close {self.pin} -> entrada")
class RotaryEncoder: pass
class Button: pass
_DEVICES = []
def _shutdown():
    # Como o gpiozero real: fecha tudo na saída (pinos voltam a ser entrada)
    for d in _DEVICES:
        d.close()
    _log("gpiozero-shutdown")
atexit.register(_shutdown)
'''

FAKE_PINCTRL = '''#!/bin/sh
echo "pinctrl $*" >> "$RELAY_TEST_LOG"
'''


class TestOrdemNaSaida(unittest.TestCase):
    """Executa um processo Python real com gpiozero e pinctrl falsos."""

    def run_script(self, script):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "gpiozero"))
        with open(os.path.join(d, "gpiozero", "__init__.py"), "w") as f:
            f.write(FAKE_GPIOZERO)
        binp = os.path.join(d, "bin")
        os.makedirs(binp)
        with open(os.path.join(binp, "pinctrl"), "w") as f:
            f.write(FAKE_PINCTRL)
        os.chmod(os.path.join(binp, "pinctrl"), 0o755)
        log = os.path.join(d, "log.txt")
        env = dict(os.environ, RELAY_TEST_LOG=log,
                   PYTHONPATH=os.pathsep.join([ROOT, d]),
                   PATH=binp + os.pathsep + os.environ.get("PATH", ""))
        env.pop("SIM_RELAYS", None)
        subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                       env=env, check=True, capture_output=True, timeout=30)
        with open(log) as f:
            return [l.strip() for l in f if l.strip()]

    @unittest.skipUnless(os.name == "posix", "pinctrl falso é um script de shell")
    def test_pinos_terminam_em_high_depois_do_gpiozero(self):
        log = self.run_script("""
            import relays
            import encoder            # outro módulo que usa gpiozero, importado depois
            r = relays.create_relays(13, 16)
            r.set(True, True)
            r.cleanup()
        """)
        i_shutdown = log.index("gpiozero-shutdown")
        after = log[i_shutdown + 1:]
        self.assertIn("pinctrl set 13 op dh", after)       # HIGH depois de o gpiozero
        self.assertIn("pinctrl set 16 op dh", after)       # devolver os pinos como entrada
        # cleanup() não fecha os pinos (só o encerramento do gpiozero fecha)
        self.assertNotIn("close 13 -> entrada", log[:i_shutdown - 2])

    def test_sem_relés_de_hardware_nada_a_fazer(self):
        log = self.run_script("""
            import os
            os.environ["SIM_RELAYS"] = "1"
            import relays
            relays.create_relays().cleanup()
        """)
        self.assertFalse(any(l.startswith("pinctrl") for l in log))


class TestMainImportaRelésPrimeiro(unittest.TestCase):

    def test_ordem_dos_imports(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            src = f.read()
        pos = {m: src.index(f"from {m} ") for m in ("relays", "encoder", "sensor", "rtc", "display")}
        for m in ("encoder", "sensor", "rtc", "display"):
            self.assertLess(pos["relays"], pos[m], f"relays deve vir antes de {m}")


class TestHardwareRelays(unittest.TestCase):

    def setUp(self):
        import relays
        self.relays = relays
        self.devs = []

        class Dev:
            def __init__(dev, pin, active_high=True, initial_value=False):
                dev.pin, dev.writes, dev.closed = pin, [], False
                self.devs.append(dev)
            @property
            def value(dev): return dev.writes[-1] if dev.writes else False
            @value.setter
            def value(dev, v): dev.writes.append(v)
            def close(dev): dev.closed = True

        p = mock.patch.object(relays, "OutputDevice", Dev)
        p.start()
        self.addCleanup(p.stop)

    def test_off_escreve_mesmo_com_cache_desligado(self):
        r = self.relays.HardwareRelays(13, 16)
        r.off()
        self.assertEqual([d.writes for d in self.devs], [[False], [False]])

    def test_cleanup_nao_fecha_os_pinos(self):
        r = self.relays.HardwareRelays(13, 16)
        r.set(True, False)
        r.cleanup()
        self.assertFalse(any(d.closed for d in self.devs))
        self.assertEqual(self.devs[0].writes[-1], False)

    def test_sem_ferramenta_de_gpio_avisa(self):
        with mock.patch.object(self.relays.shutil, "which", return_value=None):
            self.assertFalse(self.relays.force_pins_high([13]))

    def test_comandos_pinctrl(self):
        with mock.patch.object(self.relays.shutil, "which",
                               side_effect=lambda n: "/usr/bin/pinctrl" if n == "pinctrl" else None), \
             mock.patch.object(self.relays.subprocess, "run") as run:
            self.assertTrue(self.relays.force_pins_high([16, 13]))
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["pinctrl", "set", "13", "op", "dh"], ["pinctrl", "set", "16", "op", "dh"]])


if __name__ == "__main__":
    unittest.main()
