"""The SIP service's building blocks: codecs, RTP payloads, SDP, and the SIP user agent talking to itself."""
import asyncio
import socket

import numpy as np
import pytest

from gateway.audio import tone_amplitude_near
from intercom_sip.codecs import AudioFormat, CodecError, Decoder, Encoder, flag_mp2_mode, validate_format
from intercom_sip.rtp import Packetizer, SequenceFilter, build_rtp, depacketize, parse_rtp
from intercom_sip.sdp import SdpError, build_sdp, choose_codec, parse_sdp, payload_type
from intercom_sip.sip import (Accept, Reject, SipEndpoint, SipError, digest_authorization, header_tag, parse_challenge,
                              parse_message, parse_uri, split_commas)

MP2_DUAL = AudioFormat("mp2", 48000, 2, 192, "dual-mono")
MP2_STEREO = AudioFormat("mp2", 48000, 2, 192, "stereo")


def two_tones(n, left, right, level=0.3):
    t = np.arange(n) / 48000
    return np.stack([level * np.sin(2 * np.pi * left * t), level * np.sin(2 * np.pi * right * t)], axis=1).astype(np.float32)


def roundtrip(fmt, pcm):
    enc, dec = Encoder(fmt), Decoder(fmt)
    chunks, out = [], []
    for i in range(0, len(pcm), 480):
        for c in enc.encode(pcm[i:i + 480]):
            chunks.append(c)
            y = dec.decode(c)
            if y is not None:
                out.append(y)
    return chunks, np.concatenate(out), dec


# ---------------------------------------------------------------- codecs

@pytest.mark.parametrize("fmt", [MP2_DUAL, MP2_STEREO, AudioFormat("mp2", 32000, 2, 128, "stereo")])
def test_layer2_keeps_the_two_channels_apart(fmt):
    _, y, dec = roundtrip(fmt, two_tones(96000, 1000, 3000))
    seg = y[len(y) // 2:len(y) // 2 + 24000]
    assert dec.last_stream == (fmt.sample_rate, 2)
    assert tone_amplitude_near(seg[:, 0], 1000) > 0.25 and tone_amplitude_near(seg[:, 0], 3000) < 0.01
    assert tone_amplitude_near(seg[:, 1], 3000) > 0.25 and tone_amplitude_near(seg[:, 1], 1000) < 0.01


def test_layer2_dual_mono_is_flagged_in_the_frame_header_and_stereo_is_not():
    dual, _, _ = roundtrip(MP2_DUAL, two_tones(24000, 1000, 3000))
    stereo, _, _ = roundtrip(MP2_STEREO, two_tones(24000, 1000, 3000))
    assert {c[3] >> 6 for c in dual} == {0b10} and {c[3] >> 6 for c in stereo} == {0b00}
    assert {len(c) for c in dual} == {576}  # 192 kbit/s at 48 kHz = 576 bytes per 24 ms frame
    assert all(c[0] == 0xFF and c[1] & 0xE0 == 0xE0 for c in dual)


def test_flag_mp2_mode_leaves_mono_frames_and_other_data_alone():
    mono = bytes([0xFF, 0xFD, 0x84, 0xC4]) + b"\x00" * 20
    assert flag_mp2_mode(mono, "dual-mono") == mono
    assert flag_mp2_mode(b"not a frame", "dual-mono") == b"not a frame"
    layer3 = bytes([0xFF, 0xFB, 0x90, 0x04]) + b"\x00" * 20
    assert flag_mp2_mode(layer3, "dual-mono") == layer3


@pytest.mark.parametrize("fmt,size", [(AudioFormat("g722", 16000, 1, 0, "mono"), 160),
                                       (AudioFormat("pcmu", 8000, 1, 0, "mono"), 80),
                                       (AudioFormat("pcma", 8000, 1, 0, "mono"), 80),
                                       (AudioFormat("l16", 48000, 2, 0, "stereo"), 1920)])
def test_other_codecs_carry_a_tone(fmt, size):
    chunks, y, _ = roundtrip(fmt, two_tones(96000, 1000, 1000))
    seg = y[len(y) // 2:len(y) // 2 + 24000]
    assert tone_amplitude_near(seg[:, 0], 1000) > 0.25
    assert len(chunks) > 10


def test_formats_are_checked():
    with pytest.raises(CodecError):
        validate_format(AudioFormat("mp2", 48000, 2, 100, "stereo"))  # no such Layer II bit rate
    with pytest.raises(CodecError):
        validate_format(AudioFormat("mp2", 48000, 2, 32, "dual-mono"))  # 32 kbit/s exists for mono only
    with pytest.raises(CodecError):
        validate_format(AudioFormat("g722", 16000, 1, 0, "dual-mono"))
    with pytest.raises(CodecError):
        validate_format(AudioFormat("opus", 48000, 2))
    with pytest.raises(CodecError):
        validate_format(AudioFormat("mp2", 8000, 2, 192, "stereo"))
    assert validate_format(AudioFormat("mp2", 48000, 1, 128, "mono"))


def test_decoder_survives_garbage_and_lost_data():
    dec = Decoder(MP2_STEREO)
    assert dec.decode(b"") is None
    dec.decode(b"\x00\x01\x02" * 200)  # must not raise
    chunks, _, _ = roundtrip(MP2_STEREO, two_tones(48000, 500, 700))
    del chunks[10:14]  # a hole in the middle
    out = [dec.decode(c) for c in chunks]
    assert sum(len(o) for o in out if o is not None) > 20000


# ---------------------------------------------------------------- RTP

def test_rtp_packet_roundtrip_and_rejections():
    raw = build_rtp(14, 65535, 123456, 0xAABBCCDD, b"hello", marker=True)
    p = parse_rtp(raw)
    assert (p.pt, p.seq, p.ts, p.ssrc, p.marker, p.payload) == (14, 65535, 123456, 0xAABBCCDD, True, b"hello")
    assert parse_rtp(b"\x00" * 20) is None and parse_rtp(b"short") is None
    assert parse_rtp(bytes([0x80, 200]) + b"\x00" * 30) is None  # an RTCP sender report
    padded = bytes([0xA0, 0x09, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]) + b"data" + b"\x00\x00\x03"
    assert parse_rtp(padded).payload == b"data"


def test_layer2_packets_carry_the_mpeg_audio_header_and_the_timestamp_follows_the_samples():
    chunks, _, _ = roundtrip(MP2_DUAL, two_tones(48000, 440, 880))
    pk = Packetizer(MP2_DUAL, 14)
    pkts = [parse_rtp(d) for d in pk.pack(chunks)]
    assert len(pkts) == len(chunks) and all(p.pt == 14 and p.payload[:4] == b"\0\0\0\0" for p in pkts)
    assert all(depacketize(MP2_DUAL, p) == c for p, c in zip(pkts, chunks))
    assert {(b.ts - a.ts) & 0xFFFFFFFF for a, b in zip(pkts, pkts[1:])} == {2160}  # 1152 samples at 48 kHz in 90 kHz ticks
    assert [(b.seq - a.seq) & 0xFFFF for a, b in zip(pkts, pkts[1:])] == [1] * (len(pkts) - 1)


def test_a_layer2_frame_larger_than_a_packet_is_fragmented_and_put_back_together():
    big = AudioFormat("mp2", 32000, 2, 384, "stereo")
    chunks, y, _ = roundtrip(big, two_tones(64000, 440, 880))
    assert len(chunks[2]) > 1200
    pk = Packetizer(big, 14)
    datagrams = pk.pack(chunks)
    pkts = [parse_rtp(d) for d in datagrams]
    assert len(pkts) > len(chunks) and sum(p.marker for p in pkts) == len(chunks)
    dec = Decoder(big)
    got = [dec.decode(depacketize(big, p)) for p in pkts]
    assert sum(len(g) for g in got if g is not None) == len(y)


def test_stream_codecs_are_cut_by_ptime_with_the_right_clock():
    fmt = AudioFormat("g722", 16000, 1, 0, "mono", ptime_ms=20)
    chunks, _, _ = roundtrip(fmt, two_tones(48000, 440, 440))
    pk = Packetizer(fmt, 9)
    pkts = [parse_rtp(d) for d in pk.pack(chunks)]
    assert {len(p.payload) for p in pkts} == {160}
    assert {(b.ts - a.ts) & 0xFFFFFFFF for a, b in zip(pkts, pkts[1:])} == {160}  # G.722's clock is 8 kHz, not 16


def test_sequence_filter_counts_loss_and_drops_duplicates():
    f = SequenceFilter()
    pk = lambda seq, ssrc=1: parse_rtp(build_rtp(14, seq, 0, ssrc, b"x"))  # noqa: E731
    assert [f.accept(pk(s)) for s in (65534, 65535, 0, 0, 3, 2)] == [True, True, True, False, True, False]
    assert (f.received, f.lost, f.late) == (4, 2, 2)
    assert f.accept(pk(10, ssrc=2)) and f.lost == 2  # a restarted sender begins afresh


# ---------------------------------------------------------------- SDP

def test_offer_names_exactly_one_codec_and_parses_back():
    text = build_sdp("192.0.2.1", 5004, MP2_DUAL)
    assert "m=audio 5004 RTP/AVP 14" in text and "a=rtpmap:14 MPA/90000" in text
    r = parse_sdp(text)
    assert (r.address, r.port, r.ptime_ms, r.direction) == ("192.0.2.1", 5004, None, "sendrecv")  # no ptime for Layer II: one frame per packet
    assert [(c.pt, c.name, c.clock) for c in r.codecs] == [(14, "MPA", 90000)]
    assert "L16/48000/2" in build_sdp("192.0.2.1", 5004, AudioFormat("l16", 48000, 2, 0, "stereo"))
    assert payload_type(AudioFormat("l16", 48000, 2, 0, "stereo")) == 96


def test_answering_follows_the_other_sides_first_supported_codec():
    offer = ("v=0\r\no=- 1 1 IN IP4 10.0.0.9\r\ns=-\r\nc=IN IP4 10.0.0.9\r\nt=0 0\r\n"
             "m=audio 6000 RTP/AVP 107 96 9 0 101\r\na=rtpmap:107 opus/48000/2\r\na=rtpmap:96 AAC-LD/48000/2\r\n"
             "a=rtpmap:101 telephone-event/8000\r\na=ptime:40\r\n")
    r = parse_sdp(offer)
    assert [c.name for c in r.codecs] == ["OPUS", "AAC-LD", "G722", "PCMU", "TELEPHONE-EVENT"]
    rc, fmt = choose_codec(r)
    assert rc.pt == 9 and fmt.codec == "g722" and fmt.ptime_ms == 40
    rc, fmt = choose_codec(r, wanted=AudioFormat("pcmu", 8000, 1, 0, "mono"))
    assert rc.pt == 0 and fmt.codec == "pcmu"
    with pytest.raises(SdpError):
        choose_codec(parse_sdp(offer.replace(" 9 0 101", "").replace("107 96", "107 96")))


def test_unusable_sdp_is_refused():
    with pytest.raises(SdpError):
        parse_sdp("v=0\r\nm=video 5000 RTP/AVP 96\r\nc=IN IP4 1.2.3.4\r\n")
    with pytest.raises(SdpError):
        parse_sdp("v=0\r\nm=audio 5000 RTP/AVP 0\r\n")  # no address anywhere
    assert parse_sdp("v=0\r\nc=IN IP4 0.0.0.0\r\nm=audio 5000 RTP/AVP 0\r\na=sendonly\r\n").on_hold


# ---------------------------------------------------------------- SIP messages

def test_message_parsing_handles_compact_names_folding_and_lists():
    raw = (b"INVITE sip:x@h SIP/2.0\r\nv: SIP/2.0/UDP 1.1.1.1:5060;branch=z9hG4bKabc\r\nf: <sip:a@b>;tag=1\r\n"
           b"t: <sip:x@h>\r\ni: callid@x\r\nCSeq: 7 INVITE\r\nRecord-Route: <sip:p1;lr>,<sip:p2;lr>\r\n"
           b"Subject: folded\r\n line\r\nl: 4\r\n\r\nbodyEXTRA")
    m = parse_message(raw)
    assert (m.method, m.uri, m.call_id, m.cseq, m.branch) == ("INVITE", "sip:x@h", "callid@x", (7, "INVITE"), "z9hG4bKabc")
    assert m.get_list("record-route") == ["<sip:p1;lr>", "<sip:p2;lr>"]
    assert m.get("subject") == "folded line" and m.body == b"body"
    again = parse_message(m.encode())
    assert again.body == b"body" and again.call_id == "callid@x" and b"Content-Length: 4" in m.encode()
    assert parse_message(b"garbage") is None
    r = parse_message(b"SIP/2.0 486 Busy Here\r\nCSeq: 1 INVITE\r\n\r\n")
    assert (r.is_response, r.status, r.reason) == (True, 486, "Busy Here")


def test_uri_and_header_helpers():
    u = parse_uri("sip:codec@192.0.2.5:5070;transport=udp")
    assert (u.user, u.host, u.port) == ("codec", "192.0.2.5", 5070)
    assert str(parse_uri("192.0.2.5")) == "sip:192.0.2.5" and parse_uri("Name <sip:z@h:5061>;tag=1").port == 5061
    with pytest.raises(SipError):
        parse_uri("sip:@@")
    assert header_tag('"X" <sip:a@b>;tag=abc123') == "abc123" and header_tag("<sip:a@b>") is None
    assert split_commas('<sip:a,b>;x="1,2", <sip:c>') == ['<sip:a,b>;x="1,2"', "<sip:c>"]


def test_digest_authentication_matches_the_rfc_2617_example():
    chal = parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                           'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = digest_authorization(chal, "Mufasa", "Circle Of Life", "GET", "/dir/index.html", cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header and "qop=auth" in header
    plain = digest_authorization({"realm": "r", "nonce": "n"}, "u", "p", "INVITE", "sip:x")
    assert "qop" not in plain and 'username="u"' in plain


# ---------------------------------------------------------------- the user agent, talking to itself

async def endpoint(**kw):
    ep = SipEndpoint(port=0, advertise_ip="127.0.0.1", bind_ip="127.0.0.1", **kw)
    await ep.start()
    return ep


OFFER = build_sdp("127.0.0.1", 40000, MP2_DUAL, session_id=1)
ANSWER = build_sdp("127.0.0.1", 40002, MP2_DUAL, session_id=1)


def test_a_call_comes_up_both_sides_agree_and_either_side_can_hang_up():
    async def go(hangup_by_caller):
        uas, uac = await endpoint(user="codec"), await endpoint(user="studio")
        seen = []
        accepted = []

        async def decide(call):
            seen.append(call)
            return Accept(ANSWER)

        uas.on_invite = decide
        uas.on_accepted = accepted.append
        progress = []
        d = await uac.invite(f"sip:codec@127.0.0.1:{uas.port}", OFFER, progress=lambda s, r: progress.append(s))
        await asyncio.sleep(0.1)
        assert d.remote_sdp == ANSWER and seen[0].sdp == OFFER and seen[0].remote_ip == "127.0.0.1"
        assert 100 in progress and len(accepted) == 1 and accepted[0].remote_sdp == OFFER
        peer = accepted[0]
        if hangup_by_caller:
            await d.bye()
            await asyncio.wait_for(peer.closed.wait(), 2)
            assert peer.closed_by == "remote" and d.closed_by == "local"
        else:
            await peer.bye()
            await asyncio.wait_for(d.closed.wait(), 2)
            assert d.closed_by == "remote"
        await uac.close()
        await uas.close()

    asyncio.run(go(True))
    asyncio.run(go(False))


def test_a_refused_call_raises_the_status_and_options_are_answered():
    async def go():
        uas, uac = await endpoint(), await endpoint()

        async def busy(call):
            return Reject(486)

        uas.on_invite = busy
        with pytest.raises(SipError) as e:
            await uac.invite(f"sip:x@127.0.0.1:{uas.port}", OFFER)
        assert e.value.status == 486
        assert await uac.options(f"sip:x@127.0.0.1:{uas.port}") == 200
        await uac.close()
        await uas.close()

    asyncio.run(go())


def test_a_lost_invite_is_retransmitted():
    async def go():
        uas, uac = await endpoint(), await endpoint()

        async def ok(call):
            return Accept(ANSWER)

        uas.on_invite = ok
        real = uac._transport.sendto
        sent = []

        def lossy(data, addr=None):
            sent.append(data[:6])
            if len(sent) == 1:
                return  # the first INVITE never reaches the other side
            real(data, addr)

        uac._transport.sendto = lossy
        d = await uac.invite(f"sip:x@127.0.0.1:{uas.port}", OFFER)
        assert sent.count(b"INVITE") >= 2 and d.remote_sdp == ANSWER
        await uac.close()
        await uas.close()

    asyncio.run(go())


def test_a_cancelled_call_is_cancelled_at_the_other_side():
    async def go():
        uas, uac = await endpoint(), await endpoint()
        started, cancelled = asyncio.Event(), []

        async def slow(call):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return Accept(ANSWER)

        uas.on_invite = slow
        t = asyncio.ensure_future(uac.invite(f"sip:x@127.0.0.1:{uas.port}", OFFER))
        await asyncio.wait_for(started.wait(), 2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(0.3)
        assert cancelled == [True]
        await uac.close()
        await uas.close()

    asyncio.run(go())


class FakeServer(asyncio.DatagramProtocol):
    """Answers an INVITE with a digest challenge, then accepts it if the credentials are right."""

    def __init__(self, user, password):
        self.user, self.password, self.seen = user, password, []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        m = parse_message(data)
        if m.method != "INVITE":
            return
        auth = m.get("authorization")
        self.seen.append(auth)
        if not auth:
            r = ("SIP/2.0 401 Unauthorized\r\nVia: {via}\r\nFrom: {f}\r\nTo: {t};tag=srv\r\nCall-ID: {c}\r\nCSeq: {q}\r\n"
                 'WWW-Authenticate: Digest realm="studio", nonce="abc123", qop="auth"\r\nContent-Length: 0\r\n\r\n')
        else:
            good = digest_authorization(parse_challenge('realm="studio", nonce="abc123", qop="auth"'), self.user,
                                        self.password, "INVITE", m.uri, cnonce=auth.split('cnonce="')[1].split('"')[0])
            if good != auth:
                r = ("SIP/2.0 403 Forbidden\r\nVia: {via}\r\nFrom: {f}\r\nTo: {t};tag=srv\r\nCall-ID: {c}\r\nCSeq: {q}\r\n"
                     "Content-Length: 0\r\n\r\n")
            else:
                sdp = ANSWER
                r = ("SIP/2.0 200 OK\r\nVia: {via}\r\nFrom: {f}\r\nTo: {t};tag=srv\r\nCall-ID: {c}\r\nCSeq: {q}\r\n"
                     "Contact: <sip:srv@127.0.0.1:" + str(self.transport.get_extra_info("sockname")[1]) +
                     ">\r\nContent-Type: application/sdp\r\nContent-Length: " + str(len(sdp)) + "\r\n\r\n" + sdp)
        out = r.format(via=m.get("via"), f=m.get("from"), t=m.get("to"), c=m.call_id, q=m.get("cseq"))
        self.transport.sendto(out.encode(), addr)


def test_digest_credentials_are_used_when_the_server_asks_for_them():
    async def go(password, expect):
        loop = asyncio.get_running_loop()
        srv = FakeServer("studio-user", "s3cret")
        transport, _ = await loop.create_datagram_endpoint(lambda: srv, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        uac = await endpoint()
        try:
            if expect == 200:
                d = await uac.invite(f"sip:x@127.0.0.1:{port}", OFFER, auth=("studio-user", password))
                assert d.remote_sdp == ANSWER
            else:
                with pytest.raises(SipError) as e:
                    await uac.invite(f"sip:x@127.0.0.1:{port}", OFFER, auth=("studio-user", password))
                assert e.value.status == expect
            assert srv.seen[0] is None and srv.seen[1]
        finally:
            await uac.close()
            transport.close()

    asyncio.run(go("s3cret", 200))
    asyncio.run(go("wrong", 403))


def test_nobody_answering_ends_with_a_timeout():
    async def go():
        quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        quiet.bind(("127.0.0.1", 0))
        uac = await endpoint()
        try:
            with pytest.raises(SipError) as e:
                await uac.options(f"sip:x@127.0.0.1:{quiet.getsockname()[1]}", timeout=1.2)
            assert e.value.status == 408
        finally:
            await uac.close()
            quiet.close()

    asyncio.run(go())
