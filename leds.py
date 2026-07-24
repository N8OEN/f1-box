"""
leds.py — flag animation engine for the F1 light box.

Runs a render thread at a fixed frame rate, reading a single shared flag
value. The async side of f1_box.py only ever writes that value; it never
touches the strip. That keeps the blocking rpi_ws281x calls off the event
loop entirely.

Two drivers:
  Ws281xDriver — real WS2812B strip on GPIO18
  SimDriver    — 24-bit ANSI blocks in the terminal, no hardware needed
"""

import math
import os
import sys
import threading
import time

# ── Configuration ────────────────────────────────────────────
LED_COUNT = 15          # rewritten by install.sh
LED_PIN = 18            # GPIO18 / physical pin 12 (PWM0)
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 90     # 0-255. 90 keeps 15 LEDs under ~350mA so the
                        # strip can run off the Pi's own 5V pin.
LED_INVERT = False
LED_CHANNEL = 0

FPS = 50
GREEN_HOLD_S = 5.0      # solid green, then off


# ── Flags ────────────────────────────────────────────────────
class Flag:
    OFF = "OFF"
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    DOUBLE_YELLOW = "DOUBLE_YELLOW"
    RED = "RED"
    SC = "SC"
    VSC = "VSC"
    CHEQUERED = "CHEQUERED"

    ALL = [OFF, GREEN, YELLOW, DOUBLE_YELLOW, RED, SC, VSC, CHEQUERED]

    LABEL = {
        OFF: "Track dark",
        GREEN: "Green flag",
        YELLOW: "Yellow flag",
        DOUBLE_YELLOW: "Double yellow",
        RED: "Red flag",
        SC: "Safety car",
        VSC: "Virtual safety car",
        CHEQUERED: "Chequered flag",
    }


# Colours are pre-brightness; the driver applies LED_BRIGHTNESS.
C_OFF = (0, 0, 0)
C_GREEN = (0, 255, 60)
C_YELLOW = (255, 190, 0)
C_RED = (255, 0, 0)
C_ORANGE = (255, 90, 0)
C_PURPLE = (150, 0, 255)
C_WHITE = (255, 255, 255)


def _scale(colour, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in colour)


# ── Frame generators ─────────────────────────────────────────
# Each takes (t, n) where t is seconds since the flag was raised,
# and returns a list of n (r, g, b) tuples.

def _frame_off(t, n):
    return [C_OFF] * n


def _frame_green(t, n):
    if t >= GREEN_HOLD_S:
        return [C_OFF] * n
    return [C_GREEN] * n


def _frame_yellow(t, n):
    on = (t * 2.5) % 1.0 < 0.5
    return [C_YELLOW if on else C_OFF] * n


def _frame_double_yellow(t, n):
    phase = (t * 2.5) % 1.0 < 0.5
    return [
        C_YELLOW if ((i % 2 == 0) == phase) else C_OFF
        for i in range(n)
    ]


def _frame_red(t, n):
    return [C_RED] * n


def _breathe(t, colour, n, hz=0.5, floor=0.15):
    level = floor + (1 - floor) * (0.5 + 0.5 * math.sin(2 * math.pi * hz * t))
    return [_scale(colour, level)] * n


def _frame_sc(t, n):
    return _breathe(t, C_ORANGE, n)


def _frame_vsc(t, n):
    return _breathe(t, C_PURPLE, n)


def _frame_chequered(t, n):
    """White pixel chasing the strip, with a short decaying tail."""
    head = (t * 12.0) % n
    px = []
    for i in range(n):
        d = min(abs(i - head), n - abs(i - head))
        px.append(_scale(C_WHITE, max(0.0, 1.0 - d / 2.5)))
    return px


FRAMES = {
    Flag.OFF: _frame_off,
    Flag.GREEN: _frame_green,
    Flag.YELLOW: _frame_yellow,
    Flag.DOUBLE_YELLOW: _frame_double_yellow,
    Flag.RED: _frame_red,
    Flag.SC: _frame_sc,
    Flag.VSC: _frame_vsc,
    Flag.CHEQUERED: _frame_chequered,
}


# ── Drivers ──────────────────────────────────────────────────
class Ws281xDriver:
    def __init__(self, n=LED_COUNT):
        from rpi_ws281x import PixelStrip, Color  # noqa: F401
        self._Color = Color
        self.n = n
        self.strip = PixelStrip(
            n, LED_PIN, LED_FREQ_HZ, LED_DMA,
            LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
        )
        self.strip.begin()

    def show(self, pixels):
        for i, (r, g, b) in enumerate(pixels):
            self.strip.setPixelColor(i, self._Color(r, g, b))
        self.strip.show()

    def close(self):
        self.show([C_OFF] * self.n)


class SimDriver:
    """Renders the strip as coloured blocks on one terminal line."""

    def __init__(self, n=LED_COUNT, fps=20):
        self.n = n
        self.min_interval = 1.0 / fps
        self._last = 0.0
        self._scale = LED_BRIGHTNESS / 255.0
        sys.stdout.write("\n")

    def show(self, pixels):
        now = time.monotonic()
        if now - self._last < self.min_interval:
            return
        self._last = now
        out = []
        for (r, g, b) in pixels:
            r, g, b = (int(c * self._scale) for c in (r, g, b))
            out.append(f"\033[48;2;{r};{g};{b}m  \033[0m")
        sys.stdout.write("\r  " + "".join(out) + "  ")
        sys.stdout.flush()

    def close(self):
        self.show([C_OFF] * self.n)
        sys.stdout.write("\n")
        sys.stdout.flush()


class SerialDriver:
    """Streams frames to a Pico over USB serial. The Pico runs pico_leds.py
    and simply paints what it receives, so all animation stays here."""

    def __init__(self, port, n=LED_COUNT, baud=115200, fps=30):
        import serial  # pyserial
        self.n = n
        self.min_interval = 1.0 / fps
        self._last = 0.0
        self._scale = LED_BRIGHTNESS / 255.0
        self.ser = serial.Serial(port, baud, timeout=0, write_timeout=0.25)
        time.sleep(2.0)  # let the Pico settle after the port opens
        self.ser.reset_input_buffer()

    def show(self, pixels):
        now = time.monotonic()
        if now - self._last < self.min_interval:
            return
        self._last = now
        raw = bytearray()
        for (r, g, b) in pixels:
            raw += bytes((int(r * self._scale),
                          int(g * self._scale),
                          int(b * self._scale)))
        try:
            self.ser.write(raw.hex().encode() + b"\n")
        except Exception:  # noqa: BLE001
            pass  # dropped frame; the next one is 33ms away

    def close(self):
        self.show([C_OFF] * self.n)
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, p.description) for p in list_ports.comports()]


def find_pico():
    """Best guess at the Pico's port. Returns None if nothing looks right."""
    for device, desc in list_serial_ports():
        d = (desc or "").lower()
        if "pico" in d or "board in fs mode" in d or "micropython" in d:
            return device
    for device, _ in list_serial_ports():
        if "usbmodem" in device or "ttyACM" in device:
            return device
    return None


def make_driver(simulate=None, serial_port=None, n=LED_COUNT):
    """Pick a driver: explicit serial port, then hardware strip, then sim."""
    if serial_port:
        if serial_port == "auto":
            serial_port = find_pico()
            if not serial_port:
                print("[leds] no Pico found on any serial port; using simulator",
                      file=sys.stderr)
                return SimDriver(n), True
        try:
            print(f"[leds] streaming to Pico on {serial_port}", file=sys.stderr)
            return SerialDriver(serial_port, n), False
        except Exception as exc:  # noqa: BLE001
            print(f"[leds] serial failed ({exc}); using simulator",
                  file=sys.stderr)
            return SimDriver(n), True

    if simulate is None:
        simulate = os.environ.get("F1BOX_SIM", "").lower() in ("1", "true", "yes")
    if not simulate:
        try:
            return Ws281xDriver(n), False
        except Exception as exc:  # noqa: BLE001
            print(f"[leds] hardware unavailable ({exc}); using simulator",
                  file=sys.stderr)
    return SimDriver(n), True


# ── Renderer thread ──────────────────────────────────────────
class Renderer(threading.Thread):
    def __init__(self, driver, n=LED_COUNT):
        super().__init__(daemon=True)
        self.driver = driver
        self.n = n
        self._flag = Flag.OFF
        self._raised_at = time.monotonic()
        self._stop = threading.Event()
        # Latest rendered frame, so the web UI can mirror the strip.
        self.last_frame = [C_OFF] * n

    @property
    def flag(self):
        return self._flag

    def set_flag(self, flag):
        """Safe to call from any thread or from the event loop."""
        if flag not in FRAMES:
            return False
        if flag == self._flag:
            return False
        self._flag = flag
        self._raised_at = time.monotonic()
        return True

    def run(self):
        period = 1.0 / FPS
        while not self._stop.is_set():
            t = time.monotonic() - self._raised_at
            pixels = FRAMES[self._flag](t, self.n)
            self.last_frame = pixels
            try:
                self.driver.show(pixels)
            except Exception as exc:  # noqa: BLE001
                print(f"[leds] show failed: {exc}", file=sys.stderr)
            time.sleep(period)

    def stop(self):
        self._stop.set()
        try:
            self.driver.close()
        except Exception:  # noqa: BLE001
            pass


# ── Standalone test ──────────────────────────────────────────
if __name__ == "__main__":
    sim = "--sim" in sys.argv
    driver, is_sim = make_driver(simulate=sim or None)
    r = Renderer(driver)
    r.start()
    print(f"Cycling all flags on {LED_COUNT} LEDs "
          f"({'simulator' if is_sim else 'hardware'}). Ctrl-C to stop.\n")
    try:
        for f in [Flag.GREEN, Flag.YELLOW, Flag.DOUBLE_YELLOW, Flag.RED,
                  Flag.SC, Flag.VSC, Flag.CHEQUERED, Flag.OFF]:
            print(f"\n{Flag.LABEL[f]}")
            r.set_flag(f)
            time.sleep(6)
    except KeyboardInterrupt:
        pass
    finally:
        r.stop()
