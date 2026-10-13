"""Line passwords in the Windows Credential Manager (via `keyring`), so no password has to sit in a file
or an environment variable. The key is account@server so the same account name on two servers differs."""
from __future__ import annotations

SERVICE = "intercom-gateway"


def _key(server_url: str, username: str) -> str:
    return f"{username}@{server_url.rstrip('/')}"


def get_password(server_url: str, username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(SERVICE, _key(server_url, username)) or None
    except Exception:  # keyring missing or no usable backend: behave as "nothing stored"
        return None


def set_password(server_url: str, username: str, password: str) -> None:
    import keyring

    keyring.set_password(SERVICE, _key(server_url, username), password)


def delete_password(server_url: str, username: str) -> None:
    import keyring

    try:
        keyring.delete_password(SERVICE, _key(server_url, username))
    except keyring.errors.PasswordDeleteError:
        pass
