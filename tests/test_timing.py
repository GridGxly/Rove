"""Stage timings are measurement only: they record what happened and never get in its way.

The table lives in the state database. A missing table, a locked database or a bad fact
costs the caller nothing; `rove bench report` reads whatever was recorded.
"""

import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from rove import benchmark, cli, timing, workflow

APP = "abcdef012345"
OTHER = "0123456789ab"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(timing, "_state", {"quiet_until": 0.0, "prune_after": 0.0})
    return tmp_path / "state"


@timing.call("browser")
def browser(action: str, **kwargs) -> str:
    return action


@timing.call("model")
def model() -> str:
    return "draft"


def recorded() -> list[tuple]:
    return [(r["stage"], r["parent"]) for r in timing.rows()]


def by_stage(name: str) -> list[dict]:
    return [r for r in timing.rows() if r["stage"] == name]


def test_a_stage_records_its_laps_its_calls_and_what_ran_inside_it(state):
    with timing.stage(APP, "pass"):
        timing.lap("open")
        assert browser("open", url="https://jobs.example.com/1") == "open"
        timing.lap("fill")
        browser("prepare", run_id=APP)
        browser("prepare", run_id=APP)
        timing.note(fields_code=3)
        with timing.stage(None, "drafting", questions=2):
            assert model() == "draft"
            timing.note(proposals=1)
    # Calls and nested stages are written as they end; a stage's laps wait for the stage,
    # so a lap in the middle of the work never touches the database.
    assert recorded() == [
        ("browser", "open"),
        ("browser", "fill"),
        ("browser", "fill"),
        ("model", "drafting"),
        ("drafting", "pass"),
        ("open", "pass"),
        ("fill", "pass"),
        ("pass", ""),
    ]
    assert {r["application_id"] for r in timing.rows()} == {APP}
    assert all(r["ok"] and r["seconds"] >= 0 for r in timing.rows())
    assert by_stage("open")[0]["facts"]["browser_calls"] == 1
    fill = by_stage("fill")[0]["facts"]
    assert (fill["browser_calls"], fill["fields_code"]) == (2, 3) and "model_calls" not in fill
    drafting = by_stage("drafting")[0]["facts"]
    assert (drafting["questions"], drafting["proposals"], drafting["model_calls"]) == (2, 1, 1)
    whole = by_stage("pass")[0]["facts"]
    assert (whole["browser_calls"], whole["model_calls"]) == (3, 1)
    assert whole["model_seconds"] >= 0 and "fields_code" not in whole


def test_a_decorated_function_finds_its_application_in_its_own_arguments(state):
    @timing.stage(None, "pass")
    def process(application_id: str) -> str:
        return application_id.upper()

    @timing.stage(None, "submission")
    def submit(runtime, application_id: str, package_hash: str) -> str:
        return package_hash

    class Runtime:
        def __init__(self):
            self.run = {"id": OTHER}

        @timing.call("observe", lambda self: self.run["id"])
        def observe(self) -> dict:
            return {"fields": []}

    assert process(APP) == APP.upper()
    assert submit(object(), OTHER, "a" * 64) == "a" * 64
    assert browser("submit", run_id=APP) == "submit"
    assert Runtime().observe() == {"fields": []}
    assert [(r["stage"], r["application_id"]) for r in timing.rows()] == [
        ("pass", APP),
        ("submission", OTHER),
        ("browser", APP),
        ("observe", OTHER),
    ]
    assert process.__name__ == "process"  # the wrapper keeps the function's identity


def test_an_error_in_the_work_passes_through_and_is_recorded_as_failed(state):
    @timing.stage(None, "pass")
    def process(application_id: str):
        timing.lap("open")
        raise PermissionError("Private/local network destinations are forbidden")

    with pytest.raises(PermissionError, match="forbidden"):
        process(APP)
    assert [(r["stage"], r["ok"]) for r in timing.rows()] == [("open", False), ("pass", False)]
    # Nothing is left open: the next stage starts from a clean slate.
    with timing.stage(APP, "pass"):
        pass
    assert timing.rows()[-1]["parent"] == ""


def test_a_missing_table_never_reaches_the_caller(state, monkeypatch):
    monkeypatch.setattr(timing, "SCHEMA", "SELECT 1;")  # the table cannot be created

    @timing.stage(None, "pass")
    def process(application_id: str) -> int:
        timing.lap("open")
        timing.note(fields_code=1)
        return 42

    assert process(APP) == 42
    assert browser("open", run_id=APP) == "open"
    timing.record(APP, "queue_wait", 3.0)
    timing.queue_wait({"id": APP, "status": "QUEUED", "updated_at": workflow.now()})
    assert timing.rows() == [] and timing.rows(last=3) == []
    assert "No stage timings recorded yet" in benchmark.stage_report()


def test_a_locked_database_costs_the_caller_one_short_wait_and_no_error(state, monkeypatch):
    timing.record(APP, "queue_wait", 1.0)
    blocker = sqlite3.connect(state / "recruiting.sqlite3")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        with timing.stage(APP, "pass"):
            timing.lap("open")
            assert browser("open", run_id=APP) == "open"
            value = 41 + 1
        # One busy wait, then the table is left alone: three more rows cost nothing.
        assert value == 42 and time.perf_counter() - started < 1.5
    finally:
        blocker.rollback()
        blocker.close()
    assert [r["stage"] for r in timing.rows()] == ["queue_wait"]
    monkeypatch.setitem(timing._state, "quiet_until", 0.0)
    with timing.stage(APP, "pass"):
        pass
    assert [r["stage"] for r in timing.rows()] == ["queue_wait", "pass"]


def test_an_unusable_state_root_never_reaches_the_caller(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setenv("ROVE_STATE_DIR", str(blocked / "state"))
    monkeypatch.setattr(timing, "_state", {"quiet_until": 0.0, "prune_after": 0.0})
    with timing.stage(APP, "pass"):
        timing.lap("open")
        result = browser("open", run_id=APP)
    assert result == "open" and timing.rows() == []


def test_rows_belong_to_an_application_except_in_the_browser_service(state, monkeypatch):
    assert model() == "draft" and browser("status") == "status"
    assert timing.rows() == []  # nothing ran for an application, nothing is kept
    monkeypatch.setattr(sys, "argv", ["rove", "browser", "serve"])
    assert model() == "draft"
    assert [(r["stage"], r["application_id"]) for r in timing.rows()] == [("model", "")]


def test_token_counts_are_read_only_from_usage_the_server_reported():
    served = {"prompt_tokens": 2565, "completion_tokens": 1031}
    # The server's own usage block, with its cached-token detail.
    wire = {"result": {"usage": {**served, "prompt_tokens_details": {"cached_tokens": 512}}}}
    assert timing.tokens(wire) == {"tokens_in": 2565, "tokens_out": 1031, "tokens_cached": 512}
    # The harness's totals on the result itself.
    totals = {"result": {"input_tokens": 5350, "output_tokens": 337, "cache_read_tokens": 0}}
    assert timing.tokens(totals) == {"tokens_in": 5350, "tokens_out": 337, "tokens_cached": 0}
    assert timing.tokens({"usage": served}) == {"tokens_in": 2565, "tokens_out": 1031}
    # No usage, or usage that is not a count: nothing is recorded and nothing is guessed.
    for silent in (
        {"model": "m", "result": {"completed": True, "final_response": "{}"}},
        {"result": {"usage": {"prompt_tokens": "many", "completion_tokens": -1}}},
        {"result": {"usage": {"prompt_tokens": True}}},
        {"result": None},
        "not a result",
        None,
    ):
        assert timing.tokens(silent) == {}


def test_a_model_call_row_carries_the_token_counts_of_its_result(state):
    @timing.call("model", result=timing.tokens)
    def generate(directory, context, basename):
        return {"model": "m", "result": {"usage": context}}

    @timing.call("model", result=lambda value: value["missing"])
    def unreadable():
        return {"model": "m"}

    with timing.stage(APP, "fit_review"):
        generate(None, {"prompt_tokens": 2565, "completion_tokens": 1031}, "job-reasoning")
        generate(None, {}, "job-reasoning")
        assert unreadable() == {"model": "m"}  # a reader that fails costs the caller nothing
    calls = [r["facts"] for r in timing.rows() if r["stage"] == "model"]
    assert calls == [{"tokens_in": 2565, "tokens_out": 1031}, {}, {}]
    review = by_stage("fit_review")[0]["facts"]
    assert review["model_calls"] == 3 and "tokens_in" not in review


def test_facts_keep_numbers_and_single_words_only(state):
    timing.record(
        APP,
        "fill",
        0.5,
        fields_code=9,
        cached=True,
        share=0.123456,
        kind="textarea",
        label="Why do you want to work here?",
        answer="Because of the mission.",
        url="https://jobs.example.com/1",
        nested={"a": 1},
    )
    assert timing.rows()[0]["facts"] == {
        "fields_code": 9,
        "cached": True,
        "share": 0.123,
        "kind": "textarea",
    }


def test_timing_is_off_when_the_workflow_config_says_so(state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True, "timing": False})
    with timing.stage(APP, "pass"):
        pass
    assert timing.rows() == []
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True})
    with timing.stage(APP, "pass"):
        pass
    assert len(timing.rows()) == 1


# Sample rows sit a few hours back from now, inside the retention window.
BASE = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)


def test_queue_wait_is_read_from_the_queue_row_and_old_rows_are_pruned(state):
    long_ago = (datetime.now(UTC) - timedelta(days=timing.KEEP_DAYS + 10)).isoformat()
    waited = {"id": APP, "status": "QUEUED", "updated_at": BASE.isoformat()}
    timing._insert(APP, "pass", "", long_ago, 5.0, True, {})
    timing.queue_wait(waited)
    timing.queue_wait({**waited, "status": "NEEDS_USER"})  # not waiting in the queue
    timing.queue_wait({"id": APP})  # a row without the fields is ignored
    rows = timing.rows()
    # The first write of a process drops rows older than the retention window.
    assert [r["stage"] for r in rows] == ["queue_wait"] and rows[0]["seconds"] > 3600


def sample(at: int, application_id: str, stage: str, parent: str, seconds: float, **facts):
    """One recorded row, `at` seconds after BASE."""
    started = (BASE + timedelta(seconds=at)).isoformat()
    timing._insert(application_id, stage, parent, started, seconds, True, facts)


def record_two_applications():
    """Two applications: one prepared twice (the second pass reads the stored review and
    skips drafting) and then sent, one prepared once."""
    sample(0, APP, "queue_wait", "", 15.0)
    sample(15, APP, "open", "pass", 10.0, browser_calls=1, browser_seconds=10.0)
    sample(
        25,
        APP,
        "fit_review",
        "pass",
        80.0,
        model_calls=1,
        model_seconds=79.0,
        cached=False,
        changed="first",
    )
    sample(105, APP, "fill", "pass", 20.0, browser_calls=1, fields_code=9, fields_model=0)
    sample(
        125,
        APP,
        "drafting",
        "pass",
        60.0,
        model_calls=2,
        model_seconds=58.5,
        questions=4,
        writing=1,
        choices=1,
        short=1,
        owner_only=1,
        proposals=3,
    )
    sample(185, APP, "hold", "pass", 5.0)
    sample(15, APP, "pass", "", 180.0, model_calls=3, model_seconds=137.5, browser_calls=2)
    sample(600, APP, "open", "pass", 4.0, browser_calls=1)
    sample(604, APP, "fit_review", "pass", 0.01, cached=True)
    sample(605, APP, "fill", "pass", 10.0, browser_calls=1, fields_code=9, fields_model=3)
    sample(615, APP, "drafting", "", 0.0, skipped=True, questions=1, owner_only=1)
    sample(600, APP, "pass", "", 20.0, browser_calls=2)
    sample(660, APP, "submit", "submission", 2.0)
    sample(662, APP, "verify", "submission", 4.0)
    sample(660, APP, "submission", "", 6.0)
    fit_tokens = {"tokens_in": 2565, "tokens_out": 1031, "tokens_cached": 0}
    sample(30, APP, "model", "fit_review", 80.0, **fit_tokens)
    sample(130, APP, "model", "drafting", 57.5, tokens_in=5350, tokens_out=337)
    sample(190, APP, "model", "drafting", 1.0)  # a call whose result carried no usage
    sample(3600, OTHER, "open", "pass", 6.0, browser_calls=1)
    sample(
        3606,
        OTHER,
        "fit_review",
        "pass",
        100.0,
        model_calls=1,
        model_seconds=99.0,
        cached=False,
        changed="posting",
    )
    sample(3600, OTHER, "pass", "", 110.0, model_calls=1, model_seconds=99.0)


def test_a_row_the_browser_service_wrote_belongs_to_the_round_trip_that_covers_it(
    state, monkeypatch
):
    sample(0, APP, "browser", "open", 10.0)
    sample(100, OTHER, "browser", "open", 10.0)
    monkeypatch.setattr(sys, "argv", ["rove", "browser", "serve"])
    sample(4, "", "discord", "", 0.5)  # posted while the service worked for APP
    sample(50, "", "discord", "", 0.5)  # between the two round trips: nobody's
    sample(104, "", "discord", "", 0.5)  # posted while the service worked for OTHER
    assert [r["stage"] for r in timing.rows()].count("discord") == 3
    latest = timing.rows(last=1)
    assert [(r["stage"], r["application_id"]) for r in latest] == [
        ("browser", OTHER),
        ("discord", ""),
    ]
    assert latest[1]["started_at"] == (BASE + timedelta(seconds=104)).isoformat()
    assert len(timing.rows(last=2)) == 4


def table_row(table: str, label: str) -> list[str]:
    """The numbers of one table row, found by its exact stage label."""
    return next(line for line in table.splitlines() if line[:26].strip() == label)[26:].split()


def test_the_report_gives_count_median_p90_share_and_model_calls_per_stage(state):
    record_two_applications()
    summary = benchmark.summarize(timing.rows())
    assert (summary["applications"], summary["passes"], summary["submissions"]) == (2, 3, 1)
    assert summary["working_seconds"] == 316.0 and summary["waiting_seconds"] == 15.0
    lines = {line["label"].strip(): line for line in summary["stages"]}
    assert list(lines) == [
        "queue wait",
        "preparation pass",
        "open",
        "fit review",
        "fit review (stored)",
        "fill",
        "drafting",
        "drafting (skipped)",
        "hold card",
        "other",
        "submission",
        "submit",
        "verify",
    ]
    fit = lines["fit review"]
    assert (fit["runs"], fit["median"], fit["p90"]) == (2, 90.0, 100.0)
    assert (fit["model_calls"], fit["model_seconds"]) == (2, 178.0)
    assert fit["share"] == pytest.approx(180 / 316)
    stored = lines["fit review (stored)"]
    assert (stored["runs"], stored["model_calls"]) == (1, 0)
    whole = lines["preparation pass"]
    assert (whole["median"], whole["p90"]) == (110.0, 180.0)
    assert lines["queue wait"]["share"] is None  # waiting is not working time
    # What no stage inside a pass accounts for: 180-175, 20-14.01 and 110-106.
    other = lines["other"]
    assert (other["runs"], other["median"], round(other["p90"], 2)) == (3, 5.0, 5.99)
    calls = {line["label"]: line for line in summary["calls"]}
    assert (calls["model"]["runs"], calls["model"]["median"]) == (3, 57.5)
    assert summary["fills"] == {"code": 9, "model": 1.5, "owner": 0, "pending": 0}
    assert summary["fit"] == {"stored": 1, "asked": {"first": 1, "posting": 1}}
    assert summary["tokens"] == {
        "fit review": {"calls": 1, "in": 2565, "out": 1031, "cached": 0},
        "drafting": {"calls": 1, "in": 5350, "out": 337, "cached": None},
    }
    assert summary["drafting"] == {
        "calls": 1,
        "skipped": 1,
        "questions": 4,
        "writing": 1,
        "choices": 1,
        "short": 1,
        "owner_only": 1,
        "proposals": 3,
    }
    text = benchmark.render(summary)
    assert "2 applications · 3 preparation passes · 1 submission" in text
    assert "working 316.0s · waiting in queue 15.0s" in text
    assert table_row(text, "fit review") == ["2", "90.00s", "100.0s", "57.0%", "2", "178.0"]
    assert table_row(text, "queue wait") == ["1", "15.00s", "15.00s", "–"]
    assert "1 call skipped" in text and "9 by code" in text
    assert (
        "fit review asked of the model: 1 first for the job, 1 after a new posting · "
        "read back from the stored review: 1"
    ) in text
    assert (
        "model tokens per fit review call, median: 2565 in · 1031 out · 0 cached "
        "(1 call with usage)"
    ) in text
    assert "model tokens per drafting call, median: 5350 in · 337 out (1 call with usage)" in text


def run_cli(monkeypatch, capsys, *arguments) -> str:
    monkeypatch.setattr(sys, "argv", ["rove", *arguments])
    cli.main()
    return capsys.readouterr().out


def test_bench_report_renders_with_nothing_recorded_and_with_recorded_rows(
    state, monkeypatch, capsys
):
    assert "No stage timings recorded yet" in run_cli(monkeypatch, capsys, "bench", "report")
    record_two_applications()
    everything = run_cli(monkeypatch, capsys, "bench", "report")
    assert "2 applications · 3 preparation passes · 1 submission" in everything
    assert everything.splitlines()[3].split()[:4] == ["stage", "runs", "median", "p90"]
    assert table_row(everything, "fit review (stored)")[0] == "1"
    latest = run_cli(monkeypatch, capsys, "bench", "report", "--last", "1")
    assert "1 application · 1 preparation pass · 0 submissions" in latest
    assert "drafting" not in latest  # the most recent application never reached drafting
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, capsys, "bench", "report", "--last", "0")


def test_the_fixture_application_runs_offline_and_shows_the_stored_review_and_the_skip(
    state, monkeypatch
):
    before = dict(os.environ)
    table = benchmark.fixture()
    assert dict(os.environ) == before  # the throwaway state root is gone again
    assert not (state / "recruiting.sqlite3").exists()  # nothing was written outside it
    assert "1 application · 3 preparation passes · 1 submission" in table
    # One model call for the fit review, then the stored review on both later passes.
    assert table_row(table, "fit review")[0] == "1"
    assert table_row(table, "fit review (stored)")[0] == "2"
    assert table_row(table, "drafting")[0] == "1"
    assert table_row(table, "drafting (skipped)")[0] == "1"
    assert table_row(table, "model")[0] == "2"  # both stand-ins, no model was called
    assert table_row(table, "submit")[0] == "1" and table_row(table, "verify")[0] == "1"
    assert "sent 3 questions (1 writing, 1 choice, 0 short answer, 1 owner-only)" in table
    assert "and got 2 drafts · 1 call skipped" in table
    assert "asked of the model: 1 first for the job · read back from the stored review: 2" in table
