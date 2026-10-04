"""Local runtime discovery; credentials never enter command-line arguments."""

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path

import httpx

MODEL = "Qwen3.8-27B-Uncensored-4bit"
MODEL_REPO = "orcarouter/Qwen3.8-27B-Uncensored-MLX"
MODEL_REVISION = "14963e70f886455cf93090ac95bdbf4c8730cbe1"
BASE_URL = "http://127.0.0.1:8000/v1"


def state_root() -> Path:
    p = Path(os.environ.get("ROVE_STATE_DIR", "~/.config/rove")).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def api_key() -> str:
    if key := os.environ.get("ROVE_MODEL_API_KEY"):
        return key
    settings = Path.home() / ".omlx/settings.json"
    return json.loads(settings.read_text())["auth"]["api_key"]


def client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": "Bearer " + api_key()},
        timeout=httpx.Timeout(900, connect=10),
        trust_env=False,
    )


def write_private(path: Path, value: dict) -> None:
    write_private_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


@contextlib.contextmanager
def private_lock(path: Path):
    """Serialize a complete read/change/write operation across threads and processes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_private_bytes(path: Path, value: bytes) -> None:
    """Replace a private artifact atomically; an interrupted write keeps the old copy."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
