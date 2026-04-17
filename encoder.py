"""
encoder.py — Leitura do encoder rotativo com botão via GPIO.

Pinagem padrão (ajustável via argumentos):
  SW  → GPIO5  (pino físico 29)
  DT  → GPIO6  (pino físico 31)
  CLK → GPIO12 (pino físico 32)

Eventos gerados:
  ROTATE_CW   — girou para direita (+1)
  ROTATE_CCW  — girou para esquerda (-1)
  CLICK       — pressionou e soltou (< LONG_PRESS_MS)
  LONG_PRESS  — manteve pressionado (>= LONG_PRESS_MS)
"""

import time
from enum import Enum, auto
import os

# GPIO via gpiozero (backend automático: lgpio, etc.)
try:
    from gpiozero import Button
    _GPIO_AVAILABLE = True
except Exception:
    _GPIO_AVAILABLE = False
    Button = None


def _hw_available():
    if not _GPIO_AVAILABLE:
        return False
    kiln_hw = os.environ.get("KILN_HW")
    if kiln_hw == "0":
        return False
    if kiln_hw == "1":
        return True
    return True


_HW_AVAILABLE = _hw_available()


LONG_PRESS_MS = 1000  # ms para considerar click longo


class EncoderEvent(Enum):
    ROTATE_CW  = auto()
    ROTATE_CCW = auto()
    CLICK      = auto()
    LONG_PRESS = auto()

_TRANSITIONS = {
    (0b00, 0b01): +1,
    (0b01, 0b11): +1,
    (0b11, 0b10): +1,
    (0b10, 0b00): +1,

    (0b00, 0b10): -1,
    (0b10, 0b11): -1,
    (0b11, 0b01): -1,
    (0b01, 0b00): -1,
}

# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareEncoder:

    def __init__(self, pin_clk, pin_dt, pin_sw):
        self._clk = Button(pin_clk, pull_up=True, bounce_time=None)
        self._dt  = Button(pin_dt,  pull_up=True, bounce_time=None)
        self._sw  = Button(pin_sw,  pull_up=True, bounce_time=0.05)

        self._last_state = (int(self._clk.value) << 1) | int(self._dt.value)
        self._direction = 0
        self._in_cycle = False
        self._last_event_time = 0

        self._sw_pressed = False
        self._press_time = 0.0

        self._events = []

        # Callbacks de rotação
        self._clk.when_pressed  = self._on_rotate
        self._clk.when_released = self._on_rotate
        self._dt.when_pressed   = self._on_rotate
        self._dt.when_released  = self._on_rotate

        # Botão
        self._sw.when_pressed   = self._on_button_press
        self._sw.when_released  = self._on_button_release

    def _read_state(self):
        clk = int(self._clk.value)
        dt  = int(self._dt.value)
        return (clk << 1) | dt

    def _on_rotate(self):
        state = self._read_state()
        last  = self._last_state

        move = _TRANSITIONS.get((last, state), 0)

        if move != 0:
            self._acc += move

            # 4 passos = 1 "clique" do encoder
            if self._acc >= 4:
                self._events.append(EncoderEvent.ROTATE_CW)
                self._acc = 0
            elif self._acc <= -4:
                self._events.append(EncoderEvent.ROTATE_CCW)
                self._acc = 0

        self._last_state = state

    def _on_button_press(self):
        self._sw_pressed = True
        self._press_time = time.monotonic()

    def _on_button_release(self):
        if self._sw_pressed:
            held_ms = (time.monotonic() - self._press_time) * 1000
            if held_ms >= LONG_PRESS_MS:
                self._events.append(EncoderEvent.LONG_PRESS)
            else:
                self._events.append(EncoderEvent.CLICK)
        self._sw_pressed = False

    def poll(self) -> list:
        events = self._events[:]
        self._events.clear()
        return events

    def cleanup(self):
        pass


# ──────────────────────────────────────────────
# Simulador
# ──────────────────────────────────────────────

class SimulatedEncoder:
    """
    Gera eventos a partir de stdin em modo não-bloqueante.
    Teclas: d = CW, a = CCW, s = click, l = long press
    """

    def __init__(self):
        import sys, tty, termios, select
        self._sys      = sys
        self._tty      = tty
        self._termios  = termios
        self._select   = select
        self._fd       = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        self._raw_active   = False

    def poll(self) -> list:
        if not self._raw_active:
            self._tty.setraw(self._fd)
            self._sys.stdout.write("\r\n[encoder sim] a=CCW  d=CW  s=click  l=long  q=quit\r\n")
            self._sys.stdout.flush()
            self._raw_active = True

        events = []

        if self._select.select([self._sys.stdin], [], [], 0)[0]:
            ch = self._sys.stdin.read(1)

            if ch == 'd':
                events.append(EncoderEvent.ROTATE_CW)
            elif ch == 'a':
                events.append(EncoderEvent.ROTATE_CCW)
            elif ch == 's':
                events.append(EncoderEvent.CLICK)
            elif ch == 'l':
                events.append(EncoderEvent.LONG_PRESS)
            elif ch == 'q':
                raise KeyboardInterrupt

        return events

    def cleanup(self):
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old_settings)


# ──────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────

def create_encoder(pin_clk=12, pin_dt=6, pin_sw=5):
    if _HW_AVAILABLE:
        print("\r[encoder] Usando hardware GPIO.")
        return HardwareEncoder(pin_clk, pin_dt, pin_sw)

    print("\r[encoder] GPIO não disponível — usando simulador de teclado.")
    return SimulatedEncoder()
