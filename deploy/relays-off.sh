#!/bin/sh
# Força os pinos dos relés para HIGH (módulo HL-52S é ativo em LOW → desligado).
# Chamado pelo systemd em ExecStopPost. Pinos: banco A = GPIO13, banco B = GPIO16.
PINS="13 16"

if command -v pinctrl >/dev/null 2>&1; then          # Raspberry Pi OS Bookworm+
    for p in $PINS; do pinctrl set "$p" op dh; done
elif command -v raspi-gpio >/dev/null 2>&1; then     # Bullseye e anteriores
    for p in $PINS; do raspi-gpio set "$p" op dh; done
elif command -v gpioset >/dev/null 2>&1; then        # libgpiod (mantém enquanto roda)
    gpioset --mode=time --sec=1 gpiochip0 13=1 16=1
else
    echo "relays-off: nenhuma ferramenta de GPIO encontrada (pinctrl/raspi-gpio)" >&2
    exit 1
fi
echo "relays-off: GPIO $PINS em HIGH (relés desligados)"
