import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "compat", Path(__file__).parents[1] / "scripts/hermes_small_context.py"
)
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def test_context_floor_is_opt_in_and_patch_is_idempotent(monkeypatch):
    patched = compat.patched(compat.ORIGINAL)
    assert compat.patched(patched) == patched
    monkeypatch.delenv("HERMES_AUTOPILOT_16K", raising=False)
    values = {}
    exec(patched, values)  # noqa: S102 — only our fixed compatibility fixture
    assert values["MINIMUM_CONTEXT_LENGTH"] == 64000
    monkeypatch.setenv("HERMES_AUTOPILOT_16K", "1")
    exec(patched, values)  # noqa: S102 — only our fixed compatibility fixture
    assert values["MINIMUM_CONTEXT_LENGTH"] == 16384


def test_unknown_upstream_source_refuses_patch():
    with pytest.raises(ValueError):
        compat.patched("MINIMUM_CONTEXT_LENGTH = 128_000")
