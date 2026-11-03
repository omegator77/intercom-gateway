"""A call of the bridge to itself (two of its accounts), passed on by a registrar that keeps the call-id: both ends of the
call then have the same call-id. A message must find the end it is meant for by the tags, not by the call-id alone -
otherwise the ACK of the answered end goes to the calling end and the answered end hangs up after 32 s ("no ACK")."""
import asyncio

from intercom_sip.sip import Accept, Dialog, SipEndpoint

PEER = ("127.0.0.1", 5070)
SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 5004 RTP/AVP 14\r\n"


class Wire:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)

    def close(self):
        pass

    def get_extra_info(self, name, default=None):
        return default


def request(method, call_id, cseq, to_tag=None, from_tag="remote1"):
    to = "<sip:b@127.0.0.1>" + (f";tag={to_tag}" if to_tag else "")
    return (f"{method} sip:b@127.0.0.1:5060 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK{method}{cseq}\r\n"
            f"Max-Forwards: 70\r\nFrom: <sip:a@127.0.0.1>;tag={from_tag}\r\nTo: {to}\r\nCall-ID: {call_id}\r\n"
            f"CSeq: {cseq} {method}\r\nContact: <sip:a@127.0.0.1:5070>\r\nContent-Length: 0\r\n\r\n").encode()


async def answered_end(call_id="same-id@proxy"):
    """An endpoint that answered an incoming call (the called end of the self-call)."""
    ua = SipEndpoint(0, bind_ip="127.0.0.1")
    ua._transport = Wire()

    async def accept(call):
        return Accept(SDP)

    ua.on_invite = accept
    ua.datagram_received(request("INVITE", call_id, 1).replace(b"Content-Length: 0", b"Content-Type: application/sdp\r\nContent-Length: 0"), PEER)
    for _ in range(50):
        if ua._dialogs:
            break
        await asyncio.sleep(0.01)
    (uas,) = ua._dialogs.values()
    return ua, uas


def calling_end(ua, call_id="same-id@proxy"):
    """What `invite()` leaves behind when the call we placed is answered: a dialog under the same call-id."""
    uac = Dialog(ua, call_id, "sip:a@127.0.0.1", "sip:b@127.0.0.1", "ourtag", "remote2", "sip:b@127.0.0.1:5070", [],
                 "uac", 1, 0, "127.0.0.1", PEER)
    ua._dialogs[(call_id, uac.local_tag)] = uac
    return uac


def stop(ua):
    for t in list(ua._tasks):
        t.cancel()


def test_the_ack_reaches_the_answered_end_even_when_the_calling_end_has_the_same_call_id():
    async def go():
        ua, uas = await answered_end()
        uac = calling_end(ua)  # registered after the answered end: with the call-id as the only key it replaced it
        assert len(ua._dialogs) == 2
        ua.datagram_received(request("ACK", "same-id@proxy", 1, to_tag=uas.local_tag), PEER)
        assert uas._acked.is_set() and not uac._acked.is_set()
        stop(ua)

    asyncio.run(go())


def test_a_bye_ends_only_the_end_it_is_for():
    async def go():
        ua, uas = await answered_end()
        uac = calling_end(ua)
        ua.datagram_received(request("BYE", "same-id@proxy", 7, to_tag=uas.local_tag), PEER)  # the older end first
        assert uas.closed.is_set() and not uac.closed.is_set()
        assert list(ua._dialogs.values()) == [uac]  # the other end carries on
        ua.datagram_received(request("BYE", "same-id@proxy", 8, to_tag=uac.local_tag), PEER)
        assert uac.closed.is_set() and ua._dialogs == {}
        assert all(b"200 OK" in m for m in ua._transport.sent[-2:])
        stop(ua)

    asyncio.run(go())


def test_a_peer_that_leaves_the_tag_out_is_still_found_unless_it_is_ambiguous():
    async def go():
        ua, uas = await answered_end()
        ua.datagram_received(request("ACK", "same-id@proxy", 1), PEER)  # one call with this id: no tag needed
        assert uas._acked.is_set()
        calling_end(ua)
        sent = len(ua._transport.sent)
        ua.datagram_received(request("BYE", "same-id@proxy", 9), PEER)  # two of them and no tag: which one?
        assert not uas.closed.is_set() and b"481" in ua._transport.sent[sent]
        stop(ua)

    asyncio.run(go())


def test_audio_for_a_peer_at_our_own_public_address_stays_inside_the_bridge():
    from types import SimpleNamespace

    from intercom_sip.calls import CallRunner

    runner = CallRunner.__new__(CallRunner)  # only the address rule is under test
    runner._sip = SimpleNamespace(local_ip="188.68.41.204")
    assert runner._rtp_address("188.68.41.204") == "127.0.0.1"  # the registrar passed our own SDP back to us
    assert runner._rtp_address("91.195.79.253") == "91.195.79.253"  # anyone else is sent to as the SDP says
    runner._sip = SimpleNamespace(local_ip=None)
    assert runner._rtp_address("188.68.41.204") == "188.68.41.204"  # without a configured address there is nothing to compare


def test_two_different_codecs_calling_at_once_each_get_their_own_ack_even_without_tags():
    async def go():
        ua = SipEndpoint(0, bind_ip="127.0.0.1")
        ua._transport = Wire()

        async def accept(call):
            return Accept(SDP)

        ua.on_invite = accept
        for cid, port in (("codec-one@10.0.0.1", 5070), ("codec-two@10.0.0.2", 5072)):
            data = request("INVITE", cid, 1).replace(b"Content-Length: 0", b"Content-Type: application/sdp\r\nContent-Length: 0")
            ua.datagram_received(data, ("127.0.0.1", port))
        for _ in range(50):
            if len(ua._dialogs) == 2:
                break
            await asyncio.sleep(0.01)
        one = next(v for (cid, _), v in ua._dialogs.items() if cid == "codec-one@10.0.0.1")
        two = next(v for (cid, _), v in ua._dialogs.items() if cid == "codec-two@10.0.0.2")
        ua.datagram_received(request("ACK", "codec-two@10.0.0.2", 1), ("127.0.0.1", 5072))  # a device that sends no To tag
        assert two._acked.is_set() and not one._acked.is_set()
        ua.datagram_received(request("ACK", "codec-one@10.0.0.1", 1, to_tag=one.local_tag), ("127.0.0.1", 5070))
        assert one._acked.is_set()
        ua.datagram_received(request("BYE", "codec-one@10.0.0.1", 2, to_tag=one.local_tag), ("127.0.0.1", 5070))
        assert one.closed.is_set() and not two.closed.is_set()
        stop(ua)

    asyncio.run(go())
