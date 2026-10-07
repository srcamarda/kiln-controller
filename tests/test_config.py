"""
test_config.py — Carregamento de segredos via ambiente/.env.
Rodar:  python3 -m unittest tests.test_config -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import config
from notifier import TelegramNotifier


class TestEnvFile(unittest.TestCase):

    def _write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o600)
        self.addCleanup(os.remove, path)
        return path

    def test_parse(self):
        path = self._write(
            "# comentário\n\n"
            "KILN_TELEGRAM_TOKEN=\"123:abc\"\n"
            "export KILN_TELEGRAM_CHAT_IDS=1, 2 ,\n"
            "KILN_STOP_PASSWORD='s3nh@=x'\n"
            "linha_invalida\n"
        )
        env = {}
        self.assertTrue(config.load_env_file(path, env))
        self.assertEqual(config.telegram_config(env), ("123:abc", ["1", "2"], "s3nh@=x"))

    def test_ambiente_tem_precedencia(self):
        path = self._write("KILN_TELEGRAM_TOKEN=do_arquivo\n")
        env = {"KILN_TELEGRAM_TOKEN": "do_ambiente"}
        config.load_env_file(path, env)
        self.assertEqual(env["KILN_TELEGRAM_TOKEN"], "do_ambiente")

    def test_arquivo_ausente(self):
        self.assertFalse(config.load_env_file("/nao/existe/.env", {}))

    def test_sem_variaveis(self):
        self.assertEqual(config.telegram_config({}), ("", [], None))


class TestNotifierSemSenha(unittest.TestCase):

    def test_stop_desativado_sem_senha(self):
        n = TelegramNotifier("", [], stop_callback=lambda: None, stop_password=None)
        self.assertIsNone(n.stop_callback)

    def test_sem_senha_padrao(self):
        n = TelegramNotifier("", [], stop_callback=lambda: None)
        self.assertIsNone(n.stop_password)


if __name__ == "__main__":
    unittest.main()
