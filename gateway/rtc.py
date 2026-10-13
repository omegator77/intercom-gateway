"""One WebRTC session on one line, speaking the same protocol as the browser frontend:
POST /session (server SDP offer) -> local answer (non-trickle) -> PATCH /session/:id -> heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import httpx
import numpy as np
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcsctptransport import RTCSctpTransport

log = logging.getLogger("gateway.rtc")

API = "/api/v1"

# aiortc derives its SCTP role from the ICE role. The media bridge is ice-controlling, so aiortc
# would wait forever for an SCTP INIT the bridge never sends. Always initiate ourselves.
RTCSctpTransport.is_server = property(lambda self: False)


class SessionError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def strip_extra_fingerprints(sdp: str) -> str:
    """aiortc offers sha-256/384/512 fingerprints per m-line, browsers only sha-256. The manager's
    SDP parser keeps the LAST one and forwards it to the media bridge, which verifies sha-256
    ('Dtls fingerprint mismatch', no media at all). Keep sha-256 only."""
    kept = [l for l in sdp.splitlines() if not l.startswith(("a=fingerprint:sha-384", "a=fingerprint:sha-512"))]
    return "\r\n".join(kept) + "\r\n"


class LineConnection:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        send_track: MediaStreamTrack,
        on_pcm: Callable[[np.ndarray], None],
        ice_servers: tuple[str, ...] = (),
    ):
        self._http = http  # shared with every other line; never closed here
        self._headers = {"Cookie": f"auth_token={token}"}
        self._track = send_track
        self._on_pcm = on_pcm
        cfg = RTCConfiguration(iceServers=[RTCIceServer(urls=u) for u in ice_servers])
        self.pc = RTCPeerConnection(cfg)
        self.session_id: str | None = None
        self.dead = asyncio.Event()  # set when the peer connection fails or closes
        self.connected_ms: float | None = None
        self._pump_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self.frames_in = 0

    async def connect(self, production_id: str, line_id: str, username: str, timeout: float = 30.0) -> str:
        try:
            return await asyncio.wait_for(self._connect(production_id, line_id, username), timeout)
        except asyncio.TimeoutError:
            raise SessionError("timed out establishing the session") from None
        except httpx.HTTPError as e:
            raise SessionError(f"http error while connecting: {e}") from e

    async def _connect(self, production_id: str, line_id: str, username: str) -> str:
        t0 = time.monotonic()
        r = await self._http.post(
            f"{API}/session",
            json={"productionId": production_id, "lineId": line_id, "username": username, "relayType": "mixed"},
            headers=self._headers,
        )
        if r.status_code != 201:
            raise SessionError(f"POST /session -> HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        data = r.json()
        self.session_id = data["sessionId"]

        pc = self.pc
        pc.addTrack(self._track)
        self._dc = pc.createDataChannel("conference-data-channel", ordered=True)  # parity with the browser

        @pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                self._pump_task = asyncio.ensure_future(self._pump(track))

        @pc.on("connectionstatechange")
        async def _on_state():
            log.debug("connection state: %s", pc.connectionState)
            if pc.connectionState == "connected":
                self._connected.set()
            elif pc.connectionState in ("failed", "closed"):
                self.dead.set()

        await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
        await pc.setLocalDescription(await pc.createAnswer())  # aiortc finishes ICE gathering here
        answer = strip_extra_fingerprints(pc.localDescription.sdp)
        r = await self._http.patch(f"{API}/session/{self.session_id}", json={"sdpAnswer": answer}, headers=self._headers)
        if r.status_code not in (200, 204):
            raise SessionError(f"PATCH /session -> HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        await self._connected.wait()
        self.connected_ms = (time.monotonic() - t0) * 1000
        return self.session_id

    async def _pump(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                ch = len(frame.layout.channels)
                pcm = frame.to_ndarray().reshape(-1, ch).astype(np.float32).mean(axis=1) / 32768.0
                self.frames_in += 1
                self._on_pcm(pcm)
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("receive pump failed")
            self.dead.set()

    async def heartbeat(self) -> int:
        r = await self._http.get(f"{API}/heartbeat/{self.session_id}", headers=self._headers)
        return r.status_code

    async def close(self) -> None:
        """Best effort and bounded: shutting down must never hang on a half-dead connection."""
        if self.session_id:
            try:
                await asyncio.wait_for(
                    self._http.delete(f"{API}/session/{self.session_id}", headers=self._headers), 5)
            except Exception:
                pass  # the server expires silent sessions on its own
        if self._pump_task:
            self._pump_task.cancel()
        try:
            await asyncio.wait_for(self.pc.close(), 5)
        except Exception:
            log.warning("closing the peer connection failed or timed out; abandoning it")
