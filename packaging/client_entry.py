"""Entry point of the packaged intercom client (IntercomClient.exe, and intercom-client.exe with a console).

Started normally it opens the window. `--selftest` loads everything the packaged app depends on (the native
audio libraries, the keyboard hook module, the WebSocket and WebRTC stacks, Qt) and runs a little audio through the
echo/noise processor and the level control. It prints one line per check and exits 1 if anything is missing; the
build script runs it on the finished app, because a forgotten library only shows up when the app is frozen."""
import os
import sys
import tempfile


def selftest() -> int:
    failures = []

    def check(name, fn):
        try:
            detail = fn()
            print(f"  ok   {name}" + (f"  ({detail})" if detail else ""))
        except Exception as e:  # report every missing piece, not just the first
            failures.append(name)
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    def stack():
        import aioice  # noqa: F401
        import aiortc  # noqa: F401
        import av  # noqa: F401
        import httpx  # noqa: F401
        import numpy  # noqa: F401
        import websockets.asyncio.client  # noqa: F401

    def audio_devices():
        import sounddevice as sd

        return f"PortAudio {len(sd.query_hostapis())} host APIs"

    def hotkeys():
        from pynput import keyboard  # noqa: F401  (loads the Windows backend; no hook is installed)

        from intercom_client.hotkeys import HotkeyListener, parse_combo

        HotkeyListener({"x": parse_combo("ctrl+f1")}, lambda a, e: None)

    def credentials():
        import keyring

        return type(keyring.get_keyring()).__name__

    def processing():
        import numpy as np

        from intercom_client.agc import SpeechLeveller
        from intercom_client.processing import FRAME, MicProcessor

        block = (0.1 * np.sin(np.arange(FRAME) / 7.0)).astype(np.float32)
        proc = MicProcessor(True, 2)
        proc.reverse(block)
        out = proc.process(block)
        level = SpeechLeveller().process(block)
        assert len(out) == FRAME and len(level) == FRAME

    def updates():
        import base64

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from intercom_client import updates as up

        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                               serialization.PublicFormat.Raw)).decode()
        up.verify_signature(public, b"x", base64.b64encode(key.sign(b"x")).decode())
        up.parse_version("client-v0.1.0")
        base64.b64decode(up.PUBLIC_KEY, validate=True)  # the built-in release key is a well-formed key

    def window():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from pathlib import Path

        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication

        from intercom_client.ui.settings import ClientSettingsDialog
        from intercom_client.ui.window import MainWindow

        app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp())
        win = MainWindow(tmp, tmp / "client.toml", enable_hotkeys=False, autostart=False)
        dlg = ClientSettingsDialog(tmp / "client.toml", win, device_lists=([], []))
        # Qt widgets must be deleted while the application still exists. Left to the interpreter's exit they are torn
        # down in a random order, which crashed this process about every second run.
        dlg.deleteLater()
        win.close()
        win.deleteLater()
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)

    from intercom_client import __version__

    print(f"intercom-client {__version__} selftest")
    skip = {k for k in os.environ.get("SELFTEST_SKIP", "").split(",") if k}  # for debugging: e.g. SELFTEST_SKIP=window
    for key, name, fn in (("stack", "WebRTC, HTTP and WebSocket libraries", stack),
                          ("audio", "audio devices (PortAudio)", audio_devices),
                          ("hotkeys", "global hotkeys", hotkeys), ("creds", "Windows credential store", credentials),
                          ("proc", "echo / noise processing and level control", processing),
                          ("updates", "update check (signatures)", updates),
                          ("window", "window and settings dialog", window)):
        if key not in skip:
            check(name, fn)
    print("selftest ok" if not failures else f"selftest FAILED: {', '.join(failures)}")
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest()
    from intercom_client.ui.app import main as ui_main

    return ui_main()


if __name__ == "__main__":
    sys.exit(main())
