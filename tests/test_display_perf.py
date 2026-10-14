"""
test_display_perf.py — Conversão RGB565 rápida e quadros repetidos (achado M6).
Rodar:  python3 -m unittest tests.test_display_perf -v
"""

import contextlib
import io
import os
import random
import sys
import time as _time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from PIL import Image
    HAS_PIL = not isinstance(Image, mock.MagicMock)
except ImportError:
    HAS_PIL = False


def reference_rgb565(img):
    """Conversão antiga, pixel a pixel — referência de correção."""
    out = bytearray()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = img.getpixel((x, y))
            p = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out += bytes((p & 0xFF, p >> 8))
    return bytes(out)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@unittest.skipUnless(HAS_PIL, "Pillow não instalado")
class TestConversao(unittest.TestCase):

    def setUp(self):
        import display
        self.display = display

    def test_igual_a_referencia(self):
        rnd = random.Random(42)
        img = Image.new("RGB", (64, 48))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                     for _ in range(64 * 48)])
        self.assertEqual(self.display.rgb565_bytes(img), reference_rgb565(img))

    def test_valores_extremos(self):
        cores = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255),
                 (0x07, 0x03, 0x07), (0xF8, 0xFC, 0xF8), (10, 10, 10), (255, 140, 0)]
        img = Image.new("RGB", (len(cores), 1))
        img.putdata(cores)
        self.assertEqual(self.display.rgb565_bytes(img), reference_rgb565(img))
        self.assertEqual(self.display.rgb565_bytes(Image.new("RGB", (1, 1), (255, 255, 255))),
                         b"\xff\xff")
        self.assertEqual(self.display.rgb565_bytes(Image.new("RGB", (1, 1), (255, 0, 0))),
                         b"\x00\xf8")

    def test_tamanho_e_desempenho(self):
        img = Image.new("RGB", (self.display.W, self.display.H), (255, 140, 0))
        t0 = _time.perf_counter()
        for _ in range(10):
            buf = self.display.rgb565_bytes(img)
        per_frame = (_time.perf_counter() - t0) / 10
        self.assertEqual(len(buf), self.display.W * self.display.H * 2)
        self.assertLess(per_frame, 0.01)     # era ~20 ms por quadro num PC


@unittest.skipUnless(HAS_PIL, "Pillow não instalado")
class TestQuadrosRepetidos(unittest.TestCase):

    def setUp(self):
        import display
        self.display = display
        self.clock = Clock()
        p = mock.patch.object(display, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.d = display.Display(fb_dev="/nao/existe")
        self.fb = mock.MagicMock()
        self.d._fb = self.fb

    def writes(self):
        return self.fb.write.call_count

    def test_quadro_identico_nao_e_reescrito(self):
        self.d.show_idle(25.0, "12:00:00")
        self.d.show_idle(25.0, "12:00:00")
        self.assertEqual(self.writes(), 1)

    def test_quadro_diferente_e_escrito(self):
        self.d.show_idle(25.0, "12:00:00")
        self.d.show_idle(25.0, "12:00:01")
        self.assertEqual(self.writes(), 2)

    def test_reescreve_periodicamente(self):
        self.d.show_idle(25.0, "12:00:00")
        self.clock.t += self.d.REFRESH_S + 1
        self.d.show_idle(25.0, "12:00:00")
        self.assertEqual(self.writes(), 2)

    def test_apos_erro_reescreve_mesmo_identico(self):
        self.d.show_idle(25.0, "12:00:00")
        self.fb.write.side_effect = OSError(5, "I/O")
        with contextlib.redirect_stdout(io.StringIO()):
            self.d.show_idle(25.0, "12:00:01")          # falha → fecha
        self.assertIsNone(self.d._last_rendered)
        novo = mock.MagicMock()
        self.d._fb = novo                               # "reabriu"
        self.d.show_idle(25.0, "12:00:01")
        self.assertEqual(novo.write.call_count, 1)

    def test_escreve_rgb565_no_framebuffer(self):
        self.d.show_error("Teste")
        buf = self.fb.write.call_args.args[0]
        self.assertEqual(len(buf), self.display.W * self.display.H * 2)


if __name__ == "__main__":
    unittest.main()
