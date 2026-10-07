"""
test_heartbeat.py — Sinal de vida externo (healthchecks.io).
Rodar:  python3 -m unittest tests.test_heartbeat -v
"""

import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import heartbeat as hbmod
from heartbeat import Heartbeat

URL = "https://hc-ping.com/0f2b8c1e-1234-4abc-9def-0123456789ab"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Base(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(hbmod, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        # sem thread: chamamos _handle_event/_tick diretamente
        p = mock.patch.object(hbmod.threading, "Thread")
        p.start()
        self.addCleanup(p.stop)
        self.reqs = []
        self.fail_net = False

        def fake_urlopen(req, timeout=None):
            if self.fail_net:
                raise urllib.error.URLError("sem rede")
            self.reqs.append((req.full_url, req.data.decode(), dict(req.header_items())))
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        p = mock.patch.object(hbmod.urllib.request, "urlopen", side_effect=fake_urlopen)
        p.start()
        self.addCleanup(p.stop)

    def drain(self, hb):
        while not hb._events.empty():
            hb._handle_event(*hb._events.get())


class TestCiclo(Base):

    def test_desligado_sem_url(self):
        hb = Heartbeat("")
        hb.start("x"); hb.update("y"); hb.finish("z")
        self.assertFalse(hb.enabled)
        self.assertTrue(hb._events.empty())

    def test_queima_completa(self):
        hb = Heartbeat(URL, "KEY")
        self.assertEqual(hb.uuid, "0f2b8c1e-1234-4abc-9def-0123456789ab")
        hb.start("Queima iniciada")
        self.drain(hb)
        self.assertEqual(self.reqs[-1][0], URL + "/start")

        hb.update('{"temp_c": 500}')
        self.clock.t += hbmod.PING_INTERVAL_S
        hb._tick()
        self.assertEqual(self.reqs[-1][0], URL)
        self.assertIn("500", self.reqs[-1][1])

        n = len(self.reqs)
        self.clock.t += 10
        hb._tick()                                   # antes do intervalo: nada
        self.assertEqual(len(self.reqs), n)

        hb.finish("Queima concluída")
        self.drain(hb)
        urls = [r[0] for r in self.reqs[-2:]]
        self.assertEqual(urls[0], URL)
        self.assertEqual(urls[1], f"{hbmod.API_BASE}/checks/{hb.uuid}/pause")
        self.assertEqual(self.reqs[-1][2].get("X-api-key"), "KEY")
        self.assertFalse(hb.active)

        n = len(self.reqs)
        self.clock.t += 10 * hbmod.PING_INTERVAL_S
        hb._tick()                                   # pausado: sem pings
        self.assertEqual(len(self.reqs), n)

    def test_erro_envia_fail(self):
        hb = Heartbeat(URL, "KEY")
        hb.start("x")
        hb.fail("ERRO: sobretemperatura")
        self.drain(hb)
        self.assertEqual(self.reqs[-1][0], URL + "/fail")
        self.assertIn("sobretemperatura", self.reqs[-1][1])

    def test_sem_chave_nao_pausa(self):
        hb = Heartbeat(URL, "")
        hb.start("x"); hb.finish("fim")
        self.drain(hb)
        self.assertFalse(any("/pause" in r[0] for r in self.reqs))

    def test_url_por_slug_nao_pausa(self):
        hb = Heartbeat("https://hc-ping.com/pingkey/forno", "KEY")
        self.assertIsNone(hb.uuid)
        hb.start("x"); hb.finish("fim")
        self.drain(hb)
        self.assertFalse(any("/pause" in r[0] for r in self.reqs))

    def test_sem_rede_nao_quebra_e_tenta_de_novo(self):
        hb = Heartbeat(URL, "KEY")
        self.fail_net = True
        hb.start("x")
        self.drain(hb)                               # 3 tentativas, sem exceção
        self.clock.t += hbmod.PING_INTERVAL_S
        hb._tick()
        self.fail_net = False
        self.clock.t += 16                           # nova tentativa em ~15 s
        hb._tick()
        self.assertEqual(self.reqs[-1][0], URL)


class TestIntegracaoMain(unittest.TestCase):
    """O main chama start/finish/fail nos momentos certos."""

    def test_eventos(self):
        from tests.test_firing_log import TestQueimaCompleta
        t = TestQueimaCompleta("test_cancelamento")
        t.setUp()
        try:
            app = t._app()
            app.hb = mock.MagicMock(active=False)
            # como no Heartbeat real, fail/finish desativam o heartbeat
            off = lambda *a: setattr(app.hb, "active", False)
            app.hb.fail.side_effect = off
            app.hb.finish.side_effect = off
            app._start_curve()
            app.hb.start.assert_called_once()
            app.hb.active = True
            t._run(app, 60)
            app.hb.update.assert_called()
            app._stop_run()
            app.hb.finish.assert_called_once()

            app.hb.reset_mock()
            app._start_curve()
            app.hb.active = True
            t._run(app, 60)
            app.sensor.temp = 600                    # sobretemperatura → ERRO
            t._run(app, 10)
            app.hb.fail.assert_called_once()
        finally:
            t.doCleanups()


if __name__ == "__main__":
    unittest.main()
