"""
test_notifier.py — Formatação, entrega com retry e comando /stop do Telegram.
Rodar:  python3 -m unittest tests.test_notifier -v
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import notifier as n
from notifier import TelegramNotifier, esc, b, to_plain


def http_error(code, desc, retry_after=None):
    body = {"ok": False, "description": desc}
    if retry_after:
        body["parameters"] = {"retry_after": retry_after}
    return urllib.error.HTTPError("u", code, desc, {}, io.BytesIO(json.dumps(body).encode()))


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000 + self.t

    def sleep(self, s):
        self.t += s


def make(**kw):
    t = TelegramNotifier("", [], **kw)          # sem threads
    t.token = "TOKEN"
    t.chat_ids = ["111"]
    return t


class TestFormatacao(unittest.TestCase):

    def test_escapa_texto_variavel(self):
        self.assertEqual(esc("Cone_6 *rápido* <A&B>"), "Cone_6 *rápido* &lt;A&amp;B&gt;")
        self.assertEqual(b("x<y"), "<b>x&lt;y</b>")

    def test_texto_simples(self):
        self.assertEqual(to_plain(f"🔥 {b('Queima')}\nCurva: {esc('a<b')}"), "🔥 Queima\nCurva: a<b")


class TestEntrega(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(n, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.t = make()
        self.calls = []

    def _post_seq(self, *outcomes):
        seq = list(outcomes)

        def fake(method, params, timeout=None, quiet=False):
            self.calls.append(dict(params))
            o = seq.pop(0) if seq else None
            if isinstance(o, Exception):
                raise o
            return {"ok": True}
        return mock.patch.object(self.t, "_post", side_effect=fake)

    def test_formatacao_recusada_reenvia_texto_simples(self):
        with self._post_seq(n._BadFormat("can't parse entities")):
            self.t._deliver("111", f"{b('Curva')} x", False, 0.0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", self.calls[1])
        self.assertEqual(self.calls[1]["text"], "Curva x")

    def test_falha_de_rede_repete(self):
        with self._post_seq(n._Retry(), n._Retry(), None):
            self.t._deliver("111", "oi", False, 0.0)
        self.assertEqual(len(self.calls), 3)

    def test_normal_desiste_apos_prazo(self):
        with self._post_seq(*[n._Retry()] * 1000):
            self.t._deliver("111", "status", False, 0.0)
        self.assertGreater(self.clock.t, n.NORMAL_MAX_AGE_S)
        self.assertLess(self.clock.t, n.NORMAL_MAX_AGE_S + 120)

    def test_critica_insiste_bem_mais(self):
        with self._post_seq(*[n._Retry()] * 100 + [None]):
            self.t._deliver("111", "ERRO", True, 0.0)
        self.assertEqual(len(self.calls), 101)          # entregue após ~1,5 h tentando
        self.assertGreater(self.clock.t, n.NORMAL_MAX_AGE_S)

    def test_erro_permanente_nao_repete(self):
        with self._post_seq(n._Fatal("403 bot was blocked")):
            self.t._deliver("111", "x", True, 0.0)
        self.assertEqual(len(self.calls), 1)


class TestClassificacaoHTTP(unittest.TestCase):

    def setUp(self):
        self.t = make()

    def _raise(self, exc):
        return mock.patch.object(n.urllib.request, "urlopen", side_effect=exc)

    def test_400_parse(self):
        with self._raise(http_error(400, "Bad Request: can't parse entities")):
            with self.assertRaises(n._BadFormat):
                self.t._post("sendMessage", {})

    def test_429(self):
        with self._raise(http_error(429, "Too Many Requests", retry_after=7)):
            with self.assertRaises(n._Retry) as cm:
                self.t._post("sendMessage", {})
        self.assertEqual(cm.exception.delay, 7)

    def test_500(self):
        with self._raise(http_error(502, "Bad Gateway")):
            with self.assertRaises(n._Retry):
                self.t._post("sendMessage", {})

    def test_rede(self):
        with self._raise(urllib.error.URLError("no route")):
            with self.assertRaises(n._Retry):
                self.t._post("sendMessage", {})

    def test_grupo_migrado(self):
        body = {"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -100987}}
        err = urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(json.dumps(body).encode()))
        with self._raise(err):
            with self.assertRaises(n._Fatal) as cm:
                self.t._post("sendMessage", {})
        self.assertIn("-100987", str(cm.exception))

    def test_403(self):
        with self._raise(http_error(403, "Forbidden: bot was blocked by the user")):
            with self.assertRaises(n._Fatal):
                self.t._post("sendMessage", {})


class TestComandoStop(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(n, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.stopped = 0
        self.t = make(stop_callback=self._stop, stop_password="segredo")
        self.t.stop_callback = self._stop
        self.sent = []
        self.t.send = lambda msg, specific_chat_id=None, critical=False: self.sent.append(msg)
        self.deleted = []
        self.t._delete_message = lambda c, m: self.deleted.append(m)

    def _stop(self):
        self.stopped += 1

    def msg(self, text, chat="111", frm=None, mid=1):
        self.t._handle_update({"text": text, "message_id": mid, "chat": {"id": int(chat)},
                               "from": {"id": int(frm or chat)}})

    def test_senha_correta(self):
        self.msg("/stop")
        self.msg("segredo", mid=42)
        self.assertEqual(self.stopped, 1)
        self.assertEqual(self.deleted, [42])           # senha apagada do chat
        self.assertIn("DESLIGAMENTO EXECUTADO", self.sent[-1])

    def test_bloqueio_apos_tentativas(self):
        for _ in range(n.STOP_MAX_FAILS):
            self.msg("/stop")
            self.msg("errada")
        self.assertIn("bloqueado", self.sent[-1])
        self.msg("/stop")
        self.assertIn("bloqueado por mais", self.sent[-1])
        self.msg("segredo")                            # não é tratado como senha
        self.assertEqual(self.stopped, 0)
        self.clock.t += n.STOP_LOCKOUT_S + 1
        self.msg("/stop")
        self.msg("segredo")
        self.assertEqual(self.stopped, 1)

    def test_tempo_esgotado(self):
        self.msg("/stop")
        self.clock.t += n.STOP_CONFIRM_S + 1
        self.msg("segredo")
        self.assertEqual(self.stopped, 0)
        self.assertIn("Tempo esgotado", self.sent[-1])

    def test_chat_nao_autorizado(self):
        self.msg("/stop", chat="999")
        self.assertEqual(self.sent, [])

    def test_membro_de_grupo_nao_autorizado(self):
        self.t.chat_ids = ["-100", "111"]
        self.msg("/stop", chat="-100", frm="555")      # grupo autorizado, pessoa não
        self.assertEqual(self.sent, [])
        self.msg("/stop", chat="-100", frm="111")      # pessoa autorizada no grupo
        self.assertEqual(len(self.sent), 1)

    def test_grupo_com_admins_separados(self):
        """Mensagens vão para o grupo; comandos só de usuários em admin_ids."""
        self.t.chat_ids = ["-1001"]
        self.t._admin_ids = ["111"]
        self.msg("/stop", chat="-1001", frm="999")     # membro qualquer
        self.assertEqual(self.sent, [])
        self.msg("/stop", chat="-1001", frm="111")     # admin
        self.msg("segredo", chat="-1001", frm="111")
        self.assertEqual(self.stopped, 1)

    def test_senha_so_de_quem_pediu(self):
        self.t.chat_ids = ["-1001"]
        self.t._admin_ids = ["111", "222"]
        self.msg("/stop", chat="-1001", frm="111")
        self.msg("segredo", chat="-1001", frm="222")   # outro admin não confirma o pedido do primeiro
        self.assertEqual(self.stopped, 0)
        self.msg("segredo", chat="-1001", frm="111")
        self.assertEqual(self.stopped, 1)

    def test_novo_comando_cancela_espera_da_senha(self):
        self.msg("/stop")
        self.msg("/status")
        self.msg("segredo")
        self.assertEqual(self.stopped, 0)

    def test_chat_desconhecido_logado_uma_vez(self):
        with mock.patch("builtins.print") as p:
            self.msg("/status", chat="-100555")
            self.msg("oi", chat="-100555")
        logs = [c.args[0] for c in p.call_args_list if "-100555" in c.args[0]]
        self.assertEqual(len(logs), 1)
        self.assertIn("KILN_TELEGRAM_CHAT_IDS", logs[0])

    def test_sem_senha_stop_desativado(self):
        t = TelegramNotifier("", [], stop_callback=self._stop)
        self.assertIsNone(t.stop_callback)


if __name__ == "__main__":
    unittest.main()
