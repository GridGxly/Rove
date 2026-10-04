"""Real headless browser: every existing fixture reads exactly as it did.

Rove's observation of the Greenhouse, Lever and Ashby fixtures, the four board fixtures
and the other synthetic pages is kept in `fixtures/readings.json`: labels, kinds, keys,
options, controls and markers. A change to the reader that alters any of them for a page
it already handled shows up here, field by field. Keys matter most: answers for
applications in flight are stored under them.

To accept an intended change, regenerate the file with `ROVE_WRITE_READINGS=1`.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from patchright.sync_api import sync_playwright
from test_boards import PAGES
from test_live_submission import (
    ASHBY_FORM,
    FORM,
    GENERIC_FORM,
    LEVER_FORM,
    LOGIN,
    REGISTER,
    TWO_STEP,
)

from rove import live_browser
from rove.live_browser import RecruitingBrowser

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "readings.json"
# What changes with every observation, or with where the test runs.
VOLATILE = {
    "observation_id",
    "screenshot",
    "screenshot_error",
    "url",
    "run_id",
    "application_id",
    "profile_hash",
}


def pages() -> dict[str, tuple[str | None, str]]:
    """Every fixture page: (address it is served at, or None for an offline page; html)."""
    found: dict[str, tuple[str | None, str]] = {
        "greenhouse": (None, FORM.decode()),
        "ashby": (None, ASHBY_FORM.decode()),
        "two_step": (None, TWO_STEP.decode()),
        "lever_form": (None, LEVER_FORM.decode()),
        "generic_form": (None, GENERIC_FORM.decode()),
        "register": (None, REGISTER.decode()),
        "login": (None, LOGIN.decode()),
    }
    for name in ("lever_cards", "question_groups", "custom_selects"):
        found[name] = (None, (FIXTURES / f"{name}.html").read_text())
    for path in sorted((FIXTURES / "overlays").glob("*.html")):
        found[f"overlays/{path.stem}"] = (None, path.read_text())
    for board, named in sorted(PAGES.items()):
        for name, url in sorted(named.items()):
            found[f"boards/{board}/{name}"] = (
                url,
                (FIXTURES / "boards" / board / f"{name}.html").read_text(),
            )
    return found


def reading(observation: dict) -> dict:
    return {k: v for k, v in observation.items() if k not in VOLATILE}


@pytest.fixture(scope="module")
def readings(tmp_path_factory):
    state = tmp_path_factory.mktemp("state")
    previous = os.environ.get("ROVE_STATE_DIR")
    os.environ["ROVE_STATE_DIR"] = str(state)
    config = live_browser.workflow.config
    live_browser.workflow.config = lambda: {"human_pacing": False}
    found = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            served: dict[str, str] = {}

            def serve(route):
                url = route.request.url
                if route.request.method == "GET" and url in served:
                    route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=served[url]
                    )
                else:
                    route.abort()

            context.route("**/*", serve)
            page = context.new_page()
            runtime = RecruitingBrowser(headless=True)
            runtime.page = page
            runtime.run = {"id": "abcdef012345", "profile_hash": "x"}
            for name, (url, html) in pages().items():
                if url is None:
                    page.goto("about:blank")
                    page.set_content(html)
                else:
                    served.clear()
                    served[url] = html
                    page.goto(url)
                    assert urlsplit(page.url).hostname == urlsplit(url).hostname
                found[name] = json.loads(json.dumps(reading(runtime.observe())))
            browser.close()
    finally:
        live_browser.workflow.config = config
        if previous is None:
            os.environ.pop("ROVE_STATE_DIR", None)
        else:
            os.environ["ROVE_STATE_DIR"] = previous
    return found


def test_every_fixture_reads_exactly_as_before(readings):
    if os.environ.get("ROVE_WRITE_READINGS") == "1":
        GOLDEN.write_text(json.dumps(readings, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert sorted(readings) == sorted(golden)
    for name, observation in readings.items():
        expected = golden[name]
        for index, (seen, wanted) in enumerate(
            zip(observation["fields"], expected["fields"], strict=False)
        ):
            assert seen == wanted, f"{name}: field {index}"
        assert len(observation["fields"]) == len(expected["fields"]), name
        assert observation == expected, name
