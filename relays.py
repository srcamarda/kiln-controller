"""
relays.py — Controle dos relés dos bancos A e B via GPIO.

Pinagem padrão:
  Banco A → GPIO23  (pino físico 16)
  Banco B → GPIO24  (pino físico 18)

O módulo HL-52S é ativo em LOW (LOW = relé fechado).
A lógica é invertida aqui para que True = banco ligado.
Confirme o comportamento do seu módulo no primeiro teste.
"""

try:
    import RPi.GPIO as GPIO
    _GPIO_IMPORTABLE = True
except (ImportError, RuntimeError):
    _GPIO_IMPORTABLE = False

def _hw_available():
    import os
    if not _GPIO_IMPORTABLE:
        return False
    if os.environ.get("KILN_HW") == "1":
        return True
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    return True

_HW_AVAILABLE = _hw_available()


# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareRelays:

    # HL-52S: LOW ativa o relé
    ACTIVE_LOW = True

    def __init__(self, pin_a=23, pin_b=24):
        self._pin_a = pin_a
        self._pin_b = pin_b
        self._state_a = False
        self._state_b = False

        # Garante que GPIO já foi inicializado (o encoder pode ter feito isso)
        try:
            GPIO.setmode(GPIO.BCM)
        except Exception:
            pass

        GPIO.setup(self._pin_a, GPIO.OUT)
        GPIO.setup(self._pin_b, GPIO.OUT)

        # Inicia com tudo desligado
        self.set(False, False)

    def _level(self, on: bool) -> int:
        """Converte estado lógico (True=ligado) para nível GPIO."""
        if self.ACTIVE_LOW:
            return GPIO.LOW if on else GPIO.HIGH
        return GPIO.HIGH if on else GPIO.LOW

    def set(self, bank_a: bool, bank_b: bool):
        if bank_a != self._state_a:
            GPIO.output(self._pin_a, self._level(bank_a))
            self._state_a = bank_a
        if bank_b != self._state_b:
            GPIO.output(self._pin_b, self._level(bank_b))
            self._state_b = bank_b

    def off(self):
        """Desliga tudo — usar em shutdown e erros."""
        self.set(False, False)

    def cleanup(self):
        self.off()
        # Não chama GPIO.cleanup() aqui — o encoder pode precisar do GPIO


# ──────────────────────────────────────────────
# Simulador
# ──────────────────────────────────────────────

class SimulatedRelays:

    def __init__(self):
        self._state_a = False
        self._state_b = False

    def set(self, bank_a: bool, bank_b: bool):
        if bank_a != self._state_a or bank_b != self._state_b:
            self._state_a = bank_a
            self._state_b = bank_b

    def off(self):
        self.set(False, False)

    def cleanup(self):
        self.off()


# ──────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────

def create_relays(pin_a=23, pin_b=24):
    if _HW_AVAILABLE:
        print("[relays] Usando GPIO hardware.")
        return HardwareRelays(pin_a, pin_b)
    print("[relays] GPIO não disponível — usando simulador.")
    return SimulatedRelays()
