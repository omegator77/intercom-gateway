"""Looking for a newer client and fetching its installer - safely.

Where the releases are: the GitHub releases of omegator77/intercom-gateway, tags `client-v<version>`. A release carries
three files: `intercom-client-<version>-setup.exe`, `SHA256SUMS.txt` (its checksum) and `SHA256SUMS.txt.sig` (an Ed25519
signature of that file). Everything is checked before the installer is offered to the user:

  * the checksum file must carry a valid signature of the release key whose public half is built into this program
    (`PUBLIC_KEY`) - somebody who only gets at the GitHub account cannot hand out a program of their own;
  * the downloaded installer must have exactly the checksum from that file;
  * downloads go over https only, to GitHub's own hosts, with a size limit, into a folder of their own.

Nothing is installed by this module; the window asks the user, and only then starts the installer."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPO = "omegator77/intercom-gateway"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"
TAG_PREFIX = "client-v"
# Public half of the release key (Ed25519, raw, base64). The private half stays with the person who publishes releases
# (dev/release_client.py keygen / sign).
PUBLIC_KEY = "gk6SiHGbcMGVS7nUlxzLR4VC703YoL4IEzJBa8tfdH8="
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
MAX_INSTALLER_BYTES = 400 * 1024 * 1024
MAX_SMALL_BYTES = 64 * 1024
TIMEOUT = httpx.Timeout(15.0, read=60.0)
USER_AGENT = "intercom-client-update-check"


class UpdateError(Exception):
    """The message is fit to show the user."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    notes: str
    installer_name: str
    installer_url: str
    installer_size: int
    sums_url: str
    sig_url: str
    page_url: str


def parse_version(text: str) -> tuple[int, ...]:
    """'0.1.2' / 'v0.1.2' / 'client-v0.1.2' -> (0, 1, 2); raises ValueError for anything else."""
    m = re.fullmatch(r"(?:client-)?v?(\d+(?:\.\d+){0,3})", text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"Der Download-Link zeigt nicht auf GitHub und wird nicht benutzt: {u.scheme}://{u.hostname}")


def _get(client: httpx.Client, url: str, limit: int, *, accept: str | None = None):
    """GET with the redirects checked one by one (GitHub redirects asset downloads to its storage host)."""
    headers = {"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})}
    for _ in range(5):
        _check_url(url)
        request = client.build_request("GET", url, headers=headers)
        response = client.send(request, stream=True)
        if response.is_redirect:
            url = str(response.next_request.url) if response.next_request else response.headers.get("location", "")
            response.close()
            continue
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            response.close()
            raise UpdateError("GitHub nimmt gerade keine weiteren Anfragen an (Limit erreicht). Später noch einmal versuchen.")
        if response.status_code != 200:
            response.close()
            raise UpdateError(f"GitHub antwortet mit HTTP {response.status_code}.")
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            response.close()
            raise UpdateError("Die Datei ist größer als erlaubt und wird nicht geladen.")
        return response
    raise UpdateError("Zu viele Weiterleitungen beim Download.")


def _read_small(client: httpx.Client, url: str, **kw) -> bytes:
    response = _get(client, url, MAX_SMALL_BYTES, **kw)
    try:
        data = b""
        for chunk in response.iter_bytes():
            data += chunk
            if len(data) > MAX_SMALL_BYTES:
                raise UpdateError("Die Datei ist größer als erlaubt und wird nicht geladen.")
        return data
    finally:
        response.close()


def latest_release(current_version: str, client: httpx.Client | None = None) -> UpdateInfo | None:
    """The newest published client release if it is newer than `current_version`, else None."""
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        try:
            data = _read_small(client, RELEASES_URL + "?per_page=30", accept="application/vnd.github+json")
        except httpx.HTTPError as e:
            raise UpdateError(f"GitHub ist nicht erreichbar: {e}") from e
        try:
            releases = json.loads(data)
            best = None
            for r in releases:
                tag = str(r.get("tag_name", ""))
                if r.get("draft") or r.get("prerelease") or not tag.startswith(TAG_PREFIX):
                    continue
                try:
                    version = ".".join(map(str, parse_version(tag)))
                except ValueError:
                    continue
                if best is None or is_newer(version, best[0]):
                    best = (version, r)
            if best is None or not is_newer(best[0], current_version):
                return None
            version, r = best
            assets = {a["name"]: a for a in r.get("assets", [])}
            name = f"intercom-client-{version}-setup.exe"
            if name not in assets or "SHA256SUMS.txt" not in assets or "SHA256SUMS.txt.sig" not in assets:
                raise UpdateError(f"Die Version {version} ist unvollständig veröffentlicht (Installer, Prüfsumme oder Signatur fehlt).")
            for a in (assets[name], assets["SHA256SUMS.txt"], assets["SHA256SUMS.txt.sig"]):
                _check_url(a["browser_download_url"])
            return UpdateInfo(version, str(r.get("body") or "")[:2000], name, assets[name]["browser_download_url"],
                              int(assets[name].get("size") or 0), assets["SHA256SUMS.txt"]["browser_download_url"],
                              assets["SHA256SUMS.txt.sig"]["browser_download_url"], str(r.get("html_url") or ""))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise UpdateError("GitHub hat eine unerwartete Antwort geschickt.") from e
    finally:
        if own:
            client.close()


def verify_signature(public_key_b64: str, data: bytes, signature_b64: str) -> None:
    """Raises UpdateError unless `signature_b64` is a valid Ed25519 signature of `data` for the key."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64.strip(), validate=True), data)
    except (InvalidSignature, ValueError):
        raise UpdateError("Die Signatur der Prüfsummen-Datei stimmt nicht. Das Update wird nicht installiert.") from None


def expected_checksum(sums: bytes, name: str) -> str:
    for line in sums.decode("utf-8", errors="replace").splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line.strip())
        if m and m.group(2) == name:
            return m.group(1).lower()
    raise UpdateError(f"In der Prüfsummen-Datei steht {name} nicht.")


def download_update(info: UpdateInfo, progress: Callable[[int, int], None] | None = None, *,
                    public_key: str | None = None, client: httpx.Client | None = None,
                    dest_dir: Path | None = None) -> Path:
    """Fetch the installer, check signature and checksum, and return the path of the verified file (in a folder created
    for it). Raises UpdateError, with nothing left behind, if anything does not check out."""
    key = public_key or PUBLIC_KEY
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    folder = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="intercom-client-update-"))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / info.installer_name
    partial = folder / (info.installer_name + f".{secrets.token_hex(4)}.part")
    try:
        try:
            sums = _read_small(client, info.sums_url)
            verify_signature(key, sums, _read_small(client, info.sig_url).decode("ascii", errors="replace"))
            want = expected_checksum(sums, info.installer_name)
            digest = hashlib.sha256()
            done = 0
            response = _get(client, info.installer_url, MAX_INSTALLER_BYTES)
            try:
                total = int(response.headers.get("content-length") or info.installer_size or 0)
                with partial.open("wb") as f:
                    for chunk in response.iter_bytes(64 * 1024):
                        done += len(chunk)
                        if done > MAX_INSTALLER_BYTES:
                            raise UpdateError("Die Datei ist größer als erlaubt und wird nicht geladen.")
                        digest.update(chunk)
                        f.write(chunk)
                        if progress:
                            progress(done, total)
            finally:
                response.close()
        except httpx.HTTPError as e:
            raise UpdateError(f"Der Download ist abgebrochen: {e}") from e
        if digest.hexdigest() != want:
            raise UpdateError("Die geladene Datei stimmt nicht mit der Prüfsumme überein (beschädigt oder verändert). "
                              "Sie wird gelöscht.")
        partial.replace(target)
        return target
    except BaseException:
        for p in (partial, target):
            try:
                p.unlink()
            except OSError:
                pass
        raise
    finally:
        if own:
            client.close()
