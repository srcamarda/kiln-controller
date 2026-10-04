"""
firing_state.py — Persistência da queima em andamento para retomar após
queda de energia, travamento do Python ou reinício pelo watchdog.

O arquivo é gravado de forma atômica (tmp + fsync + rename) e removido
quando a queima termina, é cancelada pelo operador ou entra em ERRO —
só uma interrupção "inesperada" deixa o arquivo para trás.

Local: $STATE_DIRECTORY (definido pelo systemd via StateDirectory=) ou
./state ao lado do main.py.
"""

import json
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.environ.get("STATE_DIRECTORY") or os.path.join(_HERE, "state")
STATE_FILE = os.path.join(STATE_DIR, "firing.json")

SCHEMA_VERSION = 1

_lock = threading.Lock()


def save(snapshot: dict, path: str = None):
    path = path or STATE_FILE
    data = dict(snapshot, version=SCHEMA_VERSION)
    tmp  = path + ".tmp"
    with _lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path))


def load(path: str = None):
    """Retorna o snapshot salvo, ou None se não houver/for inválido."""
    path = path or STATE_FILE
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            print(f"\r[state] Arquivo de queima ilegível ({e}) — ignorado.")
            return None
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        print("\r[state] Versão de arquivo de queima desconhecida — ignorado.")
        return None
    return data


def clear(path: str = None):
    path = path or STATE_FILE
    with _lock:
        for p in (path, path + ".tmp"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"\r[state] Não foi possível remover {p}: {e}")
        _fsync_dir(os.path.dirname(path))


def _fsync_dir(d):
    if os.name != "posix":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
