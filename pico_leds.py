# pico_leds.py — save this onto the Pico as main.py
#
# Listens on USB serial for frames and paints them onto the strip.
# One frame is N*3 bytes as lowercase hex, terminated with a newline:
#   15 LEDs -> 90 hex characters + "\n"
#
# The host (f1_box.py --serial) does all the animation. This just paints.
# If frames stop arriving for 3 seconds the strip blanks itself, so a
# host crash can't leave a stale red flag burning.

import sys
import select
import time
from machine import Pin
from neopixel import NeoPixel

try:
    from binascii import unhexlify
except ImportError:
    from ubinascii import unhexlify

# ── Configuration ────────────────────────────────────────────
N = 15          # LEDs on the strip
PIN = 0         # GP0, physical pin 1. Change to whatever you wired.
TIMEOUT_MS = 3000

np = NeoPixel(Pin(PIN, Pin.OUT), N)
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)

FRAME_CHARS = N * 6


def blank():
    for i in range(N):
        np[i] = (0, 0, 0)
    np.write()


def startup_blink():
    """Two short white blinks so you know the firmware is running."""
    for _ in range(2):
        for i in range(N):
            np[i] = (12, 12, 12)
        np.write()
        time.sleep_ms(90)
        blank()
        time.sleep_ms(120)


startup_blink()

buf = b""
frame = None
last_rx = time.ticks_ms()

while True:
    # Drain everything waiting. If the host has run ahead of us, only the
    # most recent complete frame is kept — stale ones are dropped.
    while poll.poll(0):
        ch = sys.stdin.buffer.read(1)
        if not ch:
            break
        if ch == b"\n":
            if len(buf) == FRAME_CHARS:
                frame = buf
            buf = b""
        elif ch != b"\r":
            buf += ch
            if len(buf) > FRAME_CHARS:
                buf = b""       # resync after a corrupt line

    if frame is not None:
        try:
            raw = unhexlify(frame)
            for i in range(N):
                np[i] = (raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2])
            np.write()
            last_rx = time.ticks_ms()
        except Exception:
            pass
        frame = None
    elif time.ticks_diff(time.ticks_ms(), last_rx) > TIMEOUT_MS:
        blank()
        last_rx = time.ticks_ms()

    time.sleep_ms(4)
