"""
sdnotify.py — Notificações para o systemd (Type=notify + WatchdogSec).

Sem dependências: escreve no socket $NOTIFY_SOCKET. Fora do systemd
(rodando o main.py à mão) as chamadas não fazem nada.
"""

import os
import socket
import time

_addr = os.environ.get("NOTIFY_SOCKET")
if _addr and _addr.startswith("@"):
    _addr = "\0" + _addr[1:]   # namespace abstrato

_sock = None
_last_ping = 0.0
PING_INTERVAL_S = 2.0


def notify(state: str) -> bool:
    global _sock
    if not _addr:
        return False
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _sock.sendto(state.encode("utf-8"), _addr)
        return True
    except OSError:
        return False


def ready():
    notify("READY=1")


def watchdog_ping():
    """Sinaliza que o loop principal está vivo (com limite de frequência)."""
    global _last_ping
    now = time.monotonic()
    if now - _last_ping >= PING_INTERVAL_S:
        _last_ping = now
        notify("WATCHDOG=1")


def status(text: str):
    notify(f"STATUS={text}")
