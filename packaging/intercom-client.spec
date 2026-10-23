# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the intercom client: ONE folder holding both executables, sharing all libraries.
#   IntercomClient.exe   the window (no console window)
#   intercom-client.exe  the same program with a console: --version, --selftest, and log output when something fails
# Build with build-client.ps1 from the repository root.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], ["_cffi_backend"]
# aec_audio_processing loads webrtc-audio-processing-2-1.dll from its own files/ folder: collect_all keeps that layout
for pkg in ("aiortc", "aioice", "av", "pylibsrtp", "sounddevice", "_sounddevice_data", "keyring",
            "aec_audio_processing"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# loaded by name at run time and therefore invisible to PyInstaller's analysis
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += collect_submodules("pynput")  # the Windows keyboard backend is picked by platform
hiddenimports += collect_submodules("websockets")
hiddenimports += ["win32ctypes.pywin32.pywintypes", "win32ctypes.pywin32.win32cred"]

a = Analysis(
    [str(ROOT / "packaging" / "client_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)

# a.scripts also holds PyInstaller's runtime hooks (they must run first), so pick the entry script by name
hooks = [s for s in a.scripts if s[0] != "client_entry"]
main_script = [s for s in a.scripts if s[0] == "client_entry"]
assert main_script, f"entry script missing from {[s[0] for s in a.scripts]}"

gui = EXE(pyz, hooks + main_script, [], exclude_binaries=True, name="IntercomClient", console=False)
cli = EXE(pyz, hooks + main_script, [], exclude_binaries=True, name="intercom-client", console=True)

coll = COLLECT(gui, cli, a.binaries, a.datas, name="IntercomClient")
