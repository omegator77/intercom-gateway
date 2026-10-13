import asyncio, json, sys, logging
import httpx
from gwclient import Peer, ToneTrack, login
logging.basicConfig(level=logging.WARNING)
logging.getLogger("aiortc.rtcsctptransport").setLevel(logging.DEBUG)
async def main(state):
    st = json.load(open(state))
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001", timeout=20) as anon:
        u = st["users"]["alice"]; tok = await login(anon, u["username"], u["password"])
    p = Peer("alice", "http://127.0.0.1:8001", tok, ToneTrack())
    seen = []
    p.pc.on("datachannel", lambda ch: seen.append(("remote-channel", ch.label)))
    try:
        await p.connect(st["productionId"], st["lineId"], "Alice", "mixed")
        await asyncio.sleep(4)
        print("remote-initiated channels:", seen)
        print("sctp channels:", {k: (v.label, v.readyState) for k, v in p.pc.sctp._data_channels.items()})
    finally:
        await p.close()
asyncio.run(asyncio.wait_for(main(sys.argv[1]), 40))
