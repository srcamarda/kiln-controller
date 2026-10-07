"""
tools/plot_firing.py — Gráfico e resumo de uma queima registrada.

Uso (no PC, com o CSV copiado do Pi):
    python tools/plot_firing.py 20261010-080000_cone-6.csv
    python tools/plot_firing.py arquivo.csv --png saida.png

Copiar do Pi:  scp pi@<ip-do-pi>:/var/log/kiln-controller/*.csv* .
               (o .json com o mesmo nome traz curva, parâmetros e resumo)

Requer matplotlib (pip install matplotlib). Sem ele, imprime só o resumo.
"""

import argparse
import csv
import json
import os
from datetime import datetime


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    meta_path = os.path.splitext(path)[0] + ".json"
    meta = None
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    return rows, meta


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def print_summary(rows, meta):
    if meta:
        print(f"Queima: {meta.get('name')}  início {meta.get('started_at')}  "
              f"fim {meta.get('ended_at', '?')} ({meta.get('end_reason', 'sem fim registrado')})")
        for r in meta.get("resumes", []):
            print(f"  retomada em {r['at']} após {r['downtime_s'] / 60:.0f} min: {r['note']}")
        s = meta.get("summary") or meta.get("partial_summary") or {}
        if s:
            print(f"Temp. máx: {s.get('max_temp')} °C | acima do setpoint (máx): "
                  f"{s.get('max_over_setpoint')} °C | comutações A/B: "
                  f"{s.get('switches_a')}/{s.get('switches_b')}")
            if s.get("soak_max_over") is not None:
                print(f"Patamar: de {s['soak_min_under']:+.1f} a {s['soak_max_over']:+.1f} °C do alvo")
            curve = (meta.get("curve") or {}).get("segments", [])
            for k, seg in sorted(s.get("segments", {}).items(), key=lambda x: int(x[0])):
                prog = curve[int(k) - 1]["rate_per_hour"] if int(k) <= len(curve) else "?"
                print(f"  Segmento {k}: {seg.get('start_temp')} → {seg.get('end_temp')} °C em "
                      f"{seg.get('duration_min', '?')} min — taxa real "
                      f"{seg.get('actual_rate_c_h', '?')} °C/h (programada {prog})")
    events = [(r["timestamp"], r["event"]) for r in rows if r.get("event")]
    if events:
        print("Eventos:")
        for ts, ev in events:
            print(f"  {ts}  {ev}")


def plot(rows, meta, png=None):
    import matplotlib.pyplot as plt

    t   = [datetime.fromisoformat(r["timestamp"]) for r in rows]
    tmp = [num(r["temp_c"]) for r in rows]
    sp  = [num(r["setpoint_c"]) for r in rows]
    a   = [num(r["bank_a"]) or 0 for r in rows]
    b   = [num(r["bank_b"]) or 0 for r in rows]
    rate = [num(r["rate_display_c_h"]) for r in rows]

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, sharex=True, figsize=(12, 8),
                                        gridspec_kw={"height_ratios": [3, 1, 1]})
    ax1.plot(t, tmp, label="Temperatura", lw=1.2)
    ax1.plot(t, sp, label="Setpoint", lw=1, ls="--")
    for r, ti in zip(rows, t):
        if r.get("event"):
            ax1.axvline(ti, color="gray", lw=0.5, alpha=0.5)
    ax1.set_ylabel("°C")
    ax1.legend(loc="upper left")
    ax1.set_title((meta or {}).get("name", "Queima"))

    ax2.step(t, a, where="post", label="Banco A")
    ax2.step(t, [x * 0.9 for x in b], where="post", label="Banco B")
    ax2.set_yticks([0, 1])
    ax2.legend(loc="upper left")

    ax3.plot(t, rate, lw=1)
    ax3.set_ylabel("°C/h")
    fig.autofmt_xdate()
    fig.tight_layout()
    if png:
        fig.savefig(png, dpi=120)
        print(f"Gráfico salvo em {png}")
    else:
        plt.show()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--png", help="salva o gráfico em PNG em vez de abrir a janela")
    args = ap.parse_args()
    rows, meta = load(args.csv)
    print_summary(rows, meta)
    try:
        plot(rows, meta, args.png)
    except ImportError:
        print("\n(matplotlib não instalado — só o resumo foi mostrado: pip install matplotlib)")


if __name__ == "__main__":
    main()
