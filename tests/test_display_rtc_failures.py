"""
test_display_rtc_failures.py — Falha de tela ou relógio não derruba o controle (achado A4).
Rodar:  python3 -m unittest tests.test_display_rtc_failures -v
"""

import contextlib
import io
import os
import sys
import tempfile
import time as _time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import rtc
from tests.test_firing_log import _import_main

try:
    import PIL.Image  # noqa: F401
    import PIL.ImageFont  # noqa: F401
    HAS_PIL = not isinstance(PIL.Image, mock.MagicMock)
except ImportError:
    HAS_PIL = False


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class FakeDS3231:
    def __init__(self):
        self.fail = False
        self.dt = datetime(2026, 10, 9, 12, 0, 0)

    @property
    def datetime(self):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        return self.dt.timetuple()


class TestRTC(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(rtc, "_time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_leitura_normal(self):
        dev = FakeDS3231()
        self.assertEqual(rtc.HardwareRTC(dev).now(), dev.dt)

    def test_falha_continua_a_partir_da_ultima_leitura(self):
        dev = FakeDS3231()
        r = rtc.HardwareRTC(dev)
        r.now()
        dev.fail = True
        self.clock.t += 90
        with quiet():
            self.assertEqual(r.now(), datetime(2026, 10, 9, 12, 1, 30))

    def test_falha_sem_leitura_previa_usa_hora_do_sistema(self):
        dev = FakeDS3231()
        dev.fail = True
        with quiet():
            got = rtc.HardwareRTC(dev).now()
        self.assertLess(abs((got - datetime.now()).total_seconds()), 5)

    def test_valor_invalido_nao_propaga(self):
        dev = mock.MagicMock()
        type(dev).datetime = mock.PropertyMock(
            return_value=_time.struct_time((2026, 13, 40, 0, 0, 0, 0, -1, -1)))
        with quiet():
            self.assertIsInstance(rtc.HardwareRTC(dev).now(), datetime)

    def test_recupera_apos_falha(self):
        dev = FakeDS3231()
        r = rtc.HardwareRTC(dev)
        dev.fail = True
        with quiet():
            r.now()
            dev.fail = False
            dev.dt = datetime(2026, 10, 9, 13, 0, 0)
            self.assertEqual(r.now(), dev.dt)
        self.assertFalse(r._failing)


@unittest.skipUnless(HAS_PIL, "Pillow não instalado")
class TestDisplay(unittest.TestCase):
    def setUp(self):
        import display
        self.display = display
        self.clock = Clock()
        p = mock.patch.object(display, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_sem_framebuffer_nao_levanta(self):
        d = self.display.Display(fb_dev="/nao/existe/fb9")
        with quiet():
            d.open()
            d.show_idle(25.0, "12:00:00")
            d.show_error("Teste")
            d.close()
        self.assertIsNone(d._fb)

    def test_reabre_depois_do_intervalo(self):
        with tempfile.TemporaryDirectory() as tmp:
            fb = os.path.join(tmp, "fb")
            d = self.display.Display(fb_dev=fb)
            with mock.patch.object(self.display, "open",
                                   side_effect=OSError("sem tela"), create=True):
                with quiet():
                    d.open()
            self.assertIsNone(d._fb)
            with quiet():
                d.show_idle(25.0, "12:00:00")      # antes do intervalo: ignora
            self.assertIsNone(d._fb)
            self.clock.t += d.REOPEN_S + 1
            with quiet():
                d.show_idle(25.0, "12:00:00")      # reabre e escreve
            self.assertIsNotNone(d._fb)
            d.close()
            self.assertEqual(os.path.getsize(fb), 320 * 240 * 2)

    def test_erro_de_escrita_fecha_e_agenda_nova_tentativa(self):
        d = self.display.Display(fb_dev="/nao/existe")
        fb = mock.MagicMock()
        fb.write.side_effect = OSError(5, "I/O error")
        d._fb = fb
        with quiet():
            d.show_idle(25.0, "12:00:00")
        self.assertIsNone(d._fb)
        self.assertGreater(d._next_open, self.clock.t)

    def test_fonte_ausente_usa_padrao(self):
        with mock.patch.dict(self.display._font_cache, clear=True), \
             mock.patch.object(self.display, "FONT", "/nao/existe.ttf"):
            with quiet():
                f = self.display._font(17)
        self.assertIsNotNone(f)


class TestMainTolerante(unittest.TestCase):
    def setUp(self):
        self.main = _import_main()

    def _app(self):
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        app.sensor = mock.MagicMock(temperature=25.0)
        app.controller = mock.MagicMock()
        app.display = mock.MagicMock()
        app.rtc = mock.MagicMock()
        app.rtc.now.return_value = datetime(2026, 10, 9, 12, 0, 0)
        app.ui = m.UIState.IDLE
        return app

    def test_excecao_do_display_nao_escapa(self):
        app = self._app()
        app.display.show_idle.side_effect = RuntimeError("tela quebrada")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(app._update_display())
            self.assertFalse(app._update_display())
        self.assertEqual(out.getvalue().count("Falha ao atualizar a tela"), 1)

    def test_excecao_do_rtc_nao_escapa(self):
        app = self._app()
        app.rtc.now.side_effect = OSError("i2c")
        with quiet():
            self.assertFalse(app._update_display())

    def test_normaliza_apos_falha(self):
        app = self._app()
        app.display.show_idle.side_effect = [RuntimeError("x"), None]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app._update_display()
            self.assertTrue(app._update_display())
        self.assertIn("normalizada", out.getvalue())

    def test_encerramento_continua_apesar_de_falhas(self):
        app = self._app()
        app.relays = mock.MagicMock()
        app.flog = mock.MagicMock()
        app.encoder = mock.MagicMock()
        app.flog.close.side_effect = OSError("disco")
        app.display.close.side_effect = RuntimeError("tela")
        app.encoder.cleanup.side_effect = RuntimeError("gpio")
        with quiet():
            app._cleanup_hardware()
        app.relays.off.assert_called()
        app.relays.cleanup.assert_called_once()

    def test_falha_na_parada_ainda_desliga_reles(self):
        app = self._app()
        app.relays = mock.MagicMock()
        app.flog = mock.MagicMock()
        app.encoder = mock.MagicMock()
        app.controller.stop.side_effect = RuntimeError("bug")
        with quiet():
            app._cleanup_hardware()
        self.assertGreaterEqual(app.relays.off.call_count, 2)
        app.relays.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
