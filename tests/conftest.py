"""Shared test setup: the suite never reaches the network.

Company research is the one step that reads public web pages. Every test gets a
refusing HTTP client and a DNS-free destination check; the research tests replace the
client with their own synthetic employer site.
"""

import httpx
import pytest

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
