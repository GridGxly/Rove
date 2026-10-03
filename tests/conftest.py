"""Shared test setup: the suite never reaches the network.

Company research is the one step that reads public web pages. Every test gets a
refusing HTTP client and a DNS-free destination check; the research tests replace the
client with their own synthetic employer site.
"""

from pathlib import Path

import httpx
import pytest

# Browser tests are marked e2e and launches from unmarked tests refused; see ci_marks.py.
from ci_marks import (  # noqa: F401 -- pytest reads hooks from this module
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_runtest_protocol,
)

from rove import research
from rove.jobs import public_link

real_http_client = research.http_client


def offline_client() -> httpx.Client:
    def refuse(_request):
        raise httpx.ConnectError("offline in tests")

    return real_http_client(httpx.MockTransport(refuse))


def public_only(url: str) -> str:
    """`validate_destination` without its DNS lookup."""
    safe = public_link(url)
    if not safe:
        raise PermissionError("Only public HTTPS pages are supported")
    return safe


@pytest.fixture(autouse=True)
def private_home(tmp_path_factory, monkeypatch):
    """Every test sees a throwaway home: no owner token, model key or tool on this machine.

    The suite must pass the same way on a developer's Mac and on a bare CI runner. A
    synthetic Discord credential is present so delivery code reaches its (refused)
    transport instead of stopping at the missing-credential check, and the model key is
    set so the client can be built; nothing ever reaches either service.

    Only Python's view of the home changes. The HOME variable is left alone, because a
    test browser started under a fake HOME finds no keychain and macOS then puts a
    "Keychain Not Found" dialog on the owner's screen.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # A test that names no state root must never fall back to the real one.
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    (home / ".hermes").mkdir()
    (home / ".hermes/.env").write_text("DISCORD_BOT_TOKEN=test-token\n")
    monkeypatch.setenv("ROVE_MODEL_API_KEY", "test-model-key")
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)


@pytest.fixture(autouse=True)
def no_network_research(monkeypatch):
    monkeypatch.setattr(research, "http_client", offline_client)
    monkeypatch.setattr(research, "validate_destination", public_only)


@pytest.fixture
def mock_http(monkeypatch):
    """Serve research requests from a handler, through the production client settings."""

    def install(handler):
        monkeypatch.setattr(
            research, "http_client", lambda: real_http_client(httpx.MockTransport(handler))
        )

    return install
