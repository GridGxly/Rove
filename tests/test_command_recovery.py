"""Interrupt accepted replies at persistence boundaries, then restart the worker."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from rove import command_effects, onboarding, vault, worker, workflow


def fail_with(message):
    def fail(*args, **kwargs):
        raise OSError(message)

    return fail


@pytest.fixture
def held(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    onboarding.propose(
        "identity",
        {"legal_first_name": "Alex", "legal_last_name": "Example"},
        onboarding.digest(onboarding.draft()),
    )
    onboarding.approve(onboarding.digest(onboarding.draft()))
    application_id = workflow.enqueue("https://jobs.example.com/recovery")["application_id"]
    workflow.transition(application_id, "NEEDS_USER", "fixture question")
    monkeypatch.setattr(worker, "browser_call", lambda *a, **k: {})
    return application_id, tmp_path / "state"


def journal(message="reply-1"):
    with workflow.db() as conn:
        command = dict(
            conn.execute("SELECT * FROM owner_commands WHERE message_id=?", (message,)).fetchone()
        )
        effects = conn.execute(
            "SELECT count(*) FROM owner_command_effects WHERE message_id=?", (message,)
        ).fetchone()[0]
    return command["status"], effects


def test_answer_recovers_original_value_when_memory_write_was_interrupted(held, monkeypatch):
    application_id, state = held
    field = {"key": "gpa", "label": "Current GPA", "kind": "text", "options": [], "required": True}
    directory = state / "applications" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "observation.json").write_text(json.dumps({"fields": [field]}))
    request = {
        "kind": "answer",
        "application_id": application_id,
        "field_key": "gpa",
        "value": "3.5",
    }
    original = workflow.remember_answer
    monkeypatch.setattr(
        workflow,
        "remember_answer",
        fail_with("disk unavailable"),
    )
    with pytest.raises(OSError, match="disk unavailable"):
        worker.apply_command(request, "reply-1")
    assert journal() == ("pending", 1)
    monkeypatch.setattr(workflow, "remember_answer", original)
    worker.apply_command({**request, "value": "4.0"}, "reply-1")
    assert workflow.approved_answers(application_id)["gpa"]["value"] == "3.5"
    with workflow.db() as conn:
        assert conn.execute("SELECT value FROM answer_memory").fetchone()[0] == "3.5"
        events = conn.execute(
            "SELECT data FROM application_events WHERE kind='owner_answer'"
        ).fetchall()
    assert len(events) == 1 and json.loads(events[0][0])["value"] == "3.5"
    assert journal() == ("applied", 0)


def test_lifecycle_and_reply_roll_back_together_when_timeline_write_fails(held, monkeypatch):
    application_id, _ = held
    original = workflow.db

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if "VALUES(?, 'lifecycle', ?, ?)" in sql:
                raise OSError("interrupted transaction")
            return self.connection.execute(sql, parameters)

    @contextmanager
    def interrupted():
        with original() as connection:
            yield FailingConnection(connection)

    monkeypatch.setattr(workflow, "db", interrupted)
    request = {"kind": "resume", "application_id": application_id}
    with pytest.raises(OSError, match="interrupted transaction"):
        worker.apply_command(request, "reply-1")
    monkeypatch.setattr(workflow, "db", original)
    assert workflow.get(application_id)["status"] == "NEEDS_USER"
    assert journal() == ("pending", 1)
    command_effects.recover(worker.close_deferred_tab)
    assert workflow.get(application_id)["status"] == "QUEUED"
    assert journal() == ("applied", 0)


def test_projection_retry_never_rewinds_application_or_duplicates_reply(held, monkeypatch):
    application_id, _ = held
    original = workflow.flush_events
    monkeypatch.setattr(workflow, "flush_events", fail_with("interrupted delivery"))
    with pytest.raises(OSError, match="interrupted delivery"):
        worker.apply_command({"kind": "resume", "application_id": application_id}, "reply-1")
    assert workflow.get(application_id)["status"] == "QUEUED"
    assert journal() == ("applied", 1)
    monkeypatch.setattr(workflow, "flush_events", original)
    workflow.set_state(application_id, "PREPARING")
    command_effects.recover(worker.close_deferred_tab)
    assert workflow.get(application_id)["status"] == "PREPARING"
    assert "status: PREPARING" in vault.note_path(workflow.get(application_id)).read_text()
    with workflow.db() as conn:
        events = conn.execute(
            "SELECT count(*) FROM application_events WHERE kind='resume_requested'"
        ).fetchone()[0]
    assert events == 1 and journal() == ("applied", 0)


def test_tick_finishes_saved_replies_before_polling_or_work(held, monkeypatch):
    application_id, _ = held
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True})
    original = command_effects.commit_local
    monkeypatch.setattr(
        command_effects,
        "commit_local",
        fail_with("process stopped"),
    )
    with pytest.raises(OSError):
        worker.apply_command({"kind": "resume", "application_id": application_id}, "reply-1")
    monkeypatch.setattr(command_effects, "commit_local", original)

    def poll():
        assert workflow.get(application_id)["status"] == "QUEUED"
        assert journal() == ("applied", 0)
        return False

    monkeypatch.setattr(worker, "poll_commands", poll)
    monkeypatch.setattr(worker, "work", lambda *a: {"idle": True})
    assert worker.tick() == {"idle": True}


@pytest.mark.parametrize("status", ["SUBMITTING", "UNKNOWN_SUBMISSION", "APPLIED", "INTERVIEW"])
def test_interrupted_resume_cannot_rewind_a_send_or_block_other_commands(held, monkeypatch, status):
    application_id, _ = held
    original = command_effects.commit_local

    def crash(*args):
        raise OSError("process stopped")

    monkeypatch.setattr(command_effects, "commit_local", crash)
    with pytest.raises(OSError):
        worker.apply_command({"kind": "resume", "application_id": application_id}, "reply-1")
    monkeypatch.setattr(command_effects, "commit_local", original)
    workflow.set_state(application_id, status)
    command_effects.recover(worker.close_deferred_tab)
    assert workflow.get(application_id)["status"] == status
    assert journal() == ("failed", 0)
    another = workflow.enqueue("https://jobs.example.com/another")["application_id"]
    worker.apply_command({"kind": "defer", "application_id": another}, "reply-2")
    assert workflow.get(another)["status"] == "DEFERRED"


def test_overlapping_command_callers_apply_each_reply_once(held):
    application_id, _ = held
    request = {"kind": "resume", "application_id": application_id}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker.apply_command, request, f"reply-{i % 12}") for i in range(96)]
        for result in futures:
            result.result(timeout=15)
    with workflow.db() as conn:
        assert (
            conn.execute("SELECT count(*) FROM owner_commands WHERE status='applied'").fetchone()[0]
            == 12
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind='resume_requested'"
            ).fetchone()[0]
            == 12
        )
        assert conn.execute("SELECT count(*) FROM owner_command_effects").fetchone()[0] == 0
    assert workflow.get(application_id)["status"] == "QUEUED"


def test_recovery_repairs_answer_mirror_after_its_write_failed(held, monkeypatch):
    application_id, state = held
    field = {"key": "gpa", "label": "Current GPA", "kind": "text", "options": [], "required": True}
    directory = state / "applications" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "observation.json").write_text(json.dumps({"fields": [field]}))
    original = vault.sync_answers
    monkeypatch.setattr(vault, "sync_answers", fail_with("vault unavailable"))
    with pytest.raises(OSError, match="vault unavailable"):
        worker.apply_command(
            {
                "kind": "answer",
                "application_id": application_id,
                "field_key": "gpa",
                "value": "3.5",
            },
            "reply-1",
        )
    assert journal() == ("applied", 1)
    monkeypatch.setattr(vault, "sync_answers", original)
    worker.recover_commands()
    assert "| Current GPA | 3.5 |" in (vault.vault_root() / "Answers.md").read_text()
    assert journal() == ("applied", 0)
