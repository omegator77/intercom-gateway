"""One WebRTC session on one line, speaking the same protocol as the browser frontend:
POST /session (server SDP offer) -> local answer (non-trickle) -> PATCH /session/:id -> heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import time
import zlib
from typing import Callable

import httpx
import numpy as np
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.codecs.opus import OpusDecoder
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcrtpreceiver import StreamStatistics
from aiortc.rtcsctptransport import RTCSctpTransport

from . import opus as opus_settings

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


class _RxStream:
    """State of one remote talker (one SSRC)."""

    def __init__(self) -> None:
        self.decoder = OpusDecoder()
        self.last_seq: int | None = None
        self.decode_error_logged = False


class LineConnection:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        send_track: MediaStreamTrack,
        on_pcm: Callable[[np.ndarray, int], None],  # (mono float32 PCM, ssrc of the talker)
        ice_servers: tuple[str, ...] = (),
        relay_type: str | None = None,  # None = send no relayType, i.e. behave like the browser frontend
        recorder=None,  # debugrec.Recorder or None
        opus: tuple[str, int] = (opus_settings.DEFAULT_MODE, opus_settings.DEFAULT_KBPS),  # (mode, kbit/s)
        stereo_rx: bool = False,  # hand on_pcm (n, 2) L/R frames instead of a mono downmix
    ):
        self._stereo_rx = stereo_rx
        self._recorder = recorder
        self._opus = opus
        self._relay_type = relay_type
        self._http = http  # shared with every other line; never closed here
        self._headers = {"Cookie": f"auth_token={token}"}
        self._track = send_track
        self._on_pcm = on_pcm
        cfg = RTCConfiguration(iceServers=[RTCIceServer(urls=u) for u in ice_servers])
        self.pc = RTCPeerConnection(cfg)
        self.session_id: str | None = None
        self.dead = asyncio.Event()  # set when the peer connection fails or closes
        self.connected_ms: float | None = None
        self._rx: dict[int, _RxStream] = {}
        self.rx_lost = 0
        self._rx_error_logged = False
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
        body = {"productionId": production_id, "lineId": line_id, "username": username}
        if self._relay_type:
            body["relayType"] = self._relay_type
        r = await self._http.post(f"{API}/session", json=body, headers=self._headers)
        if r.status_code != 201:
            raise SessionError(f"POST /session -> HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        data = r.json()
        self.session_id = data["sessionId"]
        self.offer_sdp = data["sdp"]  # kept for diagnostics and the compatibility check

        pc = self.pc
        sender = pc.addTrack(self._track)
        if opus_settings.install(sender, *self._opus):
            log.info("sending Opus %s mode at %d kbit/s%s", self._opus[0], self._opus[1],
                     ", stereo" if getattr(self._track, "channels", 1) == 2 else "")
        self._dc = pc.createDataChannel("conference-data-channel", ordered=True)  # parity with the browser

        @pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                self._install_receiver(track)

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

    def _install_receiver(self, track) -> None:
        """Take over RTP reception for `track`'s receiver. aiortc's receiver has ONE jitter buffer and ONE
        decoder per m-line, but the media bridge's default (browser) mode forwards several talkers as
        separate SSRCs on that single m-line. So demultiplex by SSRC here: every talker gets its own
        Opus decoder and is played out by its own buffer (audio.MixedPlayout sums them), like a browser."""
        for receiver in self.pc.getReceivers():
            if receiver.track is not track:
                continue

            async def handler(packet, arrival_time_ms, _receiver=receiver):
                try:
                    self._on_rtp(_receiver, packet)
                except Exception:  # an exception here would end aiortc's whole DTLS receive loop
                    if not self._rx_error_logged:
                        self._rx_error_logged = True
                        log.exception("processing an RTP packet failed (further errors are not logged)")

            receiver._handle_rtp_packet = handler

    def _on_rtp(self, receiver, packet) -> None:
        rec = self._recorder
        payload = packet.payload
        if rec:
            rec.packet(len(payload), packet.timestamp, packet.sequence_number, packet.ssrc, packet.payload_type,
                       zlib.crc32(payload))
        if not payload:
            return
        codec = receiver._RTCRtpReceiver__codecs.get(packet.payload_type)  # name-mangled private attribute
        if codec is None or codec.mimeType.lower() != "audio/opus":
            return
        try:  # keep RTCP receiver reports working like aiortc's own handler does
            stats = receiver._RTCRtpReceiver__remote_streams
            if packet.ssrc not in stats:
                stats[packet.ssrc] = StreamStatistics(codec.clockRate)
            stats[packet.ssrc].add(packet)
        except Exception:
            pass

        stream = self._rx.get(packet.ssrc)
        if stream is None:
            stream = self._rx[packet.ssrc] = _RxStream()
            log.info("receiving a new stream (ssrc %d), %d active", packet.ssrc, len(self._rx))
        if stream.last_seq is not None:
            gap = (packet.sequence_number - stream.last_seq) & 0xFFFF
            if gap == 0 or gap >= 0x8000:
                return  # duplicate or late: the playout buffer cannot use it any more
            if gap > 1:
                self.rx_lost += gap - 1
        stream.last_seq = packet.sequence_number
        try:
            frames = stream.decoder.decode(JitterFrame(payload, packet.timestamp))
        except Exception:
            if not stream.decode_error_logged:
                stream.decode_error_logged = True
                log.exception("decoding a packet of ssrc %d failed (further errors on this stream are not logged)",
                              packet.ssrc)
            return
        for frame in frames:
            ch = len(frame.layout.channels)
            chans = frame.to_ndarray().reshape(-1, ch).astype(np.float32) / 32768.0
            pcm = chans.mean(axis=1)
            self.frames_in += 1
            if rec:
                rec.rx(pcm)
            if self._stereo_rx:
                self._on_pcm(chans[:, :2] if ch >= 2 else np.stack([pcm, pcm], axis=1), packet.ssrc)
            else:
                self._on_pcm(pcm, packet.ssrc)

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
        try:
            await asyncio.wait_for(self.pc.close(), 5)
        except Exception:
            log.warning("closing the peer connection failed or timed out; abandoning it")
