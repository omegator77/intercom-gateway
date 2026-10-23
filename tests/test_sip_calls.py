"""A whole call between two SIP services in one process (no intercom server): the channel mapping of the Mainz
scenario - left = a normal duplex line, right = commentary out / N-1 in - and the call's life cycle."""
import asyncio
import time

import numpy as np
import pytest

from gateway.audio import Level, MixedPlayout, tone_amplitude_near
from intercom_sip import calls as calls_mod
from intercom_sip.calls import CallState, SipService
from intercom_sip.config import ConfigError, parse_sip_config

PW = lambda url, user: "pw"  # noqa: E731


class FakeAudio:
    """What a joined line looks like to the call: sound to listen to, and a sink for what is spoken into it."""

    def __init__(self):
        self.out_buf = MixedPlayout(target_ms=60)
        self.out_level = Level()
        self.pushed: list[np.ndarray] = []
        self.track = self

    def push(self, x):  # the binding calls track.push
        self.pushed.append(np.array(x, copy=True))


def line(name, n):
    return {"name": name, "production_id": "1", "line_id": str(n), "username": name.lower(), "password": "x"}


def config(role, sip_port, rtp_min, extra_call, lines):
    return parse_sip_config({
        "server": {"url": "http://127.0.0.1:1"},
        "sip": {"sip_port": sip_port, "local_ip": "127.0.0.1", "bind_ip": "127.0.0.1", "rtp_port_min": rtp_min,
                "rtp_port_max": rtp_min + 40},
        "line": [line(n, i + 1) for i, n in enumerate(lines)],
        "call": [extra_call],
    }, lookup_password=PW)


@pytest.fixture(autouse=True)
def no_intercom(monkeypatch):
    async def nothing(self):
        return

    monkeypatch.setattr(calls_mod.CallRunner, "_start_lines", nothing)
    monkeypatch.setattr(calls_mod.CallRunner, "_stop_lines", nothing)


async def feed(binding, freq, level=0.3):
    """Play a tone into the line's 'what you hear' buffer, in real time like the intercom would."""
    audio = binding.audio
    n = 0
    t0 = time.monotonic()
    while True:
        t = (np.arange(480) + n * 480) / 48000
        audio.out_buf.write((level * np.sin(2 * np.pi * freq * t)).astype(np.float32))
        n += 1
        await asyncio.sleep(max(0, t0 + n * 0.01 - time.monotonic()))


def heard(binding, seconds=1.0):
    chunks = binding.audio.pushed
    x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    return x[-int(48000 * seconds):]


async def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def attach(service):
    for r in service.runners:
        for b in r.bindings.values():
            b.audio = FakeAudio()


def mainz_pair():
    """Us (dialling): Regie duplex on the left, Kommentar out and N-1 in on the right. SAW (answering): the mirror."""
    saw = config("answer", 0, 41200, {
        "name": "ÜWagen", "mode": "answer", "accept_from": ["127.0.0.1"], "codec": "mp2", "bitrate_kbps": 192,
        "channel_mode": "dual-mono", "playout_ms": 60,
        "left": {"line": "SAW-L"}, "right": {"send": "SAW-N1", "receive": "SAW-KOM"}}, ["SAW-L", "SAW-N1", "SAW-KOM"])
    return saw


async def start_pair(dial_extra=None):
    saw_cfg = mainz_pair()
    saw = SipService(saw_cfg, None)
    await saw.start()
    dial = {"name": "SAW Mainz", "mode": "dial", "target": f"sip:saw@127.0.0.1:{saw.ua.port}", "codec": "mp2",
            "bitrate_kbps": 192, "channel_mode": "dual-mono", "playout_ms": 60, "redial_s": 1,
            "left": {"line": "Regie"}, "right": {"send": "Kommentar", "receive": "N-1"}}
    dial.update(dial_extra or {})
    me_cfg = config("dial", 0, 41000, dial, ["Regie", "Kommentar", "N-1"])
    me = SipService(me_cfg, None)
    await me.start()
    attach(saw)
    attach(me)
    return me, saw


def test_the_mainz_scenario_carries_every_channel_to_the_right_line():
    async def go():
        me, saw = await start_pair()
        run = asyncio.gather(me.run(), saw.run())
        try:
            mine, theirs = me.runners[0], saw.runners[0]
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and theirs.state == CallState.CONNECTED)
            feeders = [asyncio.ensure_future(feed(b, f)) for b, f in (
                (mine.bindings["Regie"], 1000), (mine.bindings["Kommentar"], 3000),
                (theirs.bindings["SAW-L"], 2000), (theirs.bindings["SAW-N1"], 4000))]
            await asyncio.sleep(3.0)
            for f in feeders:
                f.cancel()
            # what SAW hears: left = our Regie line, right = our commentary
            left, right = heard(theirs.bindings["SAW-L"]), heard(theirs.bindings["SAW-KOM"])
            assert tone_amplitude_near(left, 1000) > 0.2 and tone_amplitude_near(left, 3000) < 0.01
            assert tone_amplitude_near(right, 3000) > 0.2 and tone_amplitude_near(right, 1000) < 0.01
            # what we hear: left = SAW's line into our Regie line, right = the N-1 into the N-1 line
            regie, n1 = heard(mine.bindings["Regie"]), heard(mine.bindings["N-1"])
            assert tone_amplitude_near(regie, 2000) > 0.2 and tone_amplitude_near(regie, 4000) < 0.01
            assert tone_amplitude_near(n1, 4000) > 0.2 and tone_amplitude_near(n1, 2000) < 0.01
            # the one-way lines stay one-way: nothing is spoken into the commentary line, nothing is taken from N-1
            assert not mine.bindings["Kommentar"].talks and not mine.bindings["N-1"].listens
            assert mine.bindings["Kommentar"].audio.pushed == [] and mine.bindings["N-1"].audio.out_buf.streams == 0
            st = me.status()["calls"][0]["media"]
            assert st["codec"].startswith("MPEG Layer II 192") and st["rx_lost"] == 0 and st["decode_errors"] == 0
        finally:
            me.stop()
            saw.stop()
            await asyncio.wait_for(run, 10)
            await me.close()
            await saw.close()
        assert mine.state == CallState.STOPPED

    asyncio.run(go())


def test_the_answering_side_is_ready_again_after_the_caller_hangs_up():
    async def go():
        me, saw = await start_pair({"redial": False})
        run_saw = asyncio.ensure_future(saw.run())
        run_me = asyncio.ensure_future(me.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: theirs.state == CallState.CONNECTED)
            me.stop()
            await asyncio.wait_for(run_me, 10)
            assert await wait_for(lambda: theirs.state == CallState.WAITING)
            assert mine.state == CallState.STOPPED and mine.calls_made == 1
        finally:
            saw.stop()
            await asyncio.wait_for(run_saw, 10)
            await me.close()
            await saw.close()

    asyncio.run(go())


def test_a_caller_that_is_not_on_the_list_is_refused_and_the_dialler_tries_again():
    async def go():
        saw_cfg = mainz_pair()
        data = {"name": "ÜWagen", "mode": "answer", "accept_from": ["10.9.9.9"], "codec": "mp2", "bitrate_kbps": 192,
                "channel_mode": "dual-mono", "left": {"line": "SAW-L"}}
        saw = SipService(config("answer", 0, 41200, data, ["SAW-L"]), None)
        await saw.start()
        me_cfg = config("dial", 0, 41000, {"name": "SAW Mainz", "mode": "dial", "redial_s": 1,
                                           "target": f"sip:saw@127.0.0.1:{saw.ua.port}", "codec": "mp2",
                                           "bitrate_kbps": 192, "channel_mode": "dual-mono",
                                           "left": {"line": "Regie"}}, ["Regie"])
        me = SipService(me_cfg, None)
        await me.start()
        run = asyncio.gather(me.run(), saw.run())
        r = me.runners[0]
        try:
            assert await wait_for(lambda: r.last_error and "403" in r.last_error)
            assert r.state in (CallState.BACKOFF, CallState.DIALING, CallState.RINGING)
            assert await wait_for(lambda: r.state == CallState.BACKOFF)
        finally:
            me.stop()
            saw.stop()
            await asyncio.wait_for(run, 10)
            await me.close()
            await saw.close()

    asyncio.run(go())


def test_a_call_without_packets_from_the_other_side_stays_up_unless_a_timeout_is_set():
    async def quiet():
        me, saw = await start_pair()
        run_me = asyncio.ensure_future(me.run())
        run_saw = asyncio.ensure_future(saw.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and theirs.state == CallState.CONNECTED)
            theirs.media._tasks[0].cancel()  # the other side sends nothing any more (a PTT codec, or a crash)
            await asyncio.sleep(4.5)
            assert mine.state == CallState.CONNECTED and mine.calls_made == 1 and mine.media.silent_for() > 3
        finally:
            me.stop()
            saw.stop()
            await asyncio.wait_for(asyncio.gather(run_me, run_saw), 10)
            await me.close()
            await saw.close()

    asyncio.run(quiet())


def test_a_call_without_audio_from_the_other_side_is_hung_up_and_dialled_again():
    async def go():
        me, saw = await start_pair({"rtp_timeout_s": 3})
        run_me = asyncio.ensure_future(me.run())
        run_saw = asyncio.ensure_future(saw.run())
        mine, theirs = me.runners[0], saw.runners[0]
        try:
            assert await wait_for(lambda: mine.state == CallState.CONNECTED and theirs.state == CallState.CONNECTED)
            theirs.media._tasks[0].cancel()  # the other side stops sending (a crashed codec)
            assert await wait_for(lambda: mine.last_error and "no audio" in mine.last_error, 10)
            assert await wait_for(lambda: mine.calls_made >= 2 and mine.state == CallState.CONNECTED, 15)
        finally:
            me.stop()
            saw.stop()
            await asyncio.wait_for(asyncio.gather(run_me, run_saw), 10)
            await me.close()
            await saw.close()

    asyncio.run(go())


# ---------------------------------------------------------------- configuration

def base_cfg(**call):
    c = {"name": "A", "target": "sip:x@192.0.2.1", "left": {"line": "L1"}}
    c.update(call)
    return {"server": {"url": "http://x"}, "line": [line("L1", 1), line("L2", 2)], "call": [c]}


def test_a_quiet_other_side_is_not_hung_up_on_by_default():
    assert parse_sip_config(base_cfg(), lookup_password=PW).calls[0].rtp_timeout_s == 0
    assert parse_sip_config(base_cfg(rtp_timeout_s=30), lookup_password=PW).calls[0].rtp_timeout_s == 30
    with pytest.raises(ConfigError, match="0 .off. or at least 3"):
        parse_sip_config(base_cfg(rtp_timeout_s=1), lookup_password=PW)


def test_config_defaults_and_shorthand():
    cfg = parse_sip_config(base_cfg(right={"send": "L2"}), lookup_password=PW)
    c = cfg.calls[0]
    assert (c.fmt.codec, c.fmt.bitrate_kbps, c.fmt.sample_rate, c.fmt.mode) == ("mp2", 192, 48000, "dual-mono")
    assert [(x.send, x.receive) for x in c.channels] == [("L1", "L1"), ("L2", None)]
    assert c.line_names == ("L1", "L2") and cfg.sip.rtp_port_min == 5004


@pytest.mark.parametrize("change,message", [
    ({"codec": "opus"}, "codec must be one of"),
    ({"bitrate_kbps": 100}, "no 100 kbit/s"),
    ({"channel_mode": "joint"}, "channel_mode"),
    ({"codec": "g722", "right": {"line": "L2"}}, "one channel"),
    ({"left": {"line": "nope"}}, "unknown line"),
    ({"left": {}}, "no line is assigned"),
    ({"target": None}, "target is required"),
    ({"mode": "answer"}, "accept_from"),
    ({"mode": "x"}, "mode must be"),
    ({"left": {"lin": "L1"}}, "unknown key"),
])
def test_config_rejects_what_cannot_work(change, message):
    data = base_cfg()
    data["call"][0].update(change)
    if data["call"][0].get("target") is None:
        del data["call"][0]["target"]
    with pytest.raises(ConfigError, match=message):
        parse_sip_config(data, lookup_password=PW)


def test_a_line_serves_one_call_only():
    data = base_cfg()
    data["call"].append({"name": "B", "target": "sip:y@192.0.2.2", "left": {"line": "L1"}})
    with pytest.raises(ConfigError, match="already used by call 'A'"):
        parse_sip_config(data, lookup_password=PW)


def test_audio_from_an_address_the_sdp_did_not_name_is_ignored_unless_latching_is_on():
    import socket

    from intercom_sip.codecs import AudioFormat, Encoder
    from intercom_sip.lines import LineBinding
    from intercom_sip.media import CallMedia
    from intercom_sip.rtp import Packetizer

    async def go(latch):
        fmt = AudioFormat("g722", 16000, 1, 0, "mono")
        b = LineBinding("L", False, True)
        b.audio = FakeAudio()
        media = CallMedia(fmt, 9, [None], [b], 60, latch=latch)
        port = await media.open("127.0.0.1", 42000, 42040)
        media.start(("127.0.0.2", 5004))  # the SDP named 127.0.0.2, the packets come from 127.0.0.1
        enc, pk = Encoder(fmt), Packetizer(fmt, 9)
        tone = (0.3 * np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000)).astype(np.float32)[:, None]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(0, len(tone), 480):
            for d in pk.pack(enc.encode(tone[i:i + 480])):
                sock.sendto(d, ("127.0.0.1", port))
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        stats = media.stats()
        sock.close()
        await media.close()
        return stats

    ignored = asyncio.run(go(False))
    assert ignored["rx_packets"] == 0 and ignored["rx_other_source"] > 20
    latched = asyncio.run(go(True))
    assert latched["rx_packets"] > 20 and latched["rx_other_source"] == 0


# ---------------------------------------------------------------- registration

class FakeRegistrar(asyncio.DatagramProtocol):
    """Registers whoever logs in with the right digest credentials; remembers what it saw."""

    def __init__(self, user, password):
        self.user, self.password = user, password
        self.bindings: dict[str, int] = {}  # user -> granted expiry
        self.requests: list = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        from intercom_sip.sip import digest_authorization, parse_challenge, parse_message
        m = parse_message(data)
        if m is None or m.method != "REGISTER":
            return
        self.requests.append(m)
        head = "SIP/2.0 {code}\r\nVia: {via}\r\nFrom: {f}\r\nTo: {t};tag=reg\r\nCall-ID: {c}\r\nCSeq: {q}\r\n"
        auth = m.get("authorization")
        if not auth:
            out = (head + 'WWW-Authenticate: Digest realm="sip.local", nonce="n0nce", qop="auth"\r\n'
                   "Content-Length: 0\r\n\r\n").format(code="401 Unauthorized", via=m.get("via"), f=m.get("from"),
                                                       t=m.get("to"), c=m.call_id, q=m.get("cseq"))
        else:
            cnonce = auth.split('cnonce="')[1].split('"')[0]
            good = digest_authorization(parse_challenge('realm="sip.local", nonce="n0nce", qop="auth"'), self.user,
                                        self.password, "REGISTER", m.uri, cnonce=cnonce)
            if good != auth:
                out = (head + "Content-Length: 0\r\n\r\n").format(code="403 Forbidden", via=m.get("via"), f=m.get("from"),
                                                                  t=m.get("to"), c=m.call_id, q=m.get("cseq"))
            else:
                import re
                expires = int(re.search(r"expires=(\d+)", m.get("contact")).group(1))
                granted = min(expires, 120) if expires else 0
                if granted:
                    self.bindings[self.user] = granted
                else:
                    self.bindings.pop(self.user, None)
                out = (head + "Contact: {ct};expires={g}\r\nContent-Length: 0\r\n\r\n").format(
                    code="200 OK", via=m.get("via"), f=m.get("from"), t=m.get("to"), c=m.call_id, q=m.get("cseq"),
                    ct=m.get("contact").split(";")[0], g=granted)
        self.transport.sendto(out.encode(), addr)


def test_registering_with_a_password_keeps_the_binding_and_removes_it_on_stop():
    from intercom_sip.registration import RegisterCfg, Registration
    from intercom_sip.sip import SipEndpoint

    async def go(password):
        loop = asyncio.get_running_loop()
        srv = FakeRegistrar("bridge", "secret")
        transport, _ = await loop.create_datagram_endpoint(lambda: srv, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        ua = SipEndpoint(port=0, advertise_ip="127.0.0.1", bind_ip="127.0.0.1")
        await ua.start()
        reg = Registration(ua, RegisterCfg(f"sip:127.0.0.1:{port}", "bridge", password, "sip.local", expires_s=300))
        task = asyncio.ensure_future(reg.run())
        try:
            if password == "secret":
                assert await wait_for(lambda: reg.state == "registered")
                assert srv.bindings == {"bridge": 120} and reg.granted_s == 120  # the registrar shortened it
                assert reg.address == "sip:bridge@sip.local"
                first_call_ids = {r.call_id for r in srv.requests}
                assert len(first_call_ids) == 1
                reg.stop()
                await asyncio.wait_for(task, 10)
                assert srv.bindings == {} and reg.state == "stopped"  # removed again (expires=0)
            else:
                assert await wait_for(lambda: reg.last_error is not None)
                assert reg.state == "failed" and "403" in reg.last_error and srv.bindings == {}
                reg.stop()
                await asyncio.wait_for(task, 10)
        finally:
            reg.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await ua.close()
            transport.close()

    asyncio.run(go("secret"))
    asyncio.run(go("wrong"))


def test_config_reads_the_accounts_and_ties_calls_to_them():
    cfg = parse_sip_config({
        "server": {"url": "http://x"}, "line": [line("L1", 1), line("L2", 2)],
        "account": [{"name": "ARD Sternpunkt", "registrar": "sip:sip.ard.example:5060", "user": "4711", "password": "pw",
                     "domain": "ard.example"},
                    {"name": "ZDF", "registrar": "sip:zdf.example", "user": "9001", "password": "pw2", "contact_user": "zdf-van"}],
        "call": [{"name": "A", "mode": "answer", "account": "ARD Sternpunkt", "left": {"line": "L1"}},
                 {"name": "B", "account": "ZDF", "target": "12345", "left": {"line": "L2"}}]}, lookup_password=PW)
    ard, zdf = cfg.accounts["ARD Sternpunkt"], cfg.accounts["ZDF"]
    assert (ard.registrar, ard.user, ard.domain, ard.contact_user) == ("sip:sip.ard.example:5060", "4711", "ard.example",
                                                                         "ard-sternpunkt")
    assert zdf.contact_user == "zdf-van" and zdf.domain is None
    assert cfg.calls[0].account == "ARD Sternpunkt" and cfg.calls[0].accept_from == ()
    assert cfg.calls[1].target == "sip:12345@zdf.example"  # a bare number is dialled in the account's domain
    for change, message in (({"call": [{"name": "A", "account": "nope", "target": "1", "left": {"line": "L1"}}]},
                             "unknown account"),
                            ({"account": [{"name": "X", "user": "x"}]}, "registrar is required"),
                            ({"account": [{"name": "X", "registrar": "sip:a", "user": "x"},
                                          {"name": "X", "registrar": "sip:b", "user": "y"}]}, "duplicate account"),
                            ({"account": [{"name": "X y", "registrar": "sip:a", "user": "x"},
                                          {"name": "x-y", "registrar": "sip:b", "user": "y"}]}, "same contact_user")):
        data = {"server": {"url": "http://x"}, "line": [line("L1", 1)],
                "call": [{"name": "A", "target": "sip:x@h", "left": {"line": "L1"}}]}
        data.update(change)
        with pytest.raises(ConfigError, match=message):
            parse_sip_config(data, lookup_password=PW)
