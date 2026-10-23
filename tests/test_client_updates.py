"""Looking for a newer client and fetching its installer: only signed, checked files from GitHub's own hosts."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from intercom_client import updates
from intercom_client.updates import UpdateError, UpdateInfo

INSTALLER = b"MZ" + b"installer-bytes" * 5000
HOST = "https://github.com/omegator77/intercom-gateway/releases/download"


def keypair():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)).decode()
    return key, public


def release(tag, assets=True, **extra):
    version = tag.replace("client-v", "")
    names = [f"intercom-client-{version}-setup.exe", "SHA256SUMS.txt", "SHA256SUMS.txt.sig"] if assets else []
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": "Neu: Puffer", "html_url": f"https://github.com/x/{tag}",
            "assets": [{"name": n, "size": len(INSTALLER), "browser_download_url": f"{HOST}/{tag}/{n}"} for n in names], **extra}


def serving(releases, files=None, status=None):
    """A transport that answers the release list and the downloads; `files` maps a URL path to bytes."""
    files = files or {}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if status:
            return httpx.Response(status, headers=status_headers.get(status, {}))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=releases)
        data = files.get(request.url.path)
        if isinstance(data, httpx.Response):
            return data
        return httpx.Response(200, content=data, headers={"content-length": str(len(data))}) if data is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


status_headers = {403: {"x-ratelimit-remaining": "0"}}


def signed_files(tag, key, content=INSTALLER, name=None):
    version = tag.replace("client-v", "")
    name = name or f"intercom-client-{version}-setup.exe"
    sums = f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode()
    base = f"/omegator77/intercom-gateway/releases/download/{tag}/"
    return {base + name: content, base + "SHA256SUMS.txt": sums,
            base + "SHA256SUMS.txt.sig": base64.b64encode(key.sign(sums))}


# ---------------------------------------------------------------- versions

def test_versions_compare_numerically_and_tags_are_read():
    assert updates.parse_version("client-v0.1.12") == (0, 1, 12) and updates.parse_version("v2") == (2,)
    assert updates.is_newer("0.1.10", "0.1.9") and updates.is_newer("1.0", "0.9.9") and updates.is_newer("0.2", "0.1.9")
    assert not updates.is_newer("0.1.2", "0.1.2") and not updates.is_newer("0.1.1", "0.1.2") and not updates.is_newer("0.1", "0.1.0")
    for bad in ("latest", "client-vX", "1.2.3.4.5", ""):
        with pytest.raises(ValueError):
            updates.parse_version(bad)


# ---------------------------------------------------------------- looking for a release

def test_the_newest_client_release_is_found_and_everything_else_is_ignored():
    releases = [release("client-v0.1.3", draft=True), release("client-v0.1.4", prerelease=True),
                release("gateway-v9.9.9"), release("client-v0.1.2"), release("client-v0.1.10"), release("v5.0")]
    client, seen = serving(releases)
    info = updates.latest_release("0.1.3", client)
    assert info.version == "0.1.10" and info.installer_name == "intercom-client-0.1.10-setup.exe"
    assert info.notes == "Neu: Puffer" and info.sums_url.endswith("SHA256SUMS.txt") and info.sig_url.endswith(".sig")
    assert seen[0].startswith("https://api.github.com/repos/omegator77/intercom-gateway/releases")
    assert updates.latest_release("0.1.10", client) is None and updates.latest_release("0.2.0", client) is None


def test_an_unfinished_release_and_a_foreign_download_link_are_refused():
    client, _ = serving([release("client-v0.1.5", assets=False)])
    with pytest.raises(UpdateError, match="unvollständig"):
        updates.latest_release("0.1.0", client)
    evil = release("client-v0.1.5")
    evil["assets"][0]["browser_download_url"] = "https://evil.example/intercom-client-0.1.5-setup.exe"
    client, _ = serving([evil])
    with pytest.raises(UpdateError, match="nicht auf GitHub"):
        updates.latest_release("0.1.0", client)
    plain = release("client-v0.1.5")
    plain["assets"][0]["browser_download_url"] = plain["assets"][0]["browser_download_url"].replace("https", "http")
    client, _ = serving([plain])
    with pytest.raises(UpdateError, match="nicht auf GitHub"):
        updates.latest_release("0.1.0", client)


def test_problems_with_github_become_readable_messages():
    client, _ = serving([], status=403)
    with pytest.raises(UpdateError, match="Limit erreicht"):
        updates.latest_release("0.1.0", client)
    client, _ = serving([], status=500)
    with pytest.raises(UpdateError, match="HTTP 500"):
        updates.latest_release("0.1.0", client)

    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(UpdateError, match="nicht erreichbar"):
        updates.latest_release("0.1.0", httpx.Client(transport=httpx.MockTransport(down)))
    junk = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(UpdateError, match="unerwartete"):
        updates.latest_release("0.1.0", junk)


# ---------------------------------------------------------------- downloading

def info_for(tag="client-v0.1.5"):
    version = tag.replace("client-v", "")
    return updates.latest_release("0.1.0", serving([release(tag)])[0]) or UpdateInfo(version, "", "", "", 0, "", "", "")


def test_a_signed_installer_with_the_right_checksum_is_downloaded_into_a_folder_of_its_own(tmp_path):
    key, public = keypair()
    client, seen = serving([], signed_files("client-v0.1.5", key))
    progress = []
    path = updates.download_update(info_for(), lambda done, total: progress.append((done, total)), public_key=public,
                                   client=client, dest_dir=tmp_path / "dl")
    assert path == tmp_path / "dl" / "intercom-client-0.1.5-setup.exe" and path.read_bytes() == INSTALLER
    assert progress and progress[-1][0] == len(INSTALLER) and progress[-1][1] == len(INSTALLER)
    assert [p.name for p in (tmp_path / "dl").iterdir()] == [path.name]  # no partial file left


def test_downloads_may_be_redirected_to_githubs_storage_but_nowhere_else(tmp_path):
    key, public = keypair()
    files = signed_files("client-v0.1.5", key)
    name = "/omegator77/intercom-gateway/releases/download/client-v0.1.5/intercom-client-0.1.5-setup.exe"
    real = files[name]
    files[name] = httpx.Response(302, headers={"location": "https://objects.githubusercontent.com/blob/xyz"})
    files["/blob/xyz"] = real
    client, seen = serving([], files)
    path = updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path / "a")
    assert path.read_bytes() == INSTALLER and any("objects.githubusercontent.com" in u for u in seen)

    files[name] = httpx.Response(302, headers={"location": "https://evil.example/installer.exe"})
    client, _ = serving([], files)
    with pytest.raises(UpdateError, match="nicht auf GitHub"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path / "b")
    assert list((tmp_path / "b").iterdir()) == []

    files[name] = httpx.Response(302, headers={"location": "http://objects.githubusercontent.com/blob/xyz"})
    client, _ = serving([], files)
    with pytest.raises(UpdateError, match="nicht auf GitHub"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path / "c")


def test_a_signature_of_somebody_else_is_refused_before_anything_is_downloaded(tmp_path):
    _, public = keypair()
    other, _ = keypair()
    client, seen = serving([], signed_files("client-v0.1.5", other))
    with pytest.raises(UpdateError, match="Signatur"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path)
    assert not any(u.endswith("setup.exe") for u in seen) and list(tmp_path.iterdir()) == []


def test_a_changed_installer_is_deleted_and_not_offered(tmp_path):
    key, public = keypair()
    files = signed_files("client-v0.1.5", key)
    name = "/omegator77/intercom-gateway/releases/download/client-v0.1.5/intercom-client-0.1.5-setup.exe"
    files[name] = INSTALLER + b"tampered"
    client, _ = serving([], files)
    with pytest.raises(UpdateError, match="Prüfsumme"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_checksum_file_without_the_installer_and_oversized_files_are_refused(tmp_path, monkeypatch):
    key, public = keypair()
    client, _ = serving([], signed_files("client-v0.1.5", key, name="another-file.exe"))
    with pytest.raises(UpdateError, match="steht .* nicht"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path / "x")
    monkeypatch.setattr(updates, "MAX_INSTALLER_BYTES", 1000)
    client, _ = serving([], signed_files("client-v0.1.5", key))
    with pytest.raises(UpdateError, match="größer als erlaubt"):
        updates.download_update(info_for(), public_key=public, client=client, dest_dir=tmp_path / "y")
    assert list((tmp_path / "y").iterdir()) == []


def test_the_signature_check_and_the_checksum_parser_on_their_own():
    key, public = keypair()
    data = b"abc  file.exe\n"
    sig = base64.b64encode(key.sign(data)).decode()
    updates.verify_signature(public, data, sig)
    for bad_data, bad_sig in ((data + b"x", sig), (data, "not base64!"), (data, base64.b64encode(b"short").decode())):
        with pytest.raises(UpdateError):
            updates.verify_signature(public, bad_data, bad_sig)
    h = "a" * 64
    assert updates.expected_checksum(f"{h}  one.exe\n{'b' * 64} *two.exe\n".encode(), "two.exe") == "b" * 64


def test_the_built_in_public_key_is_a_valid_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(updates.PUBLIC_KEY, validate=True))


# ---------------------------------------------------------------- the release tool

def test_the_release_tool_signs_what_the_client_then_accepts(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("release_client", Path(__file__).parent.parent / "dev" / "release_client.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    keyfile = tmp_path / "k" / "signing.key"
    assert tool.keygen(keyfile) == 0 and keyfile.exists()
    assert tool.keygen(keyfile) == 1  # never overwrites a key
    public = tool.public_b64(tool.load_key(keyfile))
    installer = tmp_path / "intercom-client-0.1.5-setup.exe"
    installer.write_bytes(INSTALLER)
    assert tool.sign(installer, keyfile) == 0
    sums = (tmp_path / "SHA256SUMS.txt").read_bytes()
    updates.verify_signature(public, sums, (tmp_path / "SHA256SUMS.txt.sig").read_text())
    assert updates.expected_checksum(sums, installer.name) == hashlib.sha256(INSTALLER).hexdigest()
    assert tool.sign(tmp_path / "setup.exe", keyfile) == 1  # only properly named installers
    assert "client-v0.1.5" in capsys.readouterr().out
