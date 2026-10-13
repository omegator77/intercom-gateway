"""Audio device lists for the settings dialog (WASAPI only, like the gateway itself)."""
from __future__ import annotations


def _wasapi(kind: str) -> list[tuple[str, int]]:
    import sounddevice as sd

    apis = sd.query_hostapis()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out, seen = [], set()
    for d in sd.query_devices():
        if apis[d["hostapi"]]["name"] != "Windows WASAPI" or d[key] <= 0 or d["name"] in seen:
            continue
        seen.add(d["name"])
        out.append((d["name"], int(d[key])))
    return out


def refresh() -> None:
    """PortAudio caches the device list; re-initialise to see devices plugged in since start (best effort)."""
    try:
        import sounddevice as sd

        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def list_devices() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """([(name, channels)] capture, [(name, channels)] playback); empty lists when PortAudio is unavailable."""
    try:
        return _wasapi("input"), _wasapi("output")
    except Exception:
        return [], []
