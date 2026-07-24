# F1 Box

A WS2812B strip that shows the current F1 track flag, driven by the official
live timing feed, with a phone control panel.

Runs on a Raspberry Pi with the strip attached, or on any laptop with
`--sim`, which draws the strip in the terminal and mirrors it to the phone UI.

---

## Wiring — 15 LEDs

| Strip | Pi |
|---|---|
| DIN (data) | GPIO18 — physical pin 12 |
| 5V | 5V — physical pin 2 |
| GND | GND — physical pin 6 |

At the default brightness the worst-case draw across every animation is about
**185 mA**, so the strip runs off the Pi's own 5V pin. No external supply is
needed at this LED count. If you extend past roughly 30 LEDs, or raise
`LED_BRIGHTNESS` in `leds.py`, move the strip to its own 5V supply and tie the
grounds together.

Optional but worth doing if you see the first pixel misbehave: a 330–470Ω
resistor inline on the data wire, and a 1000µF capacitor across the strip's
5V and GND.

---

## Run it on a laptop first

```bash
pip install aiohttp
python3 f1_box.py --sim
```

Open http://localhost:5000. The strip appears as 15 blocks at the top of the
page and in your terminal. Everything works except the physical LEDs.

Replay a past session to prove the animations without waiting for a race:

```bash
python3 f1_box.py --sim --no-live --replay 9636 --speed 30
```

Find a session key from
`https://api.openf1.org/v1/sessions?year=2026&session_name=Race`.

---

## Install on the Pi

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. Before writing,
open the gear icon and set the hostname to `f1box`, enable SSH, set a
username and password, and enter your WiFi details. Then:

```bash
ssh pi@f1box.local
git clone <your repo url> f1-box
cd f1-box
sudo bash install.sh
sudo reboot
```

After the reboot the panel is at `http://f1box.local:5000`.

```bash
sudo systemctl start f1-box     # start now
journalctl -u f1-box -f         # watch it
```

The installer disables onboard audio. This is not optional — audio and the LED
library both want PWM0 on GPIO18, and leaving audio on causes flicker and
dropped pixels. It needs the reboot to take effect.

`rpi_ws281x` requires root, so the service runs as root. Running
`python3 f1_box.py` by hand as `pi` fails with a permissions error that does
not obviously say "permissions".

---

## Flags

| Flag | Strip |
|---|---|
| Green | Solid green, 5 seconds, then out |
| Yellow | Whole strip flashing |
| Double yellow | Alternating pixels flashing |
| Red | Solid, holds |
| Safety car | Slow orange pulse |
| Virtual safety car | Slow violet pulse |
| Chequered | White pixel chasing the strip |

Tapping a flag in the panel takes manual control for 30 seconds, then hands
back to the feed. "Hand back to the feed" releases it immediately.

---

## How the data works

The F1 SignalR feed cannot be reached from a browser: the negotiate request
and the WebSocket must share a session, because the load balancer pins the
connection token to a backend with a cookie. `f1_box.py` holds both in one
`aiohttp.ClientSession`, which preserves that affinity, and pushes state to
the phone over its own WebSocket.

If SignalR is unavailable, OpenF1's REST endpoint is polled as a fallback. It
is delayed rather than live, but it keeps the box lit.

Four parsing details that are easy to get wrong, all handled here:

- The `R` snapshot arrives once after subscribing and states the current flag.
  It must be parsed, not skipped.
- In `M` deltas, `A[0]` is the topic name and `A[1]` is the payload.
- `RaceControlMessages.Messages` arrives as a list in the snapshot but as an
  index-keyed object in deltas.
- `TrackStatus` code 7 means "ending" for both the safety car and the VSC. It
  only means green if the current state is SC; under VSC, wait for the track
  clear message.

---

Unofficial. Not affiliated with Formula 1, the FIA, or Formula One Management.
