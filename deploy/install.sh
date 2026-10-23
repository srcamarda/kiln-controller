#!/bin/sh
# Instala o controlador do forno como serviço systemd.
# Uso (no Pi, na pasta do projeto):  sudo sh deploy/install.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Rode com sudo: sudo sh deploy/install.sh" >&2
    exit 1
fi

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-pi}"
if [ -x "$DIR/kiln-env/bin/python" ]; then
    PYTHON="$DIR/kiln-env/bin/python"
else
    PYTHON="$(command -v python3)"
fi

echo "Projeto: $DIR"
echo "Usuário: $USER_NAME"
echo "Python:  $PYTHON"

# Grupos de hardware (ignora os que não existem nesta distro)
for g in gpio spi i2c video; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME" || true
done

# O ExecStopPost roda como root: o script fica numa cópia de root, fora da
# pasta do projeto (que o usuário do serviço pode alterar) — senão quem
# controlasse o usuário pi viraria root no próximo "systemctl stop" (achado N4).
RELAYS_OFF=/usr/local/sbin/kiln-relays-off
install -o root -g root -m 0755 "$DIR/deploy/relays-off.sh" "$RELAYS_OFF"

# Desligar os relés na saída depende de pinctrl ou raspi-gpio
if ! command -v pinctrl >/dev/null 2>&1 && ! command -v raspi-gpio >/dev/null 2>&1; then
    echo "AVISO: pinctrl/raspi-gpio não encontrados — instale com 'sudo apt install raspi-utils'" >&2
    echo "       (sem eles os relés não ficam garantidamente desligados quando o serviço para)." >&2
fi

# Unidade do serviço
sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" -e "s|@PYTHON@|$PYTHON|g" \
    -e "s|@RELAYS_OFF@|$RELAYS_OFF|g" \
    "$DIR/deploy/kiln-controller.service" > /etc/systemd/system/kiln-controller.service

# Watchdog de hardware
mkdir -p /etc/systemd/system.conf.d
cp "$DIR/deploy/watchdog.conf" /etc/systemd/system.conf.d/kiln-watchdog.conf

# Ativa o watchdog do SoC no boot (padrão no Raspberry Pi OS recente; garante)
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
if [ -f "$CFG" ] && ! grep -q "^dtparam=watchdog=on" "$CFG"; then
    echo "dtparam=watchdog=on" >> "$CFG"
    echo "Adicionado dtparam=watchdog=on em $CFG (vale após reiniciar)."
fi

# Relés desligados desde o início do boot: o firmware do Pi coloca GPIO13/16
# como saída em HIGH antes do Linux subir (HL-52S é ativo em LOW). Sem isso,
# os pinos nascem como entrada com pull-down (= nível baixo) até o serviço iniciar.
if [ -f "$CFG" ] && ! grep -q "^gpio=13,16=op,dh" "$CFG"; then
    echo "gpio=13,16=op,dh" >> "$CFG"
    echo "Adicionado gpio=13,16=op,dh em $CFG (vale após reiniciar)."
fi

# .env com permissão restrita
if [ -f "$DIR/.env" ]; then
    chown "$USER_NAME" "$DIR/.env"
    chmod 600 "$DIR/.env"
else
    echo "AVISO: $DIR/.env não existe — Telegram ficará desativado (ver .env.example)."
fi

systemctl daemon-reload
systemctl daemon-reexec          # aplica o RuntimeWatchdogSec
systemctl enable kiln-controller.service
systemctl restart kiln-controller.service

echo
echo "Pronto. Comandos úteis:"
echo "  systemctl status kiln-controller     # estado"
echo "  journalctl -u kiln-controller -f     # log ao vivo"
echo "  sudo systemctl stop kiln-controller  # para (a queima é retomada no próximo start!)"
