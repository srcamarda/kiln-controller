"""
tools/render_screens.py — Gera as imagens das telas do display para a
documentação, sem precisar do Pi nem do framebuffer (PC com Pillow e as
fontes DejaVu instaladas).

    python3 tools/render_screens.py            # grava docs/telas/telas.png
    python3 tools/render_screens.py --each     # também uma imagem por tela

Os valores mostrados são de exemplo; as curvas vêm do curves.json.
"""

import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw

import display as D
from controller import ControllerState, ControllerMode

OUT_DIR = os.path.join(ROOT, "docs", "telas")
COLS, PAD, CAPTION_H = 4, 10, 26


def render_all():
    """Retorna [(legenda, nome do arquivo, imagem)] na ordem de uso."""
    frames = []
    current = {}

    def capture(self, img):
        current["img"] = img.copy()

    D.Display._write = capture          # nada vai para o framebuffer
    d = D.Display()

    def shot(caption, name, fn, *a, **k):
        fn(*a, **k)
        frames.append((caption, name, current["img"]))

    with open(os.path.join(ROOT, "curves.json"), encoding="utf-8") as f:
        curves = json.load(f)
    curve = max(curves, key=lambda c: len(c["segments"]) if len(c["segments"]) <= 5 else 0)
    sel   = curves.index(curve)
    seg_i = min(2, len(curve["segments"]) - 1)
    seg   = curve["segments"][seg_i]

    shot("Tela inicial", "01-inicial", d.show_idle, temp=24.8, rtc_time="09:41:07")
    shot("Menu", "02-menu", d.show_menu,
         ["Curvas de Queima", "Pré-aquecimento", "Nova Curva", "Editar Curva"], selected=0)
    shot("Escolher a curva", "03-curvas", d.show_selecting, curves, selected=sel)
    shot("Confirmar o início", "04-confirmar", d.show_confirming, curve)

    s = ControllerState()
    s.mode, s.curve, s.segment_index = ControllerMode.RAMPING, curve, seg_i
    temp = float(seg["target_temp"]) - 12.5
    s.setpoint = temp + 3
    s.bank_a = True
    s.bank_b = temp >= 600          # abaixo de 600 °C só o banco A trabalha
    s.current_rate = s.display_rate = float(seg["rate_per_hour"]) + 2
    shot("Queima: rampa", "05-rampa", d.show_running, s, curve,
         elapsed_s=4 * 3600 + 32 * 60, progress=0.55, measured_temp=temp)

    final = float(curve["segments"][-1]["target_temp"])
    s2 = ControllerState()
    s2.mode, s2.curve = ControllerMode.SOAKING, curve
    s2.segment_index = len(curve["segments"]) - 1
    s2.setpoint, s2.bank_a = final, True
    shot("Queima: patamar", "06-patamar", d.show_soaking, s2,
         elapsed_s=8 * 3600 + 5 * 60, remaining_s=18 * 60, measured_temp=final - 1.6)

    shot("Pré-aquecimento", "07-preaquecimento", d.show_preheat,
         temp=187.0, target=350.0, bank_a=True)
    shot("Queima concluída", "08-concluida", d.show_finished,
         elapsed_s=8 * 3600 + 35 * 60, max_temp=final + 3, start_time_str="19:12")
    shot("Cancelar (toque longo)", "09-cancelar", d.show_cancel_confirm)
    shot("Erro de segurança", "10-erro", d.show_error, "Aquece desligado")

    segs = [{"rate_per_hour": 100, "target_temp": 600},
            {"rate_per_hour": 150, "target_temp": 1100}]
    shot("Nova curva: segmento", "11-segmento", d.show_curve_segment,
         seg_index=1, seg_total=3, field=1, rate=150, target=1100, is_new=True)
    shot("Nova curva: salvar", "12-salvar", d.show_curve_save, "Curva 6",
         segs + [{"rate_per_hour": 60, "target_temp": 1220}], soak_min=30)
    return frames


def contact_sheet(frames):
    w, h = D.W, D.H
    rows = (len(frames) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * w + (COLS + 1) * PAD,
                              rows * (h + CAPTION_H) + (rows + 1) * PAD), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    font = D._font(16)
    for i, (caption, _, img) in enumerate(frames):
        r, c = divmod(i, COLS)
        x = PAD + c * (w + PAD)
        y = PAD + r * (h + CAPTION_H + PAD)
        sheet.paste(img, (x, y))
        tw = draw.textlength(caption, font=font)
        draw.text((x + (w - tw) / 2, y + h + 4), caption, font=font, fill=(200, 200, 200))
    return sheet


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    frames = render_all()
    path = os.path.join(OUT_DIR, "telas.png")
    contact_sheet(frames).save(path, optimize=True)
    print(f"Gravado {os.path.relpath(path, ROOT)}")
    if "--each" in sys.argv:
        for _, name, img in frames:
            img.save(os.path.join(OUT_DIR, name + ".png"), optimize=True)
        print(f"Gravadas {len(frames)} telas em {os.path.relpath(OUT_DIR, ROOT)}/")


if __name__ == "__main__":
    main()
