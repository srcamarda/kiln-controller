#!/bin/sh
# Força os pinos dos relés para HIGH (módulo HL-52S é ativo em LOW → desligado).
# Chamado pelo systemd em ExecStopPost. Pinos: banco A = GPIO13, banco B = GPIO16.
PINS="13 16"

if command -v pinctrl >/dev/null 2>&1; then          # Raspberry Pi OS Bookworm+
    for p in $PINS; do pinctrl set "$p" op dh; done
elif command -v raspi-gpio >/dev/null 2>&1; then     # Bullseye e anteriores
    for p in $PINS; do raspi-gpio set "$p" op dh; done
else
    # Sem fallback com gpioset: o libgpiod solta a linha quando o processo
    # termina, o pino volta a ser entrada e o relé fica sem garantia de estado.
    # O Raspberry Pi OS traz pinctrl (Bookworm+) ou raspi-gpio (anteriores).
    echo "relays-off: ERRO — pinctrl/raspi-gpio não encontrados; não foi possível" \
         "garantir os relés desligados. Instale o pinctrl (sudo apt install" \
         "raspi-utils) ou o raspi-gpio." >&2
    exit 1
fi
echo "relays-off: GPIO $PINS em HIGH (relés desligados)"
