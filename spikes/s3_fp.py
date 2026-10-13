import asyncio, json, sys, logging
import httpx
from gwclient import Peer, ToneTrack, login
logging.getLogger("aiortc").setLevel(logging.WARNING)
async def main(state):
    st = json.load(open(state))
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001", timeout=20) as anon:
        u = st["users"]["alice"]; tok = await login(anon, u["username"], u["password"])
    p = Peer("alice", "http://127.0.0.1:8001", tok, ToneTrack())
    try:
        await p.connect(st["productionId"], st["lineId"], "Alice", "mixed")
        print("ANSWER fingerprint/setup lines:")
        for l in p.answer_sdp.splitlines():
            if l.startswith(("a=fingerprint", "a=setup", "m=", "a=ice-ufrag")):
                print("   ", l[:110])
        print("OFFER fingerprint/setup lines:")
        for l in p.offer_sdp.splitlines():
            if l.startswith(("a=fingerprint", "a=setup", "m=")):
                print("   ", l[:110])
    finally:
        await p.close()
asyncio.run(asyncio.wait_for(main(sys.argv[1]), 40))
