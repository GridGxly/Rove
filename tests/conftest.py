"""Shared test setup: the suite never reaches the network.

Company research is the one step that reads public web pages. Every test gets a
refusing HTTP client and a DNS-free destination check; the research tests replace the
client with their own synthetic employer site.
"""

import ipaddress
from pathlib import Path

import httpx
import pytest

# Browser tests are marked e2e and launches from unmarked tests refused; see ci_marks.py.
from ci_marks import (  # noqa: F401 -- pytest reads hooks from this module
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_runtest_protocol,
)

from rove import discord_feed, research
from rove.jobs import public_link

real_http_client = research.http_client


@pytest.fixture(autouse=True)
def local_http_only(monkeypatch):
    """Real HTTP transports may contact fixture servers only; services need mocks."""
    send = httpx.HTTPTransport.handle_request
    send_async = httpx.AsyncHTTPTransport.handle_async_request
    attempted = []

    def require_local(request):
        host = request.url.host
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            attempted.append(host)
            raise AssertionError(f"Unmocked external HTTP request in test: {host}")

    def checked(transport, request):
        require_local(request)
        return send(transport, request)

    async def checked_async(transport, request):
        require_local(request)
        return await send_async(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", checked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", checked_async)
    yield
    discord_feed.drop_client()
    assert not attempted, f"Test attempted external HTTP, even if its error was caught: {attempted}"


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
    # Discord's clock as the last response gave it is kept per thread: a test never
    # starts with the one an earlier test left behind.
    monkeypatch.setattr(discord_feed._seen, "date", None, raising=False)


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
