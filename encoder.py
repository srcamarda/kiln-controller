"""
encoder.py — Leitura do encoder rotativo com botão via GPIO.

Pinagem padrão (ajustável via argumentos):
  CLK → GPIO17  (pino físico 11)
  DT  → GPIO27  (pino físico 13)
  SW  → GPIO22  (pino físico 15)

Eventos gerados:
  ROTATE_CW   — girou para direita (+1)
  ROTATE_CCW  — girou para esquerda (-1)
  CLICK       — pressionou e soltou (< LONG_PRESS_MS)
  LONG_PRESS  — manteve pressionado (>= LONG_PRESS_MS)
"""

import time
from enum import Enum, auto

# Tenta importar RPi.GPIO; se falhar, usa simulador para dev
try:
    import RPi.GPIO as GPIO
    _HW_AVAILABLE = True
except (ImportError, RuntimeError):
    _HW_AVAILABLE = False


LONG_PRESS_MS = 1000  # ms para considerar click longo


class EncoderEvent(Enum):
    ROTATE_CW  = auto()
    ROTATE_CCW = auto()
    CLICK      = auto()
    LONG_PRESS = auto()


# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareEncoder:

    def __init__(self, pin_clk=17, pin_dt=27, pin_sw=22):
        self._clk = pin_clk
        self._dt  = pin_dt
        self._sw  = pin_sw

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._clk, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._dt,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._sw,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self._last_clk    = GPIO.input(self._clk)
        self._sw_pressed  = False
        self._press_time  = 0.0
        self._events      = []

        # Interrupções
        GPIO.add_event_detect(self._clk, GPIO.BOTH,
                              callback=self._on_rotate, bouncetime=2)
        GPIO.add_event_detect(self._sw,  GPIO.BOTH,
                              callback=self._on_button, bouncetime=50)

    def _on_rotate(self, channel):
        clk_state = GPIO.input(self._clk)
        dt_state  = GPIO.input(self._dt)
        if clk_state != self._last_clk:
            if dt_state != clk_state:
                self._events.append(EncoderEvent.ROTATE_CW)
            else:
                self._events.append(EncoderEvent.ROTATE_CCW)
        self._last_clk = clk_state

    def _on_button(self, channel):
        state = GPIO.input(self._sw)
        if state == GPIO.LOW:
            # Pressionou
            self._sw_pressed = True
            self._press_time = time.monotonic()
        else:
            # Soltou
            if self._sw_pressed:
                held_ms = (time.monotonic() - self._press_time) * 1000
                if held_ms >= LONG_PRESS_MS:
                    self._events.append(EncoderEvent.LONG_PRESS)
                else:
                    self._events.append(EncoderEvent.CLICK)
            self._sw_pressed = False

    def poll(self) -> list:
        """Retorna e limpa a lista de eventos pendentes."""
        events = self._events[:]
        self._events.clear()
        return events

    def cleanup(self):
        GPIO.cleanup()


# ──────────────────────────────────────────────
# Simulador para desenvolvimento via SSH
# ──────────────────────────────────────────────

class SimulatedEncoder:
    """
    Gera eventos a partir de stdin em modo não-bloqueante.
    Teclas: d = CW, a = CCW, s = click, l = long press
    Útil para testar o main.py sem hardware físico.
    """

    def __init__(self):
        import sys, tty, termios, select
        self._sys      = sys
        self._tty      = tty
        self._termios  = termios
        self._select   = select
        self._fd       = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        print("\r[encoder sim] a=CCW  d=CW  s=click  l=long  q=quit\r")

    def poll(self) -> list:
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

def create_encoder(pin_clk=17, pin_dt=27, pin_sw=22):
    if _HW_AVAILABLE:
        print("[encoder] Usando hardware GPIO.")
        return HardwareEncoder(pin_clk, pin_dt, pin_sw)
    print("[encoder] GPIO não disponível — usando simulador de teclado.")
    return SimulatedEncoder()
