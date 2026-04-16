"""
curves_manager.py — Carrega, salva e valida curvas de queima.
"""

import json
import os

CURVES_PATH = os.path.join(os.path.dirname(__file__), "curves.json")


def load_curves() -> list:
    with open(CURVES_PATH, "r") as f:
        return json.load(f)


def save_curves(curves: list):
    with open(CURVES_PATH, "w") as f:
        json.dump(curves, f, indent=2, ensure_ascii=False)


def next_curve_name(curves: list) -> str:
    """Gera nome automático: Curva 3, Curva 4..."""
    indices = []
    for c in curves:
        name = c.get("name", "")
        if name.startswith("Curva "):
            try:
                indices.append(int(name.split(" ")[1]))
            except (IndexError, ValueError):
                pass
    n = max(indices) + 1 if indices else len(curves) + 1
    return f"Curva {n}"


def new_curve_template(name: str) -> dict:
    return {
        "id":           name.lower().replace(" ", "_"),
        "name":         name,
        "segments":     [],
        "soak_minutes": 30,
    }


def validate_curve(curve: dict) -> str:
    """Retorna mensagem de erro ou '' se válido."""
    if not curve.get("segments"):
        return "Curva sem segmentos"
    prev_temp = 0
    for i, seg in enumerate(curve["segments"]):
        if seg["rate_per_hour"] < 10:
            return f"Seg {i+1}: taxa muito baixa"
        if seg["target_temp"] <= prev_temp:
            return f"Seg {i+1}: temp deve ser crescente"
        prev_temp = seg["target_temp"]
    if curve["soak_minutes"] < 1:
        return "Patamar deve ser >= 1 min"
    return ""
