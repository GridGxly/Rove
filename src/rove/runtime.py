"""Local runtime discovery; credentials never enter command-line arguments."""

import json
import os
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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Open with restrictive permissions from the first byte, including replacements.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
