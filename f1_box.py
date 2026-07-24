#!/usr/bin/env python3
"""
f1_box.py — F1 flag light box.

One aiohttp process does everything:
  • holds the F1 SignalR negotiate + WebSocket in a single ClientSession
    so the load balancer's cookie affinity survives
  • derives a single track-wide flag state from TrackStatus and
    RaceControlMessages
  • drives the WS2812B strip via a render thread in leds.py
  • serves the phone control UI and pushes state to it over /ws

Run:
    sudo python3 f1_box.py                 # hardware
    python3 f1_box.py --sim                # simulator, any machine
    python3 f1_box.py --sim --replay 9636  # replay a past session
"""

import argparse
import asyncio
import json
import logging
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

import leds
from leds import Flag

# ── Constants ────────────────────────────────────────────────
SIGNALR_BASE = "https://livetiming.formula1.com/signalr"
CONNECTION_DATA = '[{"name":"Streaming"}]'
CLIENT_PROTOCOL = "1.5"

TOPICS = [
    "Heartbeat",
    "SessionInfo",
    "SessionStatus",
    "TrackStatus",
    "RaceControlMessages",
    "LapCount",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

OPENF1 = "https://api.openf1.org/v1"
OPENF1_POLL_S = 6.0

HTTP_PORT = 5000
FRAME_HZ = 15
LOG_LINES = 200

log = logging.getLogger("f1box")


# ── Flag derivation ──────────────────────────────────────────
# TrackStatus.Status codes. 7 is deliberately absent: it fires for both
# safety car ending and VSC ending, so it needs the current state to
# disambiguate. See _from_track_status.
TRACK_STATUS = {
    "1": Flag.GREEN,
    "2": Flag.YELLOW,
    "4": Flag.SC,
    "5": Flag.RED,
    "6": Flag.VSC,
}

RC_FLAG = {
    "GREEN": Flag.GREEN,
    "CLEAR": Flag.GREEN,
    "YELLOW": Flag.YELLOW,
    "DOUBLE YELLOW": Flag.DOUBLE_YELLOW,
    "RED": Flag.RED,
    "CHEQUERED": Flag.CHEQUERED,
}


class State:
    def __init__(self, renderer):
        self.renderer = renderer
        self.flag = Flag.OFF
        self.source = "idle"          # signalr | openf1 | replay | manual | idle
        self.session = "—"
        self.connected = False
        self.manual_until = 0.0
        self.log = deque(maxlen=LOG_LINES)
        self.clients = set()
        self._loop = None

    def bind_loop(self, loop):
        self._loop = loop

    # -- logging ----------------------------------------------
    def note(self, text, kind="info"):
        entry = {
            "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "text": text,
            "kind": kind,
        }
        self.log.append(entry)
        log.info(text)
        self._push({"type": "log", "entry": entry})

    # -- flag setting -----------------------------------------
    def raise_flag(self, flag, source, note=None):
        """Set the flag. Feed sources are ignored while a manual hold is on."""
        if source != "manual" and time.monotonic() < self.manual_until:
            return
        if source == "manual":
            self.manual_until = time.monotonic() + 30.0
        changed = self.renderer.set_flag(flag)
        self.flag = flag
        self.source = source
        if changed:
            self.note(note or f"{Flag.LABEL[flag]} — {source}", kind=flag)
        self._push(self.snapshot())

    def clear_manual(self):
        self.manual_until = 0.0
        self.source = "signalr" if self.connected else "idle"
        self.note("Manual hold released, following the feed again")
        self._push(self.snapshot())

    def snapshot(self):
        return {
            "type": "state",
            "flag": self.flag,
            "label": Flag.LABEL[self.flag],
            "source": self.source,
            "session": self.session,
            "connected": self.connected,
            "manual": time.monotonic() < self.manual_until,
        }

    # -- fan-out ----------------------------------------------
    def _push(self, payload):
        if not self._loop or not self.clients:
            return
        data = json.dumps(payload)
        for ws in list(self.clients):
            self._loop.create_task(self._send(ws, data))

    @staticmethod
    async def _send(ws, data):
        try:
            await ws.send_str(data)
        except Exception:  # noqa: BLE001
            pass


def _from_track_status(state, status):
    status = str(status)
    if status in TRACK_STATUS:
        return TRACK_STATUS[status]
    if status == "7":
        # Ending. Only treat as a safety car restart if we were under SC;
        # under VSC, wait for the TRACK CLEAR race control message.
        return Flag.GREEN if state.flag == Flag.SC else None
    return None


def _normalise_messages(raw):
    """SignalR deltas deliver Messages as an index-keyed object, not a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return list(raw.values())
    return []


def handle_topic(state, topic, data, source):
    """Feed one topic payload into the flag state machine."""
    if not isinstance(data, dict):
        return

    if topic == "SessionInfo":
        meeting = (data.get("Meeting") or {}).get("Name")
        name = data.get("Name")
        if meeting or name:
            state.session = " — ".join(x for x in (meeting, name) if x)

    elif topic == "TrackStatus":
        flag = _from_track_status(state, data.get("Status"))
        if flag:
            msg = data.get("Message") or ""
            state.raise_flag(flag, source,
                             f"{Flag.LABEL[flag]} — TrackStatus {msg}".strip())

    elif topic == "RaceControlMessages":
        for m in _normalise_messages(data.get("Messages")):
            if not isinstance(m, dict):
                continue
            text = (m.get("Message") or "").strip()
            raw_flag = (m.get("Flag") or "").upper().strip()
            flag = RC_FLAG.get(raw_flag)
            if flag:
                scope = (m.get("Scope") or "Track").title()
                state.raise_flag(flag, source,
                                 f"{Flag.LABEL[flag]} ({scope}) — {text}")
            elif text:
                state.note(f"RC: {text}", kind="rc")

    elif topic == "SessionStatus":
        st = (data.get("Status") or "").lower()
        if st in ("finished", "finalised", "ends"):
            state.raise_flag(Flag.CHEQUERED, source, "Session finished")


# ── SignalR client ───────────────────────────────────────────
async def signalr_task(state):
    backoff = 3
    while True:
        try:
            await _signalr_once(state)
            backoff = 3
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.connected = False
            state.note(f"Live timing dropped: {exc}. Retrying in {backoff}s.",
                       kind="error")
            state._push(state.snapshot())
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def _signalr_once(state):
    # One session for both requests. The negotiate response sets a GCLB
    # affinity cookie that the WebSocket handshake must carry, otherwise
    # the ConnectionToken lands on a different backend and is rejected.
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout,
                                     headers={"User-Agent": UA}) as sess:
        neg_url = (f"{SIGNALR_BASE}/negotiate"
                   f"?connectionData={urllib.parse.quote(CONNECTION_DATA)}"
                   f"&clientProtocol={CLIENT_PROTOCOL}")
        async with sess.get(neg_url) as resp:
            resp.raise_for_status()
            neg = json.loads(await resp.text())
        token = neg["ConnectionToken"]

        ws_url = (f"{SIGNALR_BASE.replace('https', 'wss')}/connect"
                  f"?transport=webSockets"
                  f"&clientProtocol={CLIENT_PROTOCOL}"
                  f"&connectionToken={urllib.parse.quote(token)}"
                  f"&connectionData={urllib.parse.quote(CONNECTION_DATA)}")

        async with sess.ws_connect(ws_url, heartbeat=15) as ws:
            await ws.send_str(json.dumps({
                "H": "Streaming", "M": "Subscribe", "A": [TOPICS], "I": 1,
            }))
            state.connected = True
            state.note("Live timing connected", kind="ok")
            state._push(state.snapshot())

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                _dispatch(state, frame)

    state.connected = False


def _dispatch(state, frame):
    # R: the full snapshot sent immediately after Subscribe. Parse it —
    # it is the only place the current flag is stated on connect.
    snapshot = frame.get("R")
    if isinstance(snapshot, dict):
        for topic, data in snapshot.items():
            handle_topic(state, topic, data, "signalr")

    # M: deltas. A[0] is the topic name, A[1] is the payload.
    for m in frame.get("M") or []:
        args = m.get("A") or []
        if len(args) >= 2 and isinstance(args[0], str):
            handle_topic(state, args[0], args[1], "signalr")


# ── OpenF1 fallback ──────────────────────────────────────────
async def openf1_task(state):
    """Polls OpenF1 while SignalR is down. Delayed, but better than dark."""
    seen = set()
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
        while True:
            await asyncio.sleep(OPENF1_POLL_S)
            if state.connected or state.source == "replay":
                continue
            try:
                async with sess.get(f"{OPENF1}/race_control",
                                    params={"session_key": "latest"}) as r:
                    if r.status != 200:
                        continue
                    rows = await r.json()
            except Exception:  # noqa: BLE001
                continue
            for row in sorted(rows, key=lambda x: x.get("date") or ""):
                key = (row.get("date"), row.get("message"))
                if key in seen:
                    continue
                seen.add(key)
                handle_topic(state, "RaceControlMessages",
                             {"Messages": [{
                                 "Message": row.get("message"),
                                 "Flag": row.get("flag"),
                                 "Scope": row.get("scope"),
                             }]}, "openf1")


# ── Replay ───────────────────────────────────────────────────
async def replay_task(state, session_key, speed):
    state.note(f"Replaying session {session_key} at {speed}x", kind="ok")
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
            async with sess.get(f"{OPENF1}/race_control",
                                params={"session_key": session_key}) as r:
                rows = await r.json()
    except Exception as exc:  # noqa: BLE001
        state.note(f"Replay failed to load: {exc}", kind="error")
        return

    rows = [x for x in rows if x.get("date")]
    rows.sort(key=lambda x: x["date"])
    if not rows:
        state.note("Replay found no race control messages", kind="error")
        return

    t0 = datetime.fromisoformat(rows[0]["date"].replace("Z", "+00:00"))
    started = time.monotonic()
    for row in rows:
        ts = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
        target = (ts - t0).total_seconds() / max(speed, 0.1)
        wait = target - (time.monotonic() - started)
        if wait > 0:
            await asyncio.sleep(min(wait, 30))
        handle_topic(state, "RaceControlMessages",
                     {"Messages": [{
                         "Message": row.get("message"),
                         "Flag": row.get("flag"),
                         "Scope": row.get("scope"),
                     }]}, "replay")
    state.note("Replay finished", kind="ok")


# ── Web ──────────────────────────────────────────────────────
async def index(request):
    html = (Path(__file__).parent / "templates" / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def ws_handler(request):
    state = request.app["state"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    state.clients.add(ws)
    await ws.send_str(json.dumps(state.snapshot()))
    await ws.send_str(json.dumps({"type": "backlog",
                                  "entries": list(state.log)}))
    try:
        async for _ in ws:
            pass
    finally:
        state.clients.discard(ws)
    return ws


async def api_flag(request):
    state = request.app["state"]
    body = await request.json()
    flag = body.get("flag")
    if flag == "AUTO":
        state.clear_manual()
        return web.json_response({"ok": True})
    if flag not in Flag.ALL:
        return web.json_response({"ok": False, "error": "unknown flag"},
                                 status=400)
    state.raise_flag(flag, "manual", f"{Flag.LABEL[flag]} — set by hand")
    return web.json_response({"ok": True})


async def api_replay(request):
    state = request.app["state"]
    body = await request.json()
    key = str(body.get("session_key", "")).strip()
    speed = float(body.get("speed", 10))
    if not key:
        return web.json_response({"ok": False, "error": "session_key required"},
                                 status=400)
    old = request.app.get("replay")
    if old and not old.done():
        old.cancel()
    request.app["replay"] = asyncio.create_task(replay_task(state, key, speed))
    return web.json_response({"ok": True})


async def frame_pusher(state):
    """Mirror the strip to any connected phone, so the UI shows exactly
    what the LEDs are doing — including before the hardware exists."""
    period = 1.0 / FRAME_HZ
    while True:
        await asyncio.sleep(period)
        if not state.clients:
            continue
        state._push({"type": "frame", "px": state.renderer.last_frame})


async def on_startup(app):
    state = app["state"]
    state.bind_loop(asyncio.get_running_loop())
    app["tasks"] = [
        asyncio.create_task(frame_pusher(state)),
        asyncio.create_task(openf1_task(state)),
    ]
    if not app["no_live"]:
        app["tasks"].append(asyncio.create_task(signalr_task(state)))
    if app["replay_key"]:
        app["replay"] = asyncio.create_task(
            replay_task(state, app["replay_key"], app["replay_speed"]))


async def on_cleanup(app):
    for t in app.get("tasks", []):
        t.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true",
                    help="render to the terminal instead of a strip")
    ap.add_argument("--serial", nargs="?", const="auto", metavar="PORT",
                    help="stream frames to a Pico over USB serial "
                         "(bare --serial auto-detects the port)")
    ap.add_argument("--list-ports", action="store_true",
                    help="list serial ports and exit")
    ap.add_argument("--no-live", action="store_true",
                    help="skip SignalR (useful when replaying)")
    ap.add_argument("--replay", metavar="SESSION_KEY",
                    help="replay a past session on start")
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=HTTP_PORT)
    args = ap.parse_args()

    if args.list_ports:
        ports = leds.list_serial_ports()
        if not ports:
            print("No serial ports found. Is the Pico plugged in?")
        for dev, desc in ports:
            print(f"  {dev:<28} {desc}")
        guess = leds.find_pico()
        print(f"\nBest guess: {guess}" if guess else "\nNothing looks like a Pico.")
        return

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")

    driver, is_sim = leds.make_driver(simulate=args.sim or None,
                                      serial_port=args.serial)
    renderer = leds.Renderer(driver)
    renderer.start()

    state = State(renderer)
    app = web.Application()
    app["state"] = state
    app["no_live"] = args.no_live
    app["replay_key"] = args.replay
    app["replay_speed"] = args.speed
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/api/flag", api_flag)
    app.router.add_post("/api/replay", api_replay)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    mode = ("simulator" if is_sim
            else "Pico over USB" if args.serial else "WS2812B on GPIO18")
    print(f"\nF1 box up on http://0.0.0.0:{args.port}  "
          f"({leds.LED_COUNT} LEDs, {mode})\n")
    try:
        web.run_app(app, port=args.port, print=None)
    finally:
        renderer.stop()


if __name__ == "__main__":
    main()
