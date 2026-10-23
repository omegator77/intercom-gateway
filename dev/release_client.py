"""Prepare a client release for the update check (see intercom_client/updates.py).

    python dev/release_client.py keygen --out C:\\Users\\you\\.intercom-release\\client-signing.key
        Once: makes the signing key. The private key goes into that file (keep it safe and backed up, never in git);
        the public key is printed - it belongs into PUBLIC_KEY in intercom_client/updates.py.

    python dev/release_client.py sign dist\\installer\\intercom-client-0.1.2-setup.exe --key <that file>
        Writes SHA256SUMS.txt and SHA256SUMS.txt.sig next to the installer and says what to upload.

On GitHub: Releases -> Draft a new release, tag `client-v0.1.2`, attach the three files, publish."""
from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

RAW = serialization.Encoding.Raw
RAW_PRIVATE = serialization.PrivateFormat.Raw
RAW_PUBLIC = serialization.PublicFormat.Raw


def public_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(RAW, RAW_PUBLIC)).decode()


def keygen(out: Path) -> int:
    if out.exists():
        print(f"{out} exists already - not overwritten (a new key would make every installed client refuse updates "
              "until it is updated by hand).", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    out.write_text(base64.b64encode(key.private_bytes(RAW, RAW_PRIVATE, serialization.NoEncryption())).decode() + "\n",
                   encoding="ascii")
    print(f"private key written to {out}\nPUBLIC_KEY = \"{public_b64(key)}\"")
    return 0


def load_key(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(path.read_text(encoding="ascii").strip()))


def sign(installer: Path, key_file: Path) -> int:
    m = re.fullmatch(r"intercom-client-(\d+(?:\.\d+){0,3})-setup\.exe", installer.name)
    if not m or not installer.is_file():
        print("expected an installer named intercom-client-<version>-setup.exe", file=sys.stderr)
        return 1
    digest = hashlib.sha256()
    with installer.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    sums = f"{digest.hexdigest()}  {installer.name}\n".encode()
    key = load_key(key_file)
    signature = base64.b64encode(key.sign(sums)).decode()
    sums_path = installer.with_name("SHA256SUMS.txt")
    sums_path.write_bytes(sums)
    installer.with_name("SHA256SUMS.txt.sig").write_text(signature + "\n", encoding="ascii")
    print(f"signed {installer.name} (sha256 {digest.hexdigest()[:16]}…)\n"
          f"upload to a GitHub release with the tag client-v{m.group(1)}:\n  {installer}\n  {sums_path}\n  {sums_path}.sig")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("sign")
    s.add_argument("installer", type=Path)
    s.add_argument("--key", required=True, type=Path)
    a = ap.parse_args()
    return keygen(a.out) if a.cmd == "keygen" else sign(a.installer, a.key)


if __name__ == "__main__":
    sys.exit(main())
