# Build guide — Pi + Pico

Final shape: the **Pi 3** runs everything and sits on WiFi. The **Pico** hangs
off one of its USB ports and drives the strip. The **strip** sits on the
breadboard with the Pico. Your phone is the only screen you need once it's
running.

Use the Pi 3, not the Zero 2 — the 3 has full-size USB-A ports, so the Pico's
own cable plugs straight in. The Zero 2 would need an OTG adapter.

---

## 1 — Flash the SD card

On the Mac, install **Raspberry Pi Imager** from raspberrypi.com/software.

- **Device:** Raspberry Pi 3
- **OS:** Raspberry Pi OS (64-bit) — the *full* desktop version, not Lite
- **Storage:** your SD card

Before writing, click the **gear / Edit Settings** button and fill in:

| Setting | Value |
|---|---|
| Hostname | `f1box` |
| Username | `pi` (and a password you'll remember) |
| Configure wireless LAN | your SSID and WiFi password |
| Wireless LAN country | GB |
| Locale / timezone | Europe/London |
| Enable SSH | tick it, use password authentication |

Write, wait, eject.

**Why desktop rather than Lite:** Thonny comes preinstalled, the file manager
mounts the Pico automatically when you flash it, and if the WiFi details are
wrong you can fix them on screen instead of re-flashing. You said you have
HDMI — use it for the build. Once it works, unplug the screen; nothing about
the running system needs it.

**If flashing has failed before:** it is almost always either a card the
reader can't write reliably, or Imager silently not applying the settings
because the gear dialog was dismissed rather than saved. After writing,
put the card back in the Mac — you should see a `bootfs` volume containing
`firstrun.sh`. If that file isn't there, the settings didn't take, and the
Pi will boot with no WiFi and no SSH.

---

## 2 — First boot

Card in, HDMI in, keyboard in, power last. Let it reach the desktop and
finish its first-boot resize and reboot.

Open a terminal and check WiFi:

```bash
hostname -I
```

An address like `192.168.1.x` means you're on the network. Write it down —
that's where the control panel lives. If it comes back empty, fix WiFi from
the desktop's network icon before going further.

---

## 3 — Get the code onto the Pi

Push the `f1-box` folder to GitHub from the Mac, then on the Pi:

```bash
git clone https://github.com/N8OEN/f1-box.git
cd f1-box
```

If you'd rather skip GitHub for now, put the folder on a USB stick and copy
it across — but push it afterwards, so this doesn't get lost the way the
May build did.

---

## 4 — Put MicroPython on the Pico

All of this happens **on the Pi**. The Mac is not involved.

Download the firmware:

```bash
cd ~
wget https://micropython.org/download/RPI_PICO/RPI_PICO-latest.uf2
```

Now, with the Pico **unplugged**, hold down the white **BOOTSEL** button on
the Pico, plug its USB cable into the Pi, then release BOOTSEL after a
second. A drive called `RPI-RP2` appears on the desktop.

```bash
cp ~/RPI_PICO-latest.uf2 /media/pi/RPI-RP2/
```

The Pico reboots on its own and the drive disappears. That's normal and means
it worked. Confirm the Pi can see it as a serial device:

```bash
ls /dev/ttyACM*
```

You want `/dev/ttyACM0`.

---

## 5 — Put the firmware on the Pico

```bash
sudo pip3 install --break-system-packages mpremote
cd ~/f1-box
mpremote connect /dev/ttyACM0 fs cp pico_leds.py :main.py
mpremote connect /dev/ttyACM0 reset
```

The name matters: it has to land on the Pico as `main.py`, because that's the
file MicroPython runs automatically at power-up.

The strip should blink white twice. That's the firmware saying hello, and it
happens before the Pi sends anything — so if you see it, the Pico and the
wiring are both good.

*(Thonny does the same job if you prefer clicking: open `pico_leds.py`,
Save As → Raspberry Pi Pico → filename `main.py`.)*

---

## 6 — Wire the strip

Pico unplugged while you do this.

| Strip wire | Pico |
|---|---|
| DIN / data | **GP0** — physical pin 1 |
| 5V | **VBUS** — physical pin 40 |
| GND | **GND** — physical pin 38 (or any GND) |

The Pico's male header pins push into the breadboard, and the strip's male
jumper wires push into the same rows. That's the whole reason this route needs
nothing you don't already have.

VBUS is the pin that passes through the 5V coming from the USB cable — the
3.3V pin will not drive the strip properly. Physical pin 40 is the corner pin
at the opposite end from the USB socket.

---

## 7 — Install and run

```bash
cd ~/f1-box
sudo bash install.sh
```

Choose **1** for Pico over USB, and **15** for the LED count. Then:

```bash
sudo systemctl start f1-box
journalctl -u f1-box -f
```

On your phone, open `http://f1box.local:5000` — or the IP address from step 2
if `.local` doesn't resolve. Tap the flag buttons. The strip should follow
immediately, and the panel mirrors it.

To leave it running for the race:

```bash
sudo systemctl enable f1-box
```

It now starts on boot. Unplug the HDMI and keyboard. One power cable to the
Pi, one USB cable to the Pico.

---

## Checks and fixes

**Strip does nothing, panel works.** The Pi didn't find the Pico. Run
`python3 f1_box.py --list-ports`. If nothing appears, the Pico isn't running
MicroPython — redo step 4.

**Strip lights but colours are wrong** (red showing green, and so on). Some
strips are RGB and some are GRB. Swap the order in `paint` inside
`pico_leds.py` and re-copy it.

**Strip freezes on the last flag.** The Pi stopped sending. The Pico blanks
itself after 3 seconds, so if it's frozen rather than dark, the Pi is still
streaming and the problem is upstream — check `journalctl`.

**First pixel misbehaves.** Add a 330–470Ω resistor inline on the data wire.

---

## Test plan for the weekend

- **Now:** `python3 f1_box.py --sim` on any machine and watch the log fill
  during a live session. This is the only thing that proves the live timing
  parser works, and it has never been tested against a running session.
- **Saturday 15:00 BST, qualifying:** full setup, Pi and Pico, live.
- **Sunday 14:00 BST, the race.**

If the parser turns out to be broken, the manual flag buttons still work, and
so does replay — so you always have a light box, just not an automatic one.
