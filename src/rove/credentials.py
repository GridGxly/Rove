"""Encrypted local store for employer-account credentials the workflow creates.

Ciphertext lives in the private state root; the key lives in a separate owner-only
file next to it. Passwords never enter Discord, the vault, model context, or logs:
the only reader is the browser daemon when it fills a sign-in form.
"""

import contextlib
import json
import os
import re
import secrets
import string
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .runtime import state_root

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
REDACTED = "[redacted]"
# The browser driver quotes the value it was typing in a failed action's call log, one
# call per line: everything after the call's name, to the end of its line, goes, so a
# value that itself contains `")` cannot leave a piece behind.
TYPED_VALUE = re.compile(
    r"\b(fill|type|press_sequentially|pressSequentially|select_option|selectOption)\(.*$",
    re.MULTILINE,
)
# "locator resolved to <input ... value="...">": the element the driver found, as markup.
RESOLVED_ELEMENT = re.compile(r"\b((?:locator|selector) resolved to )<.*$", re.MULTILINE)
# Any other value attribute a driver message quotes.
VALUE_ATTRIBUTE = re.compile(r"""\bvalue=(["']).*?\1""", re.DOTALL)


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


def scrub(text, extra=()) -> str:
    """Text that is safe to leave this process: no stored or in-flight password, and no
    value the browser driver quoted from a field it was filling.

    Used wherever an error is written to a file, a log, Discord or another process.
    """
    text = str(text)
    known = [str(s) for s in extra if s]
    with contextlib.suppress(Exception):  # an unreadable store must not block the scrub
        known += [str(item["password"]) for item in _load().values() if item.get("password")]
    for secret in sorted(set(known), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = TYPED_VALUE.sub(lambda match: f'{match.group(1)}("{REDACTED}")', text)
    text = RESOLVED_ELEMENT.sub(lambda match: f"{match.group(1)}<{REDACTED}>", text)
    return VALUE_ATTRIBUTE.sub(f'value="{REDACTED}"', text)
