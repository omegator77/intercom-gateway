# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: ONE folder holding both executables, sharing all libraries.
#   IntercomGateway.exe   desktop app (no console window)
#   intercom-gateway.exe  command line tool (run, check, devices, status, stop)
# Build with build.ps1 from the repository root.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], ["_cffi_backend"]
for pkg in ("aiortc", "aioice", "av", "pylibsrtp", "sounddevice", "_sounddevice_data", "keyring"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# keyring picks its backend through entry points, which PyInstaller does not follow
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += ["win32ctypes.pywin32.pywintypes", "win32ctypes.pywin32.win32cred"]

a = Analysis(
    [str(ROOT / "packaging" / "gui_entry.py"), str(ROOT / "packaging" / "cli_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)

# a.scripts also holds PyInstaller's runtime hooks (they must run first), so pick the entry scripts by name
ENTRIES = ("gui_entry", "cli_entry")
hooks = [s for s in a.scripts if s[0] not in ENTRIES]
gui_main = [s for s in a.scripts if s[0] == "gui_entry"]
cli_main = [s for s in a.scripts if s[0] == "cli_entry"]
assert gui_main and cli_main, f"entry scripts missing from {[s[0] for s in a.scripts]}"

gui = EXE(pyz, hooks + gui_main, [], exclude_binaries=True, name="IntercomGateway", console=False)
cli = EXE(pyz, hooks + cli_main, [], exclude_binaries=True, name="intercom-gateway", console=True)

coll = COLLECT(gui, cli, a.binaries, a.datas, name="IntercomGateway")
