"""
display.py — Renderização no display 320x240 via framebuffer (fb1).

Telas implementadas:
  - IDLE       : temperatura atual + horário
  - PREHEAT    : modo pré-aquecimento
  - SELECTING  : lista de curvas para escolha
  - CONFIRMING : confirmação antes de iniciar
  - RUNNING    : temperatura, setpoint, progresso, bancos, tempo
  - SOAKING    : patamar com contagem regressiva
  - FINISHED   : resumo da queima
  - ERROR      : falha no termopar
"""

import struct
import os
from PIL import Image, ImageDraw, ImageFont
from controller import ControllerMode

# ──────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────

FB_DEV   = "/dev/fb1"
W, H     = 320, 240

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT     = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_B   = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_M   = os.path.join(FONT_DIR, "DejaVuSansMono-Bold.ttf")

# Paleta de cores
C_BG       = (10,  10,  10)   # fundo quase preto
C_TEXT     = (220, 220, 220)  # texto principal
C_DIM      = (100, 100, 100)  # texto secundário
C_ORANGE   = (255, 140,   0)  # temperatura / destaque
C_RED      = (220,  40,  40)  # erro / alerta
C_GREEN    = (60,  200,  80)  # ON / ok
C_BLUE     = (80,  140, 220)  # setpoint / info
C_BAR_BG   = (40,   40,  40)  # fundo da barra de progresso
C_BAR_FG   = (255, 140,   0)  # preenchimento da barra
C_SOAK     = (180,  80, 220)  # cor do soak
C_BORDER   = (60,   60,  60)  # bordas sutis


# ──────────────────────────────────────────────
# Cache de fontes
# ──────────────────────────────────────────────

_font_cache = {}

def _font(size, bold=False, mono=False):
    key = (size, bold, mono)
    if key not in _font_cache:
        path = FONT_M if mono else (FONT_B if bold else FONT)
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


# ──────────────────────────────────────────────
# Utilitários de desenho
# ──────────────────────────────────────────────

def _text_center(draw, y, text, font, color):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, y), text, font=font, fill=color)

def _progress_bar(draw, x, y, w, h, pct, color=C_BAR_FG):
    draw.rectangle([x, y, x + w, y + h], fill=C_BAR_BG)
    fill_w = int(w * max(0.0, min(1.0, pct)))
    if fill_w > 0:
        draw.rectangle([x, y, x + fill_w, y + h], fill=color)
    # borda
    draw.rectangle([x, y, x + w, y + h], outline=C_BORDER)

def _bank_indicator(draw, x, y, label, active):
    color = C_GREEN if active else C_DIM
    dot   = "●" if active else "○"
    f     = _font(16, bold=active)
    draw.text((x, y), f"{dot} {label}", font=f, fill=color)

def _divider(draw, y):
    draw.line([(10, y), (W - 10, y)], fill=C_BORDER, width=1)

def _fmt_time(seconds):
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


# ──────────────────────────────────────────────
# Renderizadores por tela
# ──────────────────────────────────────────────

def _render_idle(draw, temp, rtc_time):
    """Tela inicial"""
    # Horário no topo
    _text_center(draw, 10, rtc_time, _font(28, bold=True, mono=True), C_TEXT)
    _divider(draw, 50)

    # Temperatura
    temp_str = f"{temp:.1f}°C"
    _text_center(draw, 76, temp_str, _font(64, bold=True), C_ORANGE)
    _divider(draw, 170)

    # Instrução
    _text_center(draw, 185, "FORNO INATIVO", _font(18), C_DIM)
    _text_center(draw, 210, "Click → Menu", _font(20, bold=True), C_TEXT)


def _render_menu(draw, options, selected):
    """Menu principal"""
    _text_center(draw, 8, "MENU", _font(20, bold=True), C_TEXT)
    _divider(draw, 35)

    for i, label in enumerate(options):
        y = 50 + i * 40
        if i == selected:
            draw.rectangle([8, y - 4, W - 8, y + 28], fill=(40, 40, 80))
            draw.rectangle([8, y - 4, W - 8, y + 28], outline=C_BLUE)
            color = C_TEXT
        else:
            color = C_DIM
        _text_center(draw, y, label, _font(20, bold=(i == selected)), color)


def _render_selecting(draw, curves, selected):
    """Lista de curvas para seleção"""
    _text_center(draw, 8, "SELECIONAR CURVA", _font(18, bold=True), C_TEXT)
    _divider(draw, 35)

    visible = 3  # 3 curvas por vez para dar mais respiro
    start   = max(0, selected - visible + 1)
    items   = curves[start:start + visible]

    for i, curve in enumerate(items):
        idx = start + i
        y   = 50 + i * 62
        if idx == selected:
            draw.rectangle([6, y - 4, W - 6, y + 50], fill=(40, 40, 80))
            draw.rectangle([6, y - 4, W - 6, y + 50], outline=C_BLUE)
            name_color = C_TEXT
            info_color = C_BLUE
        else:
            name_color = C_DIM
            info_color = (80, 80, 100)

        draw.text((14, y), curve["name"], font=_font(19, bold=(idx == selected)),
                  fill=name_color)
        segs  = len(curve["segments"])
        t_max = curve["segments"][-1]["target_temp"]
        draw.text((14, y + 26), f"{segs} subidas - {t_max}°C - Patamar {curve['soak_minutes']}min",
                  font=_font(15), fill=info_color)


def _render_confirming(draw, curve_name):
    """Tela de confirmação"""
    _text_center(draw, 8, "INICIAR QUEIMA?", _font(18, bold=True), C_TEXT)
    _divider(draw, 35)

    _text_center(draw, 70, curve_name, _font(22, bold=True), C_ORANGE)
    _divider(draw, 108)
    _text_center(draw, 185, "Click   →   Confirmar", _font(18), C_GREEN)
    _text_center(draw, 210, "Longo   →   Cancelar",  _font(18), C_DIM)


def _render_preheat(draw, temp, target, bank_a):
    """Tela de pré-aquecimento"""
    _text_center(draw, 8, "PRÉ-AQUECIMENTO", _font(18, bold=True), C_TEXT)
    _divider(draw, 35)

    # Temperatura atual
    draw.text((12, 48), "Atual", font=_font(14), fill=C_DIM)
    _text_center(draw, 58, f"{temp:.1f}°C", _font(44, bold=True), C_ORANGE)

    _divider(draw, 118)

    # Temperatura alvo
    draw.text((12, 130), "Alvo", font=_font(14), fill=C_DIM)
    _text_center(draw, 140, f"{target:.0f}°C", _font(40, bold=True), C_BLUE)

    _divider(draw, 195)

    # Status banco
    _bank_indicator(draw, 12, 210, "Banco A", bank_a)
    draw.text((160, 210), "Banco B  ○ desligado", font=_font(14), fill=C_DIM)


def _render_running(draw, state, curve, elapsed_s, progress, measured_temp):
    """Tela principal durante queima"""
    # Cabeçalho: nome da curva + segmento
    seg_label = f"Seg {state.segment_index + 1}/{len(curve['segments'])}"
    draw.text((8, 6), curve["name"], font=_font(16, bold=True), fill=C_TEXT)
    
    bbox = draw.textbbox((0, 0), seg_label, font=_font(14))
    draw.text((W - bbox[2] - 8, 8), seg_label, font=_font(14), fill=C_DIM)

    _divider(draw, 28)

    # Barra de progresso
    _progress_bar(draw, 8, 34, W - 16, 12, progress)

    # Temperatura atual
    temp_str = f"{state.setpoint:.0f}°C"
    draw.text((8, 52), "Alvo", font=_font(13), fill=C_DIM)
    draw.text((50, 48), temp_str, font=_font(22, bold=True), fill=C_BLUE)

    # Temperatura real
    _text_center(draw, 72, f"{measured_temp:.1f}°C", _font(44, bold=True), C_ORANGE)    

    _divider(draw, 126)

    # Informações
    rate_str = f"Taxa: {state.current_rate:+.0f}°C/h"
    seg = curve["segments"][state.segment_index]
    target_rate_str = f"(alvo {seg['rate_per_hour']}°C/h)"
    draw.text((8, 132), rate_str, font=_font(14), fill=C_TEXT)
    draw.text((8, 150), target_rate_str, font=_font(12), fill=C_DIM)

    # Tempo decorrido
    draw.text((W - 120, 132), _fmt_time(elapsed_s),
              font=_font(20, bold=True, mono=True), fill=C_TEXT)

    _divider(draw, 172)

    # Bancos
    _bank_indicator(draw, 12, 210, "Banco A", state.bank_a)
    if state.bank_b_suppressed:
        draw.text((165, 210), "● B desligado", font=_font(14), fill=C_RED)
    else:
        _bank_indicator(draw, 165, 210, "Banco B", state.bank_b)    


def _render_soaking(draw, state, elapsed_s, remaining_s, measured_temp):
    """Tela durante o patamar"""
    _text_center(draw, 6, "PATAMAR", _font(18, bold=True), C_SOAK)
    _divider(draw, 30)

    # Labels acima da temperatura
    draw.text((8, 34), "Atual", font=_font(13), fill=C_DIM)
    bbox = draw.textbbox((0, 0), f"Alvo {state.setpoint:.0f}°C", font=_font(13))
    draw.text((W - bbox[2] - 8, 34), f"Alvo {state.setpoint:.0f}°C",
              font=_font(13), fill=C_BLUE)

    # Temperatura grande
    _text_center(draw, 50, f"{measured_temp:.1f}°C", _font(44, bold=True), C_ORANGE)

    _divider(draw, 104)

    # Contagem regressiva
    draw.text((8, 112), "Tempo restante", font=_font(14), fill=C_DIM)
    _text_center(draw, 126, _fmt_time(remaining_s), _font(36, bold=True, mono=True), C_SOAK)

    # Barra de progresso do patamar
    total_patamar = state.curve["soak_minutes"] * 60
    soak_pct      = 1.0 - (remaining_s / total_patamar) if total_patamar > 0 else 1.0
    _progress_bar(draw, 8, 162, W - 16, 12, soak_pct, color=C_SOAK)

    _divider(draw, 182)

    # Decorrido + bancos
    draw.text((8, 190), f"Total: {_fmt_time(elapsed_s)}",
              font=_font(13), fill=C_DIM)
    _bank_indicator(draw, 165, 188, "A", state.bank_a)
    _bank_indicator(draw, 220, 188, "B", state.bank_b)


def _render_finished(draw, elapsed_s, max_temp, start_time_str):
    """Tela de resumo após conclusão"""
    _text_center(draw, 12, "QUEIMA CONCLUÍDA", _font(20, bold=True), C_GREEN)
    _divider(draw, 42)
    draw.text((16, 55),  "Duração:",      font=_font(16), fill=C_DIM)
    draw.text((150, 55), _fmt_time(elapsed_s), font=_font(18, bold=True, mono=True), fill=C_TEXT)
    draw.text((16, 85),  "Temp. máx:",    font=_font(16), fill=C_DIM)
    draw.text((150, 85), f"{max_temp:.0f}°C", font=_font(18, bold=True), fill=C_ORANGE)
    draw.text((16, 115), "Início:",       font=_font(16), fill=C_DIM)
    draw.text((150, 115), start_time_str, font=_font(16), fill=C_TEXT)
    _divider(draw, 148)
    _text_center(draw, 162, "Click → Menu", _font(18, bold=True), C_TEXT)


def _render_error(draw, message="Falha no termopar"):
    """Tela de erro crítico"""
    _text_center(draw, 30,  "⚠  ERRO  ⚠",    _font(24, bold=True), C_RED)
    _divider(draw, 68)
    _text_center(draw, 85,  message,           _font(18), C_TEXT)
    _text_center(draw, 118, "Relés desligados", _font(16), C_DIM)
    _divider(draw, 148)
    _text_center(draw, 162, "Click → Menu",     _font(18, bold=True), C_TEXT)


# ──────────────────────────────────────────────
# Classe principal
# ──────────────────────────────────────────────

class Display:

    def __init__(self, fb_dev=FB_DEV):
        self._fb_dev  = fb_dev
        self._fb      = None
        self._last_rendered = None  # evita reescrever frame idêntico

        # Pré-carrega fontes comuns para evitar lag no primeiro frame
        for size in [13, 14, 16, 17, 18, 20, 22, 24, 28, 36, 40, 44, 46, 52]:
            _font(size)
            _font(size, bold=True)

    def open(self):
        self._fb = open(self._fb_dev, "wb")

    def close(self):
        if self._fb:
            self._fb.close()
            self._fb = None

    def _write(self, img: Image.Image):
        """Converte RGB → RGB565 e escreve no framebuffer."""
        buf = bytearray(W * H * 2)
        idx = 0
        for r, g, b in img.getdata():
            pixel = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = pixel & 0xFF   # little-endian
            buf[idx + 1] = pixel >> 8
            idx += 2
        self._fb.seek(0)
        self._fb.write(buf)
        self._fb.flush()

    def _new_frame(self):
        img  = Image.new("RGB", (W, H), C_BG)
        draw = ImageDraw.Draw(img)
        return img, draw

    # ── API pública ────────────────────────────

    def show_idle(self, temp: float, rtc_time: str):
        img, draw = self._new_frame()
        _render_idle(draw, temp, rtc_time)
        self._write(img)

    def show_menu(self, options: list, selected: int):
        img, draw = self._new_frame()
        _render_menu(draw, options, selected)
        self._write(img)

    def show_selecting(self, curves: list, selected: int):
        img, draw = self._new_frame()
        _render_selecting(draw, curves, selected)
        self._write(img)

    def show_confirming(self, curve_name: str):
        img, draw = self._new_frame()
        _render_confirming(draw, curve_name)
        self._write(img)

    def show_preheat(self, temp: float, target: float, bank_a: bool):
        img, draw = self._new_frame()
        _render_preheat(draw, temp, target, bank_a)
        self._write(img)

    def show_running(self, state, curve: dict, elapsed_s: float,
                     progress: float, measured_temp: float):
        img, draw = self._new_frame()
        _render_running(draw, state, curve, elapsed_s, progress, measured_temp)
        self._write(img)

    def show_soaking(self, state, elapsed_s: float,
                     remaining_s: float, measured_temp: float):
        img, draw = self._new_frame()
        _render_soaking(draw, state, elapsed_s, remaining_s, measured_temp)
        self._write(img)

    def show_finished(self, elapsed_s: float, max_temp: float, start_time_str: str):
        img, draw = self._new_frame()
        _render_finished(draw, elapsed_s, max_temp, start_time_str)
        self._write(img)

    def show_error(self, message: str = "Falha no termopar"):
        img, draw = self._new_frame()
        _render_error(draw, message)
        self._write(img)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
