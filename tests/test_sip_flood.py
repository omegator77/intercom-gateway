"""What the SIP port does when strangers on the internet send it too much, or garbage: work and memory stay bounded, nothing
raises, the addresses it talks to itself still get through, and everything recovers when the flood stops."""
import asyncio
import logging
import random

from intercom_sip.sip import RateLimiter, Reject, SipEndpoint

INVITE = (
    "INVITE sip:4711@198.51.100.7 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 203.0.113.9:5060;branch=z9hG4bKabc{n}\r\n"
    "Max-Forwards: 70\r\n"
    "From: <sip:attacker@203.0.113.9>;tag=t{n}\r\n"
    "To: <sip:4711@198.51.100.7>\r\n"
    "Call-ID: flood-{n}@203.0.113.9\r\n"
    "CSeq: 1 INVITE\r\n"
    "Contact: <sip:attacker@203.0.113.9:5060>\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 0\r\n\r\n")

OPTIONS = INVITE.replace("INVITE", "OPTIONS").replace("flood-", "opt-")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Wire:
    """Stands in for the UDP socket: records what would be sent."""

    def __init__(self):
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        pass

    def get_extra_info(self, name, default=None):
        return default


async def refuse(call):
    return Reject(403, "Forbidden")


def endpoint(clock):
    ua = SipEndpoint(0, bind_ip="127.0.0.1")
    ua._transport = Wire()
    ua.on_invite = refuse
    ua.limiter = RateLimiter(clock=clock)
    return ua


# ---------------------------------------------------------------- the limiter

def test_one_address_is_held_to_its_bucket_and_gets_it_back_over_time():
    clock = Clock()
    lim = RateLimiter(per_ip_rate=10, per_ip_burst=20, total_rate=1000, total_burst=1000, clock=clock)
    assert sum(lim.allow("203.0.113.1") for _ in range(100)) == 20  # the burst, then nothing
    assert lim.allow("203.0.113.2")  # others are not affected
    clock.t += 1.0
    assert sum(lim.allow("203.0.113.1") for _ in range(100)) == 10  # one second = 10 more
    clock.t += 60.0
    assert sum(lim.allow("203.0.113.1") for _ in range(100)) == 20  # never more than the burst


def test_many_addresses_together_are_held_to_the_total_but_addresses_we_talk_to_are_not():
    clock = Clock()
    lim = RateLimiter(per_ip_rate=10, per_ip_burst=20, total_rate=100, total_burst=200, clock=clock)
    admitted = sum(lim.allow(f"198.51.100.{i % 250}") for i in range(2000))
    assert admitted <= 200  # spoofed sources from everywhere cannot get past the total
    assert not lim.allow("203.0.113.77")  # a stranger now has to wait
    assert lim.allow("203.0.113.88", trusted=True)  # whoever we talk to, does not
    clock.t += 10.0
    assert lim.allow("203.0.113.77")  # and it recovers


def test_the_table_of_sources_stays_bounded_and_forgets_idle_ones():
    clock = Clock()
    lim = RateLimiter(per_ip_rate=10, per_ip_burst=20, total_rate=1e9, total_burst=1e9, max_sources=100, clock=clock)
    for i in range(100):
        assert lim.allow(f"10.0.0.{i}")
    assert not lim.allow("10.1.1.1")  # full of recently active sources: no room for a stranger
    assert lim.allow("10.1.1.2", trusted=True)  # except for one we talk to
    clock.t += 5.0  # all buckets are full again: idle
    assert lim.allow("10.1.1.1") and len(lim._src) <= 100


# ---------------------------------------------------------------- the endpoint under a flood

def test_a_flood_from_spoofed_addresses_costs_bounded_memory_and_work_and_the_peers_still_get_answers():
    async def go():
        clock = Clock()
        ua = endpoint(clock)
        ua._trust("192.0.2.50")  # e.g. the registrar
        for n in range(20000):
            ua.datagram_received(INVITE.format(n=n).encode(), (f"203.0.{(n // 250) % 250}.{n % 250 + 1}", 5060))
        await asyncio.sleep(0.05)
        assert len(ua._server_tx) <= 700 and len(ua._tasks) <= 700  # not 20000 of each
        assert ua.dropped >= 19000
        sent = len(ua._transport.sent)
        ua.datagram_received(OPTIONS.format(n=1).encode(), ("192.0.2.50", 5060))
        assert len(ua._transport.sent) == sent + 1  # the peer was answered in the middle of the flood
        clock.t += 30.0
        ua.datagram_received(OPTIONS.format(n=2).encode(), ("203.0.113.200", 5060))
        assert len(ua._transport.sent) == sent + 2  # and a stranger is served again once it is quiet
        await asyncio.sleep(0.05)

    asyncio.run(go())


def test_one_flooding_address_only_hurts_itself():
    async def go():
        ua = endpoint(Clock())
        for n in range(5000):
            ua.datagram_received(INVITE.format(n=n).encode(), ("203.0.113.9", 5060))
        assert ua.dropped >= 4900
        before = len(ua._transport.sent)
        ua.datagram_received(OPTIONS.format(n=1).encode(), ("203.0.113.10", 5060))
        assert len(ua._transport.sent) == before + 1  # somebody else is served as usual
        await asyncio.sleep(0.05)

    asyncio.run(go())


def test_garbage_and_mangled_messages_raise_nothing_and_leave_the_endpoint_working(caplog):
    async def go():
        rng = random.Random(4711)
        clock = Clock()
        ua = endpoint(clock)
        ua.limiter = RateLimiter(per_ip_rate=1e9, per_ip_burst=1e9, total_rate=1e9, total_burst=1e9, clock=clock)
        base = INVITE.format(n=0).encode()
        for i in range(4000):
            data = bytearray(base)
            kind = i % 5
            if kind == 0:  # flip bytes
                for _ in range(rng.randint(1, 12)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
            elif kind == 1:  # cut it short
                data = data[:rng.randrange(len(data))]
            elif kind == 2:  # a header that is enormous, or empty
                data = data.replace(b"Call-ID:", b"Call-ID: " + b"A" * rng.randint(0, 60000) + b"\r\nX:")
            elif kind == 3:  # pure noise
                data = bytearray(rng.randbytes(rng.randint(0, 1500)))
            else:  # strange numbers
                data = data.replace(b"CSeq: 1", b"CSeq: " + rng.choice([b"-1", b"99999999999999999999", b"x", b""]))
                data = data.replace(b"Content-Length: 0", b"Content-Length: " + rng.choice([b"-5", b"999999", b"abc"]))
            ua.datagram_received(bytes(data), (f"203.0.113.{i % 200 + 1}", rng.randrange(1, 65535)))
        await asyncio.sleep(0.2)
        before = len(ua._transport.sent)
        ua.datagram_received(OPTIONS.format(n=9).encode(), ("203.0.113.250", 5060))
        assert len(ua._transport.sent) == before + 1 and b"200 OK" in ua._transport.sent[-1][0]
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="sip"):
        asyncio.run(go())
    broken = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert broken == []  # a handler that raised would have been logged with its traceback
