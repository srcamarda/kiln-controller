"""
curves_manager.py — Carrega, salva e valida curvas de queima.

Robustez:
  - Gravação atômica (arquivo temporário + fsync + rename): uma queda de
    energia durante o salvamento nunca deixa curves.json pela metade.
  - Antes de substituir, a versão anterior vira curves.json.bak.
  - Na carga, curvas inválidas são ignoradas (com aviso) em vez de impedir o
    boot; se o arquivo estiver corrompido, usa o .bak.
  - Limites de temperatura/taxa/patamar centralizados aqui e usados também
    pela interface de edição.
"""

import json
import os
import re
import shutil

CURVES_PATH = os.environ.get("KILN_CURVES_FILE") or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "curves.json")

# Limites das curvas (a interface de edição usa os mesmos valores)
RATE_MIN     = 10      # °C/h
RATE_MAX     = 300     # °C/h
TARGET_MIN   = 100     # °C
TARGET_MAX   = 1270    # °C — mantenha folga para controller.OVERTEMP_ABS
SOAK_MIN     = 1       # min
SOAK_MAX     = 240     # min
MAX_SEGMENTS = 12
NAME_MAX_LEN = 30


class CurveFileError(Exception):
    """Arquivo de curvas ilegível ou com formato inesperado."""


# ── Validação ─────────────────────────────────

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_curve(curve) -> str:
    """Retorna mensagem de erro (curta, para o display) ou '' se válida."""
    if not isinstance(curve, dict):
        return "Formato inválido"
    name = curve.get("name")
    if not isinstance(name, str) or not name.strip():
        return "Curva sem nome"
    if len(name) > NAME_MAX_LEN:
        return f"Nome com mais de {NAME_MAX_LEN} letras"
    if not isinstance(curve.get("id"), str) or not curve["id"]:
        return "Curva sem identificador"

    segs = curve.get("segments")
    if not isinstance(segs, list) or not segs:
        return "Curva sem segmentos"
    if len(segs) > MAX_SEGMENTS:
        return f"Mais de {MAX_SEGMENTS} segmentos"

    prev_temp = 0
    for i, seg in enumerate(segs, 1):
        if not isinstance(seg, dict):
            return f"Seg {i}: formato inválido"
        rate, target = seg.get("rate_per_hour"), seg.get("target_temp")
        if not _is_number(rate):
            return f"Seg {i}: taxa ausente"
        if not _is_number(target):
            return f"Seg {i}: temperatura ausente"
        if not (RATE_MIN <= rate <= RATE_MAX):
            return f"Seg {i}: taxa fora de {RATE_MIN}-{RATE_MAX}°C/h"
        if not (TARGET_MIN <= target <= TARGET_MAX):
            return f"Seg {i}: temp. fora de {TARGET_MIN}-{TARGET_MAX}°C"
        if target <= prev_temp:
            return f"Seg {i}: temp. deve ser crescente"
        prev_temp = target

    soak = curve.get("soak_minutes")
    if not _is_number(soak):
        return "Patamar ausente"
    if not (SOAK_MIN <= soak <= SOAK_MAX):
        return f"Patamar fora de {SOAK_MIN}-{SOAK_MAX} min"
    return ""


def validate_curves(curves):
    """
    Filtra uma lista carregada do disco. Retorna (válidas, avisos).
    Curvas inválidas são descartadas; ids repetidos ou ausentes são corrigidos.
    """
    if not isinstance(curves, list):
        raise CurveFileError("o arquivo não contém uma lista de curvas")
    valid, warnings, seen = [], [], set()
    for idx, curve in enumerate(curves, 1):
        if isinstance(curve, dict) and not curve.get("id"):
            curve = dict(curve, id=_slug_id(curve.get("name", ""), seen))
        err = validate_curve(curve)
        label = curve.get("name") if isinstance(curve, dict) else None
        if err:
            warnings.append(f"Curva {label or f'#{idx}'} ignorada: {err}")
            continue
        if curve["id"] in seen:
            new_id = _slug_id(curve["name"], seen)
            warnings.append(f"Curva {curve['name']}: id repetido '{curve['id']}' → '{new_id}'")
            curve = dict(curve, id=new_id)
        seen.add(curve["id"])
        valid.append(curve)
    return valid, warnings


# ── Carga e gravação ──────────────────────────

def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise CurveFileError("arquivo não encontrado")
    except (OSError, ValueError) as e:
        raise CurveFileError(f"arquivo ilegível ({e})")


def load_curves(path: str = None):
    """
    Retorna (curvas, avisos). Nunca levanta exceção: no pior caso devolve
    lista vazia com o motivo nos avisos (a interface trata lista vazia).
    """
    path = path or CURVES_PATH
    warnings = []
    for candidate, label in ((path, "curves.json"), (path + ".bak", "backup")):
        try:
            curves, w = validate_curves(_read(candidate))
        except CurveFileError as e:
            warnings.append(f"{label}: {e}")
            continue
        if candidate != path:
            warnings.append("Curvas carregadas do backup (curves.json.bak)")
        return curves, warnings + w
    warnings.append("Nenhuma curva disponível")
    return [], warnings


def save_curves(curves: list, path: str = None):
    """
    Valida e grava de forma atômica. A versão anterior fica em .bak.
    Levanta ValueError (curva inválida) ou OSError (falha de disco).
    """
    path = path or CURVES_PATH
    ids = set()
    for c in curves:
        err = validate_curve(c)
        if err:
            raise ValueError(f"{c.get('name', '?')}: {err}")
        if c["id"] in ids:
            raise ValueError(f"id repetido: {c['id']}")
        ids.add(c["id"])

    directory = os.path.dirname(os.path.abspath(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(curves, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    # Guarda a versão atual (se for válida) como backup antes de substituir
    if os.path.exists(path):
        try:
            validate_curves(_read(path))
            shutil.copy2(path, path + ".bak")
            _fsync_file(path + ".bak")
        except CurveFileError:
            pass   # arquivo atual corrompido: não sobrescreve um backup bom

    os.replace(tmp, path)
    _fsync_dir(directory)


def _fsync_file(p):
    try:
        with open(p, "rb+") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def _fsync_dir(d):
    if os.name != "posix":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ── Criação ───────────────────────────────────

def _slug_id(name: str, taken) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "curva").lower()).strip("_") or "curva"
    cand, n = base, 2
    while cand in taken:
        cand, n = f"{base}_{n}", n + 1
    return cand


def next_curve_name(curves: list) -> str:
    """Gera nome automático: Curva 3, Curva 4... (sem repetir nomes existentes)."""
    names = {c.get("name", "") for c in curves}
    indices = []
    for name in names:
        if name.startswith("Curva "):
            try:
                indices.append(int(name.split(" ")[1]))
            except (IndexError, ValueError):
                pass
    n = max(indices) + 1 if indices else len(curves) + 1
    while f"Curva {n}" in names:
        n += 1
    return f"Curva {n}"


def new_curve_template(name: str, curves: list = ()) -> dict:
    """Nova curva com id único em relação às curvas existentes."""
    return {
        "id": _slug_id(name, {c.get("id") for c in curves}),
        "name": name,
        "segments": [],
        "soak_minutes": 30,
    }
