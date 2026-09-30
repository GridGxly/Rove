"""Encrypted local store for employer-account credentials the workflow creates.

Ciphertext lives in the private state root; the key lives in a separate owner-only
file next to it. Passwords never enter Discord, the vault, model context, or logs:
the only reader is the browser daemon when it fills a sign-in form.
"""

import json
import os
import secrets
import string
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .runtime import state_root

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"


def _paths():
    root = state_root() / "credentials"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root / "key", root / "store.enc"


def _fernet() -> Fernet:
    key_path, _ = _paths()
    if not key_path.is_file():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    key_path.chmod(0o600)
    return Fernet(key_path.read_bytes())


def _load() -> dict:
    _, store_path = _paths()
    if not store_path.is_file():
        return {}
    return json.loads(_fernet().decrypt(store_path.read_bytes()).decode())


def _save(data: dict):
    _, store_path = _paths()
    fd = os.open(store_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(_fernet().encrypt(json.dumps(data).encode()))
    store_path.chmod(0o600)


def account_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def generate_password(length: int = 20) -> str:
    while True:
        candidate = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*-_=+" for c in candidate)
        ):
            return candidate


def store(host: str, username: str, password: str, application_id: str) -> dict:
    """Save one account; returns safe metadata only."""
    data = _load()
    data[host] = {
        "username": username,
        "password": password,
        "created_at": datetime.now(UTC).isoformat(),
        "application_id": application_id,
        "verified": False,
    }
    _save(data)
    return {"host": host, "username": username, "stored": True}


def lookup(host: str) -> dict | None:
    return _load().get(host)


def mark_verified(host: str):
    data = _load()
    if host in data:
        data[host]["verified"] = True
        _save(data)


def summary() -> list[dict]:
    """Safe listing for the owner: hosts and usernames, never secrets."""
    return [
        {
            "host": host,
            "username": item["username"],
            "created_at": item["created_at"],
            "verified": item.get("verified", False),
        }
        for host, item in _load().items()
    ]
