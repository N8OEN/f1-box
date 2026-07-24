#!/usr/bin/env bash
# install.sh — set up the F1 light box on a Raspberry Pi.
# Run with: sudo bash install.sh
set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; R='\033[0;31m'; N='\033[0m'
ok()  { echo -e "${G}✓${N} $1"; }
say() { echo -e "${C}→${N} $1"; }

[ "$EUID" -eq 0 ] || { echo -e "${R}Run this with sudo.${N}"; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=f1-box

echo ""
echo -e "${Y}How are the LEDs driven?${N}"
echo "  1) Pico over USB   — strip wired to a Pico, Pico plugged into the Pi"
echo "  2) Direct on GPIO18 — strip wired to the Pi's own header"
read -r -p "Choice [1]: " MODE
MODE=${MODE:-1}

say "Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3-pip git >/dev/null
ok "System packages installed"

say "Installing Python packages..."
if [ "$MODE" = "2" ]; then
  pip3 install --break-system-packages -q aiohttp rpi_ws281x
else
  pip3 install --break-system-packages -q aiohttp pyserial
fi
ok "Python packages installed"

echo ""
read -r -p "How many LEDs are on the strip? [15]: " LED_COUNT
LED_COUNT=${LED_COUNT:-15}
sed -i "s/^LED_COUNT\s*=.*/LED_COUNT = $LED_COUNT          # rewritten by install.sh/" "$REPO_DIR/leds.py"
sed -i "s/^N = .*/N = $LED_COUNT          # LEDs on the strip/" "$REPO_DIR/pico_leds.py"
sed -i "s/repeat(15,1fr)/repeat($LED_COUNT,1fr)/" "$REPO_DIR/templates/index.html"
sed -i "s/i < 15; i++/i < $LED_COUNT; i++/" "$REPO_DIR/templates/index.html"
ok "LED count set to $LED_COUNT"

if [ "$MODE" = "2" ]; then
  # GPIO18 is driven by PWM0, which onboard audio also claims. Leaving audio
  # enabled causes flicker and dropped pixels. Not needed in Pico mode.
  CFG=/boot/firmware/config.txt
  [ -f "$CFG" ] || CFG=/boot/config.txt
  if grep -q "^dtparam=audio=on" "$CFG"; then
    sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CFG"
    ok "Onboard audio disabled in $CFG"
  elif ! grep -q "dtparam=audio=off" "$CFG"; then
    echo "dtparam=audio=off" >> "$CFG"
    ok "Onboard audio disabled in $CFG"
  else
    ok "Onboard audio already disabled"
  fi
  EXEC="/usr/bin/python3 $REPO_DIR/f1_box.py"
else
  EXEC="/usr/bin/python3 $REPO_DIR/f1_box.py --serial"
fi

say "Installing the service..."
sed -e "s|WorkingDirectory=.*|WorkingDirectory=$REPO_DIR|" \
    -e "s|ExecStart=.*|ExecStart=$EXEC|" \
    "$REPO_DIR/$SERVICE.service" > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
ok "Service installed and enabled at boot"

IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${G}Done.${N} $LED_COUNT LEDs."
if [ "$MODE" = "2" ]; then
  echo "Wire DIN to GPIO18 (pin 12), 5V to pin 2, GND to pin 6."
else
  echo "Wire DIN to the Pico's GP0, 5V to VBUS (pin 40), GND to any GND pin."
  echo "Copy pico_leds.py onto the Pico as main.py before starting."
fi
echo ""
echo -e "  Control panel:  ${C}http://${IP}:5000${N}"
echo -e "  Start now:      ${Y}sudo systemctl start $SERVICE${N}"
echo -e "  Watch the log:  ${Y}journalctl -u $SERVICE -f${N}"
if [ "$MODE" = "2" ]; then
  echo ""
  echo -e "  ${R}Reboot to apply the audio change:${N} ${Y}sudo reboot${N}"
fi
echo ""
