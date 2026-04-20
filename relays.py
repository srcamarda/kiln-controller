"""
relays.py — Controle dos relés dos bancos A e B via GPIO.

Pinagem padrão:
  Banco A → GPIO13  (pino físico 33)
  Banco B → GPIO19  (pino físico 35)

O módulo HL-52S é ativo em LOW (LOW = relé fechado).
A lógica é invertida aqui para que True = banco ligado.
"""

import os

try:
    from gpiozero import OutputDevice
    _GPIO_AVAILABLE = True
except Exception:
    _GPIO_AVAILABLE = False
    OutputDevice = None

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


# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareRelays:

    # HL-52S: LOW ativa o relé
    ACTIVE_LOW = True

    def __init__(self, pin_a=13, pin_b=19):
        self._state_a = False
        self._state_b = False

        # initial_value=False garante que o relé inicie desligado 
        # (como active_high é False, o pino físico vai iniciar em HIGH)
        self._relay_a = OutputDevice(
            pin_a, 
            active_high=not self.ACTIVE_LOW, 
            initial_value=False
        )
        self._relay_b = OutputDevice(
            pin_b, 
            active_high=not self.ACTIVE_LOW, 
            initial_value=False
        )

    def set(self, bank_a: bool, bank_b: bool):
        if bank_a != self._state_a:
            self._relay_a.value = bank_a
            self._state_a = bank_a
        if bank_b != self._state_b:
            self._relay_b.value = bank_b
            self._state_b = bank_b

    def off(self):
        """Desliga tudo — usar em shutdown e erros."""
        self.set(False, False)

    def cleanup(self):
        self.off()
        # No gpiozero, usar .close() libera apenas estes pinos específicos,
        # sem interferir no encoder ou outros dispositivos em uso.
        self._relay_a.close()
        self._relay_b.close()


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

def create_relays(pin_a, pin_b):
    if _HW_AVAILABLE:
        print("\r[relays] Usando GPIO hardware.")
        return HardwareRelays(pin_a, pin_b)
    print("\r[relays] GPIO não disponível — usando simulador.")
    return SimulatedRelays()