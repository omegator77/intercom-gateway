"""Spike helpers: speak the browser's session protocol from Python (aiortc).

login -> POST /session (server SDP offer) -> local answer (non-trickle) ->
PATCH /session/:id -> heartbeat. No sound card involved: audio is synthetic.
"""
import asyncio
import fractions
import re
import time

import av
import httpx
import numpy as np
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcsctptransport import RTCSctpTransport

# aiortc derives the SCTP role from the ICE role. SMB is ice-controlling, so aiortc
# would wait passively for an INIT that SMB never sends (it expects the browser
# side to initiate). Always initiate ourselves.
RTCSctpTransport.is_server = property(lambda self: False)

SR = 48000
PTIME = 0.02
SPF = int(SR * PTIME)  # 960 samples per 20 ms frame
API = "/api/v1"


class ToneTrack(MediaStreamTrack):
    """Sends silence until .freq is set, then a sine of that frequency."""

    kind = "audio"

    def __init__(self, amp: int = 8000):
        super().__init__()
        self.freq: float | None = None
        self.amp = amp
        self._n = 0
        self._start: float | None = None
        self.first_tone_t: float | None = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        delay = self._start + self._n * PTIME - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if self.freq:
            t = (np.arange(SPF) + self._n * SPF) / SR
            samples = (self.amp * np.sin(2 * np.pi * self.freq * t)).astype(np.int16)
            if self.first_tone_t is None:
                self.first_tone_t = time.monotonic()
        else:
            samples = np.zeros(SPF, np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SR
        frame.pts = self._n * SPF
        frame.time_base = fractions.Fraction(1, SR)
        self._n += 1
        return frame


class Sink:
    """Collects decoded frames (left channel) with their arrival time."""

    def __init__(self):
        self.frames: list[tuple[float, np.ndarray]] = []
        self.ssrc_note = ""

    async def run(self, track):
        try:
            while True:
                f = await track.recv()
                ch = len(f.layout.channels)
                mono = f.to_ndarray().reshape(-1, ch)[:, 0].copy()
                self.frames.append((time.monotonic(), mono))
        except (MediaStreamError, asyncio.CancelledError):
            pass

    def window(self, t0: float, t1: float) -> np.ndarray:
        parts = [a for t, a in self.frames if t0 <= t < t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)


def tone_amp(x: np.ndarray, freq: float) -> float:
    """Amplitude estimate of a sine at `freq` (single-bin DFT, Hann window)."""
    n = len(x)
    if n < SR // 10:
        return 0.0
    w = np.hanning(n)
    c = np.exp(-2j * np.pi * freq * np.arange(n) / SR)
    return float(2 * abs(np.dot(x.astype(np.float64) * w, c)) / np.sum(w))


async def login(client: httpx.AsyncClient, user: str, pw: str) -> str:
    r = await client.post(f"{API}/auth/login", json={"username": user, "password": pw})
    r.raise_for_status()
    # Cookie is Secure+SameSite=Strict; a plain-http jar would refuse to send it,
    # so carry the token explicitly (also what a real gateway should do).
    m = re.search(r"auth_token=([^;]+)", ",".join(r.headers.get_list("set-cookie")))
    if not m:
        raise RuntimeError("no auth_token cookie in login response")
    return m.group(1)


class Peer:
    def __init__(self, name: str, base: str, token: str, track: ToneTrack):
        self.name = name
        self.http = httpx.AsyncClient(base_url=base, headers={"Cookie": f"auth_token={token}"}, timeout=15)
        self.track = track
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.sink = Sink()
        self.session_id: str | None = None
        self.dc_messages: list[str] = []
        self.dc_open = False
        self._connected = asyncio.Event()
        self._sink_task: asyncio.Task | None = None
        self.timings: dict[str, float] = {}

    async def connect(self, production_id: str, line_id: str, username: str, relay_type: str | None):
        t0 = time.monotonic()
        body = {"productionId": production_id, "lineId": line_id, "username": username}
        if relay_type:
            body["relayType"] = relay_type
        r = await self.http.post(f"{API}/session", json=body)
        r.raise_for_status()
        data = r.json()
        self.session_id = data["sessionId"]
        self.offer_sdp = data["sdp"]
        self.timings["offer_ms"] = (time.monotonic() - t0) * 1000

        pc = self.pc
        pc.addTrack(self.track)
        dc = pc.createDataChannel("conference-data-channel", ordered=True)

        @dc.on("open")
        def _open():
            self.dc_open = True

        @dc.on("message")
        def _msg(m):
            self.dc_messages.append(str(m))

        @pc.on("track")
        def _track(track):
            if track.kind == "audio":
                self._sink_task = asyncio.ensure_future(self.sink.run(track))

        @pc.on("connectionstatechange")
        async def _state():
            if pc.connectionState == "connected":
                self._connected.set()

        await pc.setRemoteDescription(RTCSessionDescription(self.offer_sdp, "offer"))
        await pc.setLocalDescription(await pc.createAnswer())  # aiortc gathers fully here
        # aiortc offers sha-256/384/512 fingerprints per m-line, browsers only sha-256.
        # The manager's SDP parser keeps the LAST one (sha-512) and forwards it to SMB,
        # which verifies sha-256 -> "Dtls fingerprint mismatch". Send sha-256 only.
        self.answer_sdp = "\r\n".join(
            line for line in pc.localDescription.sdp.splitlines()
            if not line.startswith(("a=fingerprint:sha-384", "a=fingerprint:sha-512"))
        ) + "\r\n"
        self.timings["answer_ready_ms"] = (time.monotonic() - t0) * 1000
        r = await self.http.patch(f"{API}/session/{self.session_id}", json={"sdpAnswer": self.answer_sdp})
        if r.status_code not in (200, 204):
            raise RuntimeError(f"PATCH answer failed: {r.status_code} {r.text[:200]}")
        await asyncio.wait_for(self._connected.wait(), 20)
        self.timings["connected_ms"] = (time.monotonic() - t0) * 1000

    async def heartbeat(self) -> int:
        r = await self.http.get(f"{API}/heartbeat/{self.session_id}")
        return r.status_code

    async def close(self):
        try:
            if self.session_id:
                await self.http.delete(f"{API}/session/{self.session_id}")
        except Exception:
            pass
        await self.pc.close()
        await self.http.aclose()
