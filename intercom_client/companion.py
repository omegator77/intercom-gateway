"""Stream Deck control through Bitfocus Companion.

Eyevinn's Companion module "Eyevinn: Open Intercom" (companion-module-eyevinn-intercom) is a WebSocket *server*
(default port 12345) that accepts ONE client: the intercom web app. This client speaks the same protocol, so the
same Stream Deck buttons work with it:

  client -> module   {"type": "CALLS_STATE_UPDATE", "globalMute": bool, "numberOfCalls": n,
                      "calls": [{"index": 1.., "isInputMuted", "isOutputMuted", "volume", "lineName",
                                 "isSomeoneSpeaking", ...}]}      (whenever something changed)
  module -> client   {"action": "push_to_talk_start", "index": 2}  and the other actions below
  module -> client   {"type": "error", "statusCode": 409}          another client is already connected

The index of a line is its position in the client's list (1 = first card).
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("client.companion")

POLL_S = 0.1  # how often the state is compared with the last one sent
RETRY_MIN_S, RETRY_MAX_S = 2.0, 30.0
BUSY_RETRY_S = 30.0  # the module already has a client (the web app?): try again later
VOLUME_STEP = 5  # percent per button press (the web app steps 5 % of its 0..100 range)


def build_state(snap: dict) -> dict:
    """The engine's snapshot in the shape the Companion module reads."""
    calls = []
    for i, line in enumerate(snap["lines"], start=1):
        off = not line.get("enabled", True)
        calls.append({
            "callId": line["name"],
            "index": i,
            "isInputMuted": off or not line["talking"],
            "isOutputMuted": off or line["listen_mute"],
            "volume": round(line["volume_pct"] / 100.0, 2),
            "lineId": line["line_id"],
            "lineName": line["name"],
            "productionId": line["production_id"],
            "productionName": "",
            "isProgramOutputLine": bool(line.get("program")),
            "isProgramUser": False,
            "isSomeoneSpeaking": bool(line.get("speaking")) and not off,
        })
    return {"type": "CALLS_STATE_UPDATE", "globalMute": bool(snap["global_mute"]), "numberOfCalls": len(calls),
            "calls": calls}


class CompanionLink:
    """Keeps a connection to the Companion module and translates its buttons into engine calls.
    Runs on the engine's event loop."""

    def __init__(self, engine, url: str):
        self._engine = engine
        self.url = url
        self.status = "connecting"  # "connecting", "connected", "busy" or "error: ..."
        self._busy = False
        self._holds: set[str] = set()  # lines whose push-to-talk a button is holding right now

    # ------------------------------------------------------------------ connection

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        delay = RETRY_MIN_S
        while True:
            self._busy = False
            try:
                self.status = "connecting"
                async with connect(self.url, open_timeout=3, ping_interval=20, ping_timeout=20,
                                   max_size=1 << 20) as ws:
                    self.status = "connected"
                    delay = RETRY_MIN_S
                    log.info("connected to the Companion module at %s", self.url)
                    await self._session(ws)
            except asyncio.CancelledError:
                self._release_holds()
                raise
            except Exception as e:
                self.status = f"error: {type(e).__name__}: {e}" if str(e) else f"error: {type(e).__name__}"
                log.debug("Companion link: %s", self.status)
            self._release_holds()  # a held button must not keep talking when the link is gone
            if self._busy:
                self.status = "busy"
                await asyncio.sleep(BUSY_RETRY_S)
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_S)

    async def _session(self, ws) -> None:
        async def send_changes() -> None:
            last = None
            while True:
                text = json.dumps(build_state(self._engine.snapshot()), sort_keys=True)
                if text != last:
                    await ws.send(text)
                    last = text
                await asyncio.sleep(POLL_S)

        sender = asyncio.ensure_future(send_changes())
        try:
            async for message in ws:
                self._on_message(message)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    # ------------------------------------------------------------------ buttons

    def _on_message(self, message) -> None:
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            log.debug("Companion sent something that is not JSON")
            return
        if not isinstance(data, dict):
            return
        if data.get("type") == "error":
            if data.get("statusCode") == 409:
                self._busy = True
                log.warning("the Companion module already has a client connected (%s)", data.get("message", ""))
            return
        action = data.get("action")
        if isinstance(action, str):
            self._handle(action, data.get("index"))

    def _handle(self, action: str, index) -> None:
        eng = self._engine
        if action == "toggle_global_mute":
            eng.toggle_global_mute()
            return
        try:
            n = int(index)
        except (TypeError, ValueError):
            log.debug("Companion action %s without a line number", action)
            return
        if not 1 <= n <= len(eng.order):
            log.debug("Companion action %s for line %s, which does not exist", action, n)
            return
        name = eng.order[n - 1]
        if not eng.line_enabled.get(name, True):
            return  # a disconnected line ignores its buttons
        if action == "push_to_talk_start":
            self._holds.add(name)
            eng.ptt(name, True)
        elif action == "push_to_talk_stop":
            self._holds.discard(name)
            eng.ptt(name, False)
        elif action == "toggle_input_mute":
            if eng.controls[name].talk.transmitting:
                eng.stop_talking(name)
            else:
                eng.toggle_latch(name)  # "unmute" = talk until muted again
        elif action == "toggle_output_mute":
            eng.toggle_listen_mute(name)
        elif action in ("increase_volume", "decrease_volume"):
            step = VOLUME_STEP if action == "increase_volume" else -VOLUME_STEP
            eng.set_volume(name, eng.controls[name].volume_pct + step)
        else:
            log.debug("unknown Companion action %s", action)

    def _release_holds(self) -> None:
        for name in list(self._holds):
            self._engine.ptt(name, False)
        self._holds.clear()
