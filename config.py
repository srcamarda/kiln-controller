"""
config.py — Configuração sensível (Telegram) via variáveis de ambiente.

Ordem de precedência:
  1. Variáveis de ambiente já definidas (ex.: systemd Environment=/EnvironmentFile=)
  2. Arquivo .env ao lado do main.py (não versionado — ver .env.example)

Variáveis:
  KILN_TELEGRAM_TOKEN     token do bot (BotFather)
  KILN_TELEGRAM_CHAT_IDS  ids autorizados, separados por vírgula
  KILN_STOP_PASSWORD      senha de confirmação do /stop
"""

import os
import stat

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env_file(path: str = ENV_FILE, environ=None) -> bool:
    """
    Carrega KEY=VALUE de `path` sem sobrescrever o que já está no ambiente.
    Aceita linhas em branco, comentários (#), prefixo `export` e aspas.
    Retorna True se o arquivo existia.
    """
    environ = os.environ if environ is None else environ
    if not os.path.isfile(path):
        return False

    _warn_if_permissive(path)

    with open(path, encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                print(f"\r[config] {os.path.basename(path)}:{lineno} ignorada (sem '=').")
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in environ:
                environ[key] = value
    return True


def _warn_if_permissive(path: str):
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"\r[config] AVISO: {path} legível por outros usuários. "
              f"Rode: chmod 600 {path}")


def telegram_config(environ=None):
    """Retorna (token, chat_ids, stop_password). Ausentes → vazio/None."""
    environ = os.environ if environ is None else environ
    token    = environ.get("KILN_TELEGRAM_TOKEN", "").strip()
    chat_ids = [c.strip() for c in environ.get("KILN_TELEGRAM_CHAT_IDS", "").split(",")
                if c.strip()]
    password = environ.get("KILN_STOP_PASSWORD", "").strip() or None

    if not token or not chat_ids:
        print("\r[config] KILN_TELEGRAM_TOKEN/KILN_TELEGRAM_CHAT_IDS ausentes — "
              "Telegram desativado.")
    elif password is None:
        print("\r[config] KILN_STOP_PASSWORD ausente — /stop remoto desativado.")
    return token, chat_ids, password
