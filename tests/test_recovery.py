"""Failures the owner never has to see, and plain words for the ones he does.

A step that sent nothing is tried once more without a card; its second failure is one
card with no exception name, step key or path in it. A pass whose worker died is found
by its quiet heartbeat in two minutes and goes back to the queue once. The browser
service answers `status` while a long request runs, and `rove doctor` reads the machine
through stubs.
"""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
import test_workflow
from test_unattended_failure_modes import (
    enabled_worker,
    feed_job,
    no_ids,
    nothing_sent,
    posts,
    reply_block,
    scripted_browser,
    the_card,
)

from rove import doctor, live_browser, recovery, worker, workflow

state = test_workflow.state

TIMEOUT = RuntimeError("Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating to …")


def system_log(calls) -> list[str]:
    return [p["content"] for m, path, p in calls if path == "/channels/sys/messages" and p]


def test_a_step_that_fails_once_is_tried_again_without_a_card(state, monkeypatch):
    calls = scripted_browser(monkeypatch, open=TIMEOUT)
    discord_calls, _posted = enabled_worker(monkeypatch, system_channel_id="sys")
    app = feed_job("slow")
    first = worker.tick()
    # Nothing for the owner: the application is back in the queue, first in line.
    assert first == {"application_id": app, "status": "QUEUED", "retry": "open"}
    assert workflow.get(app)["status"] == "QUEUED" and posts(discord_calls) == []
    assert recovery.due_retry() == app
    assert any("trying once more" in line for line in system_log(discord_calls))
    # The second failure of the same step is one card, in plain words.
    second = worker.tick()
    assert second["status"] == "NEEDS_USER" and calls == ["open", "open"]
    card = the_card(discord_calls)
    no_ids(card)
    reason = card["description"]
    assert "I couldn't open the posting after two tries" in reason
    assert "the page did not respond in time" in reason and "Nothing was sent" in reason
    for leaked in ("RuntimeError", "Timeout 30000ms", "Call log", "Page.goto", "open:"):
        assert leaked not in reason
    assert reply_block(card) == workflow.command_block(["go", "park it"])
    # The technical line is in the system log, where it belongs.
    assert any("open · RuntimeError · Page.goto" in line for line in system_log(discord_calls))
    nothing_sent(app, calls)
    # His `go` starts the count again: the next failure is quiet once more.
    worker.apply_command(worker.thread_command("go", app), "m-go")
    assert worker.tick()["status"] == "QUEUED" and len(posts(discord_calls)) == 1


def test_a_pass_that_goes_through_forgets_the_earlier_failure(state, monkeypatch):
    scripted_browser(monkeypatch, open=TIMEOUT)
    enabled_worker(monkeypatch)
    app = feed_job("flaky")
    assert worker.tick()["retry"] == "open"
    monkeypatch.setattr(worker, "process", lambda application_id: {"status": "NEEDS_USER"})
    worker.tick()
    with recovery.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM preparation_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM preparation_heartbeats").fetchone()[0] == 0
    assert recovery.due_retry() is None and app


def test_a_stop_that_is_the_owners_call_is_never_retried(state, monkeypatch):
    calls = scripted_browser(monkeypatch, open=RuntimeError(live_browser.UNSAFE_REDIRECT))
    discord_calls, _posted = enabled_worker(monkeypatch)
    feed_job("redirect")
    assert worker.tick()["status"] == "NEEDS_USER" and calls == ["open"]
    assert "not a public HTTPS site" in the_card(discord_calls)["description"]


@pytest.mark.parametrize(
    ("phase", "error", "retry", "words"),
    [
        ("prepare", sqlite3.OperationalError("database is locked"), True, "database was busy"),
        ("open", RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://x"), True, "network was down"),
        ("job_fit_review", ValueError("invalid JSON from the harness"), True, "the model did not"),
        ("prepare", KeyError("field_key"), True, "I couldn't fill the form after two tries."),
        ("job_fit_review", RuntimeError("prompt is too long"), False, "too long for the model"),
        ("account_creation", RuntimeError("Timeout 500ms exceeded"), False, "create the account"),
        ("prepare", OSError(28, "No space left on device"), False, "the disk is full"),
    ],
)
def test_every_stop_reads_as_plain_words(phase, error, retry, words):
    stop = recovery.classify(phase, error)
    assert stop.retry is retry and words in stop.reason
    assert recovery.plain_text(stop.reason), stop.reason
    assert "Nothing was sent" in stop.reason
    assert type(error).__name__ in stop.technical and stop.technical.startswith(phase)


def test_plain_text_refuses_what_a_card_must_never_carry():
    assert recovery.plain_text("I couldn't open the posting. Nothing was sent.")
    for text in (
        "Preparation stopped during open: RuntimeError",
        "follow_application_link failed",
        "see ~/state/error.json",
        "Traceback (most recent call last)",
        "TargetClosedError happened",
        "",
    ):
        assert not recovery.plain_text(text), text


def aged(application_id: str, minutes: float):
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    with recovery.db() as conn:
        conn.execute(
            "UPDATE preparation_heartbeats SET beat_at=? WHERE application_id=?",
            (stamp, application_id),
        )
        conn.execute(
            "UPDATE application_queue SET updated_at=? WHERE id=?", (stamp, application_id)
        )


def test_a_pass_whose_worker_died_goes_back_to_the_queue_after_two_quiet_minutes(
    state, monkeypatch
):
    discord_calls, _posted = enabled_worker(monkeypatch, system_channel_id="sys")
    quiet, alive = feed_job("quiet"), feed_job("alive")
    for app in (quiet, alive):
        workflow.set_state(app, "PREPARING")
        recovery.pass_started(app)
    aged(quiet, 3)
    aged(alive, 1)
    worker.recover_interrupted()
    assert workflow.get(quiet)["status"] == "QUEUED" and recovery.due_retry() == quiet
    assert workflow.get(alive)["status"] == "PREPARING"
    assert posts(discord_calls) == []  # the first time costs the owner nothing
    assert any("went quiet for two minutes" in line for line in system_log(discord_calls))
    # A sign of life keeps a long pass: the browser's beat is newer than the cutoff.
    recovery._beats.clear()
    aged(alive, 3)
    recovery.beat(alive)
    worker.recover_interrupted()
    assert workflow.get(alive)["status"] == "PREPARING"
    # The same application dying a second time is the owner's to look at.
    workflow.set_state(quiet, "PREPARING")
    recovery.pass_started(quiet)
    aged(quiet, 3)
    worker.recover_interrupted()
    assert workflow.get(quiet)["status"] == "NEEDS_USER"
    card = the_card(discord_calls)
    assert "Preparation interrupted" in card["description"]


def test_a_stuck_preparation_never_holds_the_queue_for_long(state, monkeypatch):
    # What happened live: a pass was killed, and the owner's next link waited behind it.
    scripted_browser(monkeypatch, open=RuntimeError(live_browser.UNSAFE_REDIRECT))
    enabled_worker(monkeypatch)
    dead = feed_job("dead")
    workflow.set_state(dead, "PREPARING")
    recovery.pass_started(dead)
    aged(dead, 3)
    his = workflow.enqueue(
        "https://jobs.example.com/his", source="owner_link", title="Example — Intern"
    )["application_id"]
    result = worker.tick()
    # One tick: the dead pass is back in the queue and work goes on, not "waiting on".
    assert "waiting_on" not in result
    assert workflow.get(dead)["status"] != "PREPARING" and his


def test_low_disk_pauses_new_work_and_says_so_once(state, monkeypatch):
    discord_calls, _posted = enabled_worker(monkeypatch)
    app = feed_job("later")
    monkeypatch.setattr(recovery, "free_bytes", lambda: int(0.4e9))
    assert worker.tick() == {"idle": True, "disk": "low"}
    assert worker.tick() == {"idle": True, "disk": "low"}
    assert workflow.get(app)["status"] == "QUEUED"
    (card,) = [c["embeds"][0] for c in posts(discord_calls)]
    assert "almost out of disk space" in card["title"] + card.get("description", "")
    assert "0.4 GB" in card.get("description", "") and recovery.plain_text(card["title"])
    monkeypatch.setattr(recovery, "free_bytes", lambda: int(50e9))
    assert recovery.disk_ok({}) is True


class StubBrowser:
    class launcher:  # the attribute the service reads
        @staticmethod
        def running_app():
            return None

    def __init__(self):
        self.closed: list[str] = []

    def close_run(self, run_id):
        self.closed.append(run_id)


def test_the_browser_service_answers_status_and_close_while_a_long_request_runs(state, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def respond(browser, raw):
        request = live_browser.Service.parsed(raw)
        if request["action"] == "prepare":
            started.set()
            assert release.wait(10)
            return {"result": {"prepared": True}}
        return {"result": {"action": request["action"]}}

    monkeypatch.setattr(live_browser, "respond", respond)
    monkeypatch.setattr(live_browser, "status_report", lambda browser: {"open_tabs": []})
    monkeypatch.setattr(
        live_browser.browser_app, "status", lambda settings: {"path": "/synthetic.app"}
    )
    browser = StubBrowser()
    service = live_browser.Service(browser)
    service.WAIT = 0.05
    threading.Thread(target=service.run, daemon=True).start()
    answers: dict = {}
    long = threading.Thread(
        target=lambda: answers.update(
            prepare=service.answer(b'{"action":"prepare","run_id":"aaaaaaaaaaaa"}')
        )
    )
    long.start()
    assert started.wait(5)
    # A peek never waits; a status waits half a second at most, then answers around it.
    peek = service.answer(b'{"action":"status","peek":true}')["result"]
    assert peek["daemon_running"] is True and peek["busy"]["action"] == "prepare"
    status = service.answer(b'{"action":"status"}')["result"]
    assert status["busy"]["action"] == "prepare"
    closed = service.answer(b'{"action":"close","run_id":"bbbbbbbbbbbb"}')["result"]
    assert closed == {"closed": "bbbbbbbbbbbb", "after": "prepare"} and browser.closed == []
    release.set()
    long.join(5)
    assert answers["prepare"] == {"result": {"prepared": True}}
    for _ in range(100):
        if browser.closed:
            break
        threading.Event().wait(0.02)
    assert browser.closed == ["bbbbbbbbbbbb"]  # done as soon as the long request ended
    # Garbage and a failing request still get an answer.
    monkeypatch.setattr(live_browser, "respond", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    assert service.answer(b'{"action":"observe"}') == {
        "error": "The browser service failed",
        "error_type": "KeyError",
    }


class StubProbe(doctor.Probe):
    def __init__(self, started: float, changed: float):
        self.started, self.changed = started, changed

    def launchd(self, label):
        return {"state": "running", "pid": 100, "last_exit": 0}

    def mtime(self, path):
        return self.started

    def browser_peek(self):
        return {"daemon_running": True, "browser_running": False, "open_tabs": []}

    def model(self):
        return ("ok", "synthetic-model")

    def processes(self):
        marker = doctor.LONG_RUNNING[0][0]
        return [(self.started, f"{doctor.ROOT}/.venv/bin/python {marker}")]

    def code_changed_at(self):
        return self.changed

    def tick_running(self):
        return False

    def free_bytes(self):
        return int(80e9)


def test_doctor_prints_one_plain_line_per_check_and_fails_on_a_problem(state, capsys):
    now = datetime.now(UTC).timestamp()
    checks = doctor.run(StubProbe(started=now, changed=now - 3600))
    assert checks and all(isinstance(c.line, str) and c.line for c in checks)
    for check in checks:
        assert "\n" not in check.line and "Traceback" not in check.line
    code = doctor.main(StubProbe(started=now, changed=now - 3600))
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == len(checks)
    assert all(line.startswith(("ok       ", "problem  ")) for line in printed)
    assert code == (0 if all(c.ok for c in checks) else 1)
    # A service started before the code last changed is running old code: a problem.
    stale = doctor.run(StubProbe(started=now - 7200, changed=now))
    assert any(not c.ok and "restart" in c.line.lower() for c in stale)


def test_the_worker_looks_for_a_receipt_after_its_pass_and_survives_mail_trouble(
    state, monkeypatch
):
    from rove import mail

    discord_calls, _posted = enabled_worker(monkeypatch, system_channel_id="sys")
    looked = []
    monkeypatch.setattr(mail, "follow_up", lambda: looked.append(1))
    assert worker.tick() == {"idle": True} and looked == [1]

    def broken():
        raise OSError("mailbox")

    monkeypatch.setattr(mail, "follow_up", broken)
    assert worker.tick() == {"idle": True}
    assert any("receipt check failed · OSError" in line for line in system_log(discord_calls))
