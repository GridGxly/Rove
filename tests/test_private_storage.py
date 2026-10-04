"""Interrupted and concurrent credential writes preserve usable private state."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from rove import chat, credentials, runtime


def test_interrupted_replacement_preserves_previous_artifact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    runtime.write_private(path, {"revision": 1})

    def interrupted(*_args):
        raise OSError("simulated interruption before replacement")

    monkeypatch.setattr(runtime.os, "replace", interrupted)
    with pytest.raises(OSError):
        runtime.write_private(path, {"revision": 2})
    assert json.loads(path.read_text()) == {"revision": 1}
    assert list(tmp_path.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_accounts_keep_every_password_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path))

    def create(index):
        credentials.store(
            f"jobs{index}.example.com",
            "alex@example.invalid",
            f"Synthetic-Secret-{index}!",
            "a" * 12,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(create, range(20)))
    assert len(credentials.summary()) == 20
    for index in range(20):
        assert credentials.lookup(f"jobs{index}.example.com")["password"] == (
            f"Synthetic-Secret-{index}!"
        )
    for path in (tmp_path / "credentials").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert b"Synthetic-Secret" not in path.read_bytes()


@pytest.mark.parametrize("length", [0, 3, True, 1025, "20"])
def test_impossible_password_lengths_fail_without_looping(length):
    with pytest.raises(ValueError):
        credentials.generate_password(length)


def test_concurrent_config_updates_keep_unrelated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path))
    path = tmp_path / "config/workflow.json"
    runtime.write_private(path, {"auto_submit": False})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: chat.set_setting(f"test_setting_{n}", n), range(20)))
    saved = json.loads(path.read_text())
    assert saved == {"auto_submit": False, **{f"test_setting_{n}": n for n in range(20)}}
    assert path.stat().st_mode & 0o777 == 0o600
