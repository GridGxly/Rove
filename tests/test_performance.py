"""Time and call budgets for one offline fixture application.

`rove bench fixture` drives a synthetic application end to end on this machine: a
loopback job board, the real worker, browser runtime, form fill and submission checks,
with the model, Erga and Discord replaced by local stand-ins (`rove.benchmark`). These
tests run it once and hold its stage timings and call counts to the budgets below.

Time budgets catch a regression such as a fixed one-second sleep per field or a
verification that waits out its timeout. They are generous multiples of what was
measured, so a slow CI runner never fails them on noise. Counts are exact properties of
the code, so they are held exactly (model calls, Discord requests) or at a ceiling that
should only come down (browser round trips, observations, owner cards).

When a change moves a number on purpose, update the table and say why in the commit.
Set ROVE_CI_ARTIFACTS to a folder to keep the bench table and the measured values.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import statistics
import time
from pathlib import Path

import pytest
from patchright.sync_api import Frame, Page

from rove import benchmark, discord_feed, timing

pytestmark = pytest.mark.performance

# Measured on a 14-inch MacBook Pro (M5 Pro, 48 GB) by three runs of this file on
# 2026-10-03 (`pytest -m performance`, ROVE_CI_ARTIFACTS set); the spread is the lowest
# and highest value of the three. Budgets are seconds and sit far above it on purpose:
# roughly 8x for the passes and 10x or more for single steps, room for a slow runner.
TIME_BUDGETS = {
    # Each entry: what is timed, the measured spread, and the budget in seconds.
    "pass": ("preparation pass, median of three", "1.12-1.18 s", 10.0),
    "fill_per_field": ("form fill per field filled, median", "0.066-0.069 s", 0.75),
    "observe": ("page observation, median", "0.026-0.030 s", 0.5),
    "submit": ("submit stage", "0.063-0.073 s", 3.0),
    "verify": ("verify stage (confirmation timeout here is 5 s)", "0.15-0.20 s", 3.0),
    "working": ("all passes and the submission together", "4.2-4.3 s", 30.0),
}
# A fixed wait is `time.sleep`, `asyncio.sleep` or Playwright's `wait_for_timeout`: time
# spent whatever the page does. Waits should end on a page state instead.
FIXED_WAIT_BUDGETS = {
    "longest": ("longest single fixed wait", "0.15 s", 1.0),
    "total": ("all fixed waits in the run (form settling polls)", "2.25 s", 4.5),
}
CALL_BUDGETS = {
    # Each entry: what is counted, the count, and whether it is exact or a ceiling.
    "model": ("model calls: one fit review and one drafting call", 2, "exact"),
    "discord": ("Discord requests from an offline fixture", 0, "exact"),
    "browser": ("browser round trips", 11, "ceiling"),
    "observations": ("page observations", 19, "ceiling"),
    "owner_cards": ("owner cards raised for the one question", 2, "ceiling"),
}


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """One fixture application under a throwaway home and state root, with every fixed
    wait and every Discord request recorded."""
    root = tmp_path_factory.mktemp("bench")
    (root / "vault").mkdir()
    home = tmp_path_factory.mktemp("bench-home")
    waits: list[tuple[str, float]] = []
    discord: list[str] = []
    real_sleep, real_async_sleep = time.sleep, asyncio.sleep
    page_wait, frame_wait = Page.wait_for_timeout, Frame.wait_for_timeout

    def sleep(seconds):
        waits.append(("time.sleep", float(seconds)))
        real_sleep(seconds)

    async def async_sleep(seconds, *args, **kwargs):
        waits.append(("asyncio.sleep", float(seconds)))
        return await real_async_sleep(seconds, *args, **kwargs)

    def waiting(original):
        def wait_for_timeout(self, timeout):
            waits.append(("wait_for_timeout", float(timeout) / 1000))
            return original(self, timeout)

        return wait_for_timeout

    def refuse_discord(*args, **_kwargs):
        discord.append(str(args[:2]))
        raise RuntimeError("the bench fixture is offline")

    real_replaced = benchmark.replaced

    @contextlib.contextmanager
    def counting_stand_ins(pairs):
        """The fixture's own Discord stand-ins, counted on the way through."""

        def counted(value):
            def call(*args, **kwargs):
                discord.append(str(args[:2]))
                return value(*args, **kwargs)

            return call

        with real_replaced([(o, n, counted(v) if n == "discord" else v) for o, n, v in pairs]):
            yield

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: home))
        patch.setenv("ROVE_STATE_DIR", str(root / "state"))
        patch.setenv("OBSIDIAN_VAULT_PATH", str(root / "vault"))
        patch.setattr(time, "sleep", sleep)
        patch.setattr(asyncio, "sleep", async_sleep)
        patch.setattr(Page, "wait_for_timeout", waiting(page_wait))
        patch.setattr(Frame, "wait_for_timeout", waiting(frame_wait))
        patch.setattr(discord_feed, "_request", refuse_discord)
        patch.setattr(benchmark, "replaced", counting_stand_ins)
        started = time.perf_counter()
        table = benchmark.run_fixture(root)
        wall = time.perf_counter() - started
        rows = timing.rows()
        with contextlib.closing(sqlite3.connect(root / "state" / "recruiting.sqlite3")) as db:
            cards = db.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0]
    result = {"rows": rows, "waits": waits, "discord": discord, "cards": cards, "wall": wall}
    result["receipt"] = json.loads((root / "employer-receipt.json").read_text())
    result["values"] = values(result)
    keep(table, result)
    return result


def median_seconds(rows: list[dict], stage: str, parent: str | None = None) -> float:
    found = [r["seconds"] for r in rows if r["stage"] == stage]
    if parent is not None:
        found = [r["seconds"] for r in rows if r["stage"] == stage and r["parent"] == parent]
    assert found, f"no {stage} rows were recorded"
    return statistics.median(found)


def values(run: dict) -> dict:
    rows = run["rows"]
    fills = [
        r["seconds"] / filled
        for r in rows
        if r["stage"] == "fill" and "fields_code" in r["facts"]
        for filled in [
            sum(int(r["facts"].get("fields_" + k, 0)) for k in ("code", "model", "owner"))
        ]
        if filled
    ]
    seconds = [w for _, w in run["waits"]]
    return {
        "pass": median_seconds(rows, "pass", ""),
        "fill_per_field": statistics.median(fills) if fills else None,
        "observe": median_seconds(rows, "observe"),
        "submit": median_seconds(rows, "submit", "submission"),
        "verify": median_seconds(rows, "verify", "submission"),
        "working": sum(r["seconds"] for r in rows if r["stage"] in {"pass", "submission"}),
        "longest": max(seconds, default=0.0),
        "total": sum(seconds),
        "model": sum(1 for r in rows if r["stage"] == "model"),
        "discord": len(run["discord"]),
        "browser": sum(1 for r in rows if r["stage"] == "browser"),
        "observations": sum(1 for r in rows if r["stage"] == "observe"),
        "owner_cards": run["cards"],
        "wall": run["wall"],
    }


def keep(table: str, run: dict):
    folder = os.environ.get("ROVE_CI_ARTIFACTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / "bench-table.txt").write_text(table + "\n")
        (Path(folder) / "bench-values.json").write_text(json.dumps(run["values"], indent=2))


def test_the_fixture_application_is_sent(measured):
    rows = measured["rows"]
    assert sum(1 for r in rows if r["stage"] == "pass") == 3
    assert sum(1 for r in rows if r["stage"] == "submission" and r["ok"]) == 1
    assert all(r["ok"] for r in rows if r["stage"] in {"pass", "submission", "submit", "verify"})
    receipt = measured["receipt"]
    assert receipt["fields"]["email"] == "alex@example.invalid"
    assert receipt["fields"]["clearance"] == "No"
    assert receipt["fields"]["visa"] == "No"
    assert len(receipt["resume_sha256"]) == 64


@pytest.mark.parametrize("key", sorted(TIME_BUDGETS))
def test_stage_time_stays_inside_its_budget(measured, key):
    what, spread, budget = TIME_BUDGETS[key]
    value = measured["values"][key]
    assert value is not None, f"{what}: nothing was measured"
    assert value <= budget, f"{what} took {value:.3f} s; budget {budget} s, measured {spread}"


@pytest.mark.parametrize("key", sorted(FIXED_WAIT_BUDGETS))
def test_no_fixed_multi_second_wait(measured, key):
    what, spread, budget = FIXED_WAIT_BUDGETS[key]
    value = measured["values"][key]
    longest = sorted(measured["waits"], key=lambda w: -w[1])[:5]
    assert value <= budget, (
        f"{what} is {value:.2f} s; budget {budget} s, measured {spread}. Longest: {longest}. "
        "Wait for a page state instead of a fixed interval."
    )


@pytest.mark.parametrize("key", sorted(CALL_BUDGETS))
def test_call_counts_hold(measured, key):
    what, count, kind = CALL_BUDGETS[key]
    value = measured["values"][key]
    if kind == "exact":
        assert value == count, f"{what}: {value}, expected exactly {count}"
    else:
        assert value <= count, f"{what}: {value}, ceiling {count}"
