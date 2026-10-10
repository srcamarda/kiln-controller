"""
relays.py — Controle dos relés dos bancos A e B via GPIO.

Pinagem padrão:
  Banco A → GPIO13  (pino físico 33)
  Banco B → GPIO16  (pino físico 36)

O módulo HL-52S é ativo em LOW (LOW = relé fechado).
A lógica é invertida aqui para que True = banco ligado.

Saída do programa: ao encerrar, o gpiozero "fecha" os pinos e os devolve
como ENTRADA — no HL-52S isso pode acionar o relé (ver Crítico 4). Por isso
este módulo registra um handler de saída que força os pinos em HIGH (relé
desligado) com `pinctrl`/`raspi-gpio` DEPOIS do encerramento do gpiozero.
Para que a ordem funcione (atexit é LIFO), o handler é registrado antes de
importar o gpiozero, e o main importa este módulo antes de qualquer outro
que use GPIO. Sob o systemd, o deploy/relays-off.sh faz o mesmo no
ExecStopPost (cobre também SIGKILL/watchdog, quando o atexit não roda).
"""

import atexit
import os
import shutil
import subprocess

# Pinos de relé em uso neste processo (preenchido por HardwareRelays)
_REGISTERED_PINS = set()


def force_pins_high(pins=None) -> bool:
    """
    Coloca os pinos como saída em HIGH (relé desligado no HL-52S) sem passar
    pelo gpiozero. Retorna True se conseguiu.
    """
    pins = sorted(pins if pins is not None else _REGISTERED_PINS)
    if not pins:
        return True
    if shutil.which("pinctrl"):                      # Raspberry Pi OS Bookworm+
        cmds = [["pinctrl", "set", str(p), "op", "dh"] for p in pins]
    elif shutil.which("raspi-gpio"):                 # Bullseye e anteriores
        cmds = [["raspi-gpio", "set", str(p), "op", "dh"] for p in pins]
    else:
        print("\r[relays] AVISO: sem pinctrl/raspi-gpio — não foi possível forçar "
              f"os pinos {pins} em HIGH na saída.")
        return False
    ok = True
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"\r[relays] Falha ao forçar pino em HIGH ({' '.join(cmd)}): {e}")
            ok = False
    return ok


def _force_on_exit():
    if _REGISTERED_PINS:
        if force_pins_high():
            print(f"\r[relays] Pinos {sorted(_REGISTERED_PINS)} em HIGH (relés desligados).")


# Registrado ANTES de importar o gpiozero → roda DEPOIS do encerramento dele
atexit.register(_force_on_exit)

try:
    from gpiozero import OutputDevice
    _GPIO_AVAILABLE = True
except Exception:
    _GPIO_AVAILABLE = False
    OutputDevice = None

def _hw_available():
    if os.environ.get("SIM_RELAYS") == "1":
        return False
    if not _GPIO_AVAILABLE:
        return False
    return True

_HW_AVAILABLE = _hw_available()


# ──────────────────────────────────────────────
# Hardware real
# ──────────────────────────────────────────────

class HardwareRelays:

    # HL-52S: LOW ativa o relé
    ACTIVE_LOW = True

    def __init__(self, pin_a, pin_b):
        self._state_a = False
        self._state_b = False
        _REGISTERED_PINS.update({pin_a, pin_b})

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
        """Desliga tudo — usar em shutdown e erros. Escreve sempre nos pinos
        (não confia no estado em cache)."""
        self._relay_a.value = False
        self._relay_b.value = False
        self._state_a = self._state_b = False

    def cleanup(self):
        # NÃO chama close(): fechar devolve o pino como entrada (= relé podendo
        # acionar). Os pinos ficam como saída em HIGH; na saída do processo o
        # gpiozero os fecha e _force_on_exit() os coloca de volta em HIGH.
        self.off()


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

def create_relays(pin_a=13, pin_b=16):
    """
    SIM_RELAYS=1 → SimulatedRelays. Caso contrário exige GPIO real:
    sem gpiozero o programa não inicia (nada de simulação silenciosa).
    """
    if os.environ.get("SIM_RELAYS") == "1":
        print("\r[relays] ATENÇÃO: simulação forçada (SIM_RELAYS=1).")
        return SimulatedRelays()
    if not _GPIO_AVAILABLE:
        raise RuntimeError(
            "gpiozero indisponível — não é possível controlar os relés. "
            "Para rodar sem hardware use SIM_RELAYS=1."
        )
    print("\r[relays] Usando hardware.")
    return HardwareRelays(pin_a, pin_b)