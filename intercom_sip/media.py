"""The audio of one call: an RTP socket, an encoder/packetizer sending what the lines sound like, and a
depacketizer/decoder speaking what arrives into the lines.

Timing: sending is paced by this machine's clock (every 10 ms the lines' mix is pulled, encoded and the finished
packets go out), receiving runs on the other side's clock and is absorbed by a drift-compensating buffer, exactly
like the gateway handles a sound card. Both directions therefore keep running for days without drift."""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from gateway.audio import DriftBuffer, Level, soft_limit

from .codecs import AudioFormat, Decoder, Encoder
from .lines import LineBinding
from .net import udp_socket
from .rtp import Packetizer, SequenceFilter, depacketize, parse_rtp

log = logging.getLogger("sip.media")

SR = 48000
TICK_S = 0.010
BLOCK = 480  # samples per tick


class CallMedia(asyncio.DatagramProtocol):
    def __init__(self, fmt: AudioFormat, payload_type: int, send: list[LineBinding | None],
                 receive: list[LineBinding | None], playout_ms: int = 60, latch: bool = False):
        """`send[i]` is the line whose sound goes out on codec channel i (None = silence), `receive[i]` the line
        that channel i of the incoming audio is spoken into (None = discarded)."""
        self.fmt = fmt
        self.pt = payload_type
        self.send, self.receive = send, receive
        self._latch = latch
        self._encoder = Encoder(fmt)
        self._decoder = Decoder(fmt)
        self._packetizer = Packetizer(fmt, payload_type)
        self._seq = SequenceFilter()
        self._rx_buf = DriftBuffer(target_ms=playout_ms, max_ms=max(300, playout_ms + 150), channels=2)
        self._transport: asyncio.DatagramTransport | None = None
        self.local_port = 0
        self.remote: tuple[str, int] | None = None
        self._tasks: list[asyncio.Task] = []
        self.tx_level = [Level(), Level()]
        self.rx_level = [Level(), Level()]
        self.tx_packets = 0
        self.rx_packets = 0
        self.rx_foreign = 0  # packets with a payload type we did not agree on (comfort noise, DTMF events, ...)
        self.rx_other_source = 0  # packets from an address the SDP did not name (dropped unless `latch`)
        self.first_rx: float | None = None
        self.last_rx: float | None = None
        self.started = 0.0
        self.latched = False

    # ------------------------------------------------------------ socket
    async def open(self, bind_ip: str, port_min: int, port_max: int) -> int:
        loop = asyncio.get_running_loop()
        last: Exception | None = None
        for port in range(port_min + (port_min % 2), port_max, 2):  # RTP on even ports, the next one is RTCP's
            try:
                sock = udp_socket(bind_ip, port)
            except OSError as e:
                last = e
                continue
            self._transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
            self.local_port = port
            return port
        raise OSError(f"no free RTP port between {port_min} and {port_max}: {last}")

    def start(self, remote: tuple[str, int]) -> None:
        self.remote = remote
        self.started = time.monotonic()
        self._tasks = [asyncio.ensure_future(self._ticker(self._send_tick)),
                       asyncio.ensure_future(self._ticker(self._receive_tick))]

    def retarget(self, remote: tuple[str, int]) -> None:
        self.remote = remote

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._transport:
            self._transport.close()
            self._transport = None

    # ------------------------------------------------------------ clocks
    async def _ticker(self, fn) -> None:
        loop = asyncio.get_running_loop()
        t0, n = loop.time(), 0
        while True:
            n += 1
            delay = t0 + n * TICK_S - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.25:  # the loop was blocked for a while: do not rush to catch up
                t0, n = loop.time(), 0
            try:
                fn()
            except Exception:
                log.exception("media tick failed")
                await asyncio.sleep(0.5)

    # ------------------------------------------------------------ sending
    def _send_tick(self) -> None:
        if self._transport is None or self.remote is None:
            return
        pulled: dict[int, np.ndarray] = {}
        cols = []
        for i in range(self.fmt.channels):
            b = self.send[i] if i < len(self.send) else None
            if b is None:
                x = np.zeros(BLOCK, np.float32)
            else:
                x = pulled.get(id(b))
                if x is None:
                    x = pulled[id(b)] = b.pull(BLOCK)
            self.tx_level[i].update(x)
            cols.append(x)
        block = soft_limit(np.stack(cols, axis=1))
        for datagram in self._packetizer.pack(self._encoder.encode(block)):
            self._transport.sendto(datagram, self.remote)
            self.tx_packets += 1

    # ------------------------------------------------------------ receiving
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        packet = parse_rtp(data)
        if packet is None:
            return
        if packet.pt != self.pt:
            self.rx_foreign += 1
            return
        if not self._latch and self.remote is not None and addr[0] != self.remote[0]:
            self.rx_other_source += 1  # nobody else may speak into the lines (behind NAT the SDP names a private address: set rtp_latch)
            return
        if self._latch and not self.latched and self.remote is not None and addr != self.remote:
            log.info("RTP arrives from %s:%d, not from %s:%d as the SDP said - sending there", *addr, *self.remote)
            self.remote = addr
            self.latched = True
        if not self._seq.accept(packet):
            return
        now = time.monotonic()
        self.first_rx = self.first_rx or now
        self.last_rx = now
        self.rx_packets += 1
        pcm = self._decoder.decode(depacketize(self.fmt, packet))
        if pcm is None:
            return
        self._rx_buf.write(pcm[:, 0] if pcm.shape[1] == 1 else pcm)  # a mono stream is centred (both channels)

    def _receive_tick(self) -> None:
        block = self._rx_buf.read(BLOCK)  # (BLOCK, 2)
        for i in range(2):
            self.rx_level[i].update(block[:, i])
        groups: dict[int, tuple[LineBinding, list[int]]] = {}
        for i in range(min(self.fmt.channels, len(self.receive))):
            b = self.receive[i]
            if b is not None:
                groups.setdefault(id(b), (b, []))[1].append(i)
        for b, chans in groups.values():
            b.push(block[:, chans[0]] if len(chans) == 1 else block[:, chans].mean(axis=1))

    # ------------------------------------------------------------ status
    def silent_for(self) -> float:
        """Seconds since the last packet arrived (since the start if none did yet)."""
        return time.monotonic() - (self.last_rx or self.started)

    def stats(self) -> dict:
        return {
            "codec": self.fmt.describe(),
            "local_port": self.local_port,
            "remote": f"{self.remote[0]}:{self.remote[1]}" if self.remote else None,
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "rx_lost": self._seq.lost,
            "rx_late": self._seq.late,
            "rx_foreign": self.rx_foreign,
            "rx_other_source": self.rx_other_source,
            "rx_fill_ms": round(self._rx_buf.fill_ms, 1),
            "rx_underruns": self._rx_buf.underruns,
            "decode_errors": self._decoder.errors,
            "rx_stream": self._decoder.last_stream,
            "tx_dbfs": [round(l.dbfs, 1) for l in self.tx_level[:self.fmt.channels]],
            "rx_dbfs": [round(l.dbfs, 1) for l in self.rx_level[:self.fmt.channels]],
        }
