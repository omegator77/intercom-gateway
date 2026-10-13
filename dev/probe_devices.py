"""Does a device actually run? Opens each stream briefly (output = digital silence, input = counted and
discarded) and reports audio callbacks per second. A healthy 480-frame stream at 48 kHz gives ~100/s;
0 means the device opened but its clock is not ticking (e.g. Dante Virtual Soundcard not started or not
synchronised). Nothing is recorded or played.

    python dev/probe_devices.py "Lautsprecher:output" "DVS Transmit 15-16:output" "DVS Receive 15-16:input"
"""
import sys
import time

import sounddevice as sd

from gateway.audio import match_device


def probe(query: str, kind: str, seconds: float = 3.0) -> None:
    devices, apis = sd.query_devices(), sd.query_hostapis()
    idx = match_device(devices, apis, query, kind)
    counts = {"cb": 0, "flagged": 0}

    def cb_in(indata, frames, time_info, status):
        counts["cb"] += 1
        counts["flagged"] += bool(status)

    def cb_out(outdata, frames, time_info, status):
        outdata.fill(0)
        counts["cb"] += 1
        counts["flagged"] += bool(status)

    stream_cls = sd.InputStream if kind == "input" else sd.OutputStream
    label = f"{devices[idx]['name'][:44]:44} ({kind})"
    try:
        s = stream_cls(device=idx, samplerate=48000, channels=2, dtype="float32", blocksize=480,
                       callback=cb_in if kind == "input" else cb_out)
        t0 = time.monotonic()
        s.start()
        time.sleep(seconds)
        elapsed = time.monotonic() - t0
        active = s.active
        s.stop()
        s.close()
        rate = counts["cb"] / elapsed
        verdict = "RUNNING" if rate > 80 else ("NOT RUNNING (no callbacks)" if counts["cb"] == 0 else "IRREGULAR")
        print(f"  {label}  {rate:6.1f} callbacks/s  flagged {counts['flagged']:3}  active={active}  -> {verdict}")
    except Exception as e:
        print(f"  {label}  ERROR opening: {e}")


for spec in sys.argv[1:]:
    q, k = spec.rsplit(":", 1)
    probe(q, k)
