"""Crash at recording boundaries; an employer result survives without another send."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
import test_submission

from rove import submission, submission_records, worker, workflow

state = test_submission.state


@pytest.fixture
def claimed(state, monkeypatch):
    application_id, package, _ = test_submission.ready_application(state)
    worker.apply_command(
        {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": package["package_hash"],
        },
        "send-1",
    )
    submission.claim_attempt(application_id, package["package_hash"], "send-1")
    monkeypatch.setattr(submission, "erga_confirm", lambda app: {"synced": True})
    return application_id, {
        "status": "APPLIED",
        "package_hash": package["package_hash"],
        "reason": "Synthetic employer receipt",
    }


def saved(application_id):
    with workflow.db() as conn:
        attempt = conn.execute(
            "SELECT status FROM live_submission_attempts WHERE application_id=?", (application_id,)
        ).fetchone()[0]
        outcomes = conn.execute("SELECT count(*) FROM submission_outcomes").fetchone()[0]
    return workflow.get(application_id)["status"], attempt, outcomes


def fail(*args, **kwargs):
    raise OSError("recording interrupted")


def test_failed_receipt_write_keeps_outcome_and_queue_together(claimed, state, monkeypatch):
    application_id, evidence = claimed
    original = submission_records.repair_receipt
    monkeypatch.setattr(submission_records, "repair_receipt", fail)
    with pytest.raises(OSError, match="interrupted"):
        submission.finish_attempt(application_id, "APPLIED", evidence, expect=submission.CLAIMED)
    assert saved(application_id) == ("APPLIED", "APPLIED", 1)
    # A stale daemon cannot turn the committed success back into uncertainty.
    assert not submission.finish_attempt(
        application_id, "UNKNOWN_SUBMISSION", {}, expect=submission.CLAIMED
    )
    monkeypatch.setattr(submission_records, "repair_receipt", original)
    submission.recover_outcomes()
    submission.recover_outcomes()
    receipt = json.loads((state / "applications" / application_id / "receipt.json").read_text())
    assert receipt["reason"] == evidence["reason"] and receipt["erga"]["synced"]
    assert receipt["status"] == "APPLIED"
    assert saved(application_id) == ("APPLIED", "APPLIED", 0)
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind='submission_confirmed'"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(PermissionError):
        submission.claim_attempt(application_id, evidence["package_hash"], "send-1")


def test_failed_journal_insert_rolls_back_the_whole_outcome(claimed, monkeypatch):
    application_id, evidence = claimed
    original = workflow.db

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, values=()):
            if sql.startswith("INSERT INTO submission_outcomes"):
                raise OSError("journal full")
            return self.connection.execute(sql, values)

    @contextmanager
    def interrupted():
        with original() as conn:
            yield Connection(conn)

    monkeypatch.setattr(workflow, "db", interrupted)
    with pytest.raises(OSError, match="journal full"):
        submission.finish_attempt(application_id, "APPLIED", evidence)
    monkeypatch.setattr(workflow, "db", original)
    assert saved(application_id) == ("SUBMITTING", "SUBMITTING", 0)
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind='submission_confirmed'"
            ).fetchone()[0]
            == 0
        )


def test_projection_retry_preserves_later_recruiting_state(claimed, monkeypatch):
    application_id, evidence = claimed
    original = workflow.flush_events
    monkeypatch.setattr(workflow, "flush_events", fail)
    with pytest.raises(OSError):
        submission.finish_attempt(application_id, "APPLIED", evidence)
    workflow.set_state(application_id, "INTERVIEW")
    syncs = []
    monkeypatch.setattr(submission, "erga_confirm", lambda app: syncs.append(app) or {})
    monkeypatch.setattr(workflow, "flush_events", original)
    submission.recover_outcomes()
    assert saved(application_id) == ("INTERVIEW", "APPLIED", 0)
    assert syncs == []


@pytest.mark.parametrize(
    "outcome,expected", [("applied", "APPLIED"), ("not-submitted", "NEEDS_USER")]
)
def test_reconciliation_command_survives_crash_before_recording(
    claimed, monkeypatch, outcome, expected
):
    application_id, evidence = claimed
    submission.finish_attempt(application_id, "UNKNOWN_SUBMISSION", evidence)
    original = submission_records.commit
    monkeypatch.setattr(submission_records, "commit", fail)
    command = {"kind": "reconcile", "application_id": application_id, "outcome": outcome}
    with pytest.raises(OSError):
        worker.apply_command(command, "reconcile-1")
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT status FROM owner_commands WHERE message_id='reconcile-1'"
            ).fetchone()[0]
            == "pending"
        )
    monkeypatch.setattr(submission_records, "commit", original)
    worker.recover_commands()
    worker.apply_command(command, "reconcile-1")
    assert workflow.get(application_id)["status"] == expected
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT status FROM owner_commands WHERE message_id='reconcile-1'"
            ).fetchone()[0]
            == "applied"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind='reconcile_requested'"
            ).fetchone()[0]
            == 1
        )


def test_unknown_submission_card_is_not_duplicated_on_recovery(claimed, monkeypatch):
    application_id, evidence = claimed
    original = workflow.refresh_status
    monkeypatch.setattr(workflow, "refresh_status", fail)
    with pytest.raises(OSError):
        submission.finish_attempt(application_id, "UNKNOWN_SUBMISSION", evidence)
    monkeypatch.setattr(workflow, "refresh_status", original)
    submission.recover_outcomes()
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM owner_notices WHERE delivery!='withdrawn'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind='needs_action'"
            ).fetchone()[0]
            == 1
        )


def test_recovery_never_replaces_a_newer_question_in_the_same_state(claimed, monkeypatch):
    application_id, evidence = claimed
    original = submission_records.repair_receipt
    monkeypatch.setattr(submission_records, "repair_receipt", fail)
    with pytest.raises(OSError):
        submission.finish_attempt(application_id, "NOT_SUBMITTED", evidence)
    workflow.transition(application_id, "NEEDS_USER", "New preparation needs a fact")
    workflow.action_needed(application_id, "New unanswered question", commands=["go"])
    monkeypatch.setattr(submission_records, "repair_receipt", original)
    submission.recover_outcomes()
    assert workflow.latest_hold(application_id)["reason"] == "New unanswered question"


def test_reconciliation_remains_applied_when_receipt_permissions_fail(claimed, monkeypatch):
    application_id, evidence = claimed
    submission.finish_attempt(application_id, "UNKNOWN_SUBMISSION", evidence)
    original = submission_records.repair_receipt

    def denied(*args):
        raise PermissionError("receipt read-only")

    monkeypatch.setattr(submission_records, "repair_receipt", denied)
    with pytest.raises(PermissionError, match="read-only"):
        worker.apply_command(
            {"kind": "reconcile", "application_id": application_id, "outcome": "applied"},
            "reconcile-1",
        )
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT status FROM owner_commands WHERE message_id='reconcile-1'"
            ).fetchone()[0]
            == "applied"
        )
    monkeypatch.setattr(submission_records, "repair_receipt", original)
    worker.recover_commands()
    assert saved(application_id) == ("APPLIED", "APPLIED", 0)


def test_abrupt_process_exit_after_commit_is_recovered_without_a_browser(claimed, state):
    application_id, _ = claimed
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from rove import submission, submission_records
def interrupted(*args):
    os._exit(77)
submission_records.repair_receipt = interrupted
submission.finish_attempt(sys.argv[1], 'APPLIED', {'reason': 'Synthetic employer receipt'},
                          expect=submission.CLAIMED)
""",
            application_id,
        ],
        check=False,
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 77, child.stderr.decode()
    assert saved(application_id) == ("APPLIED", "APPLIED", 1)
    worker.recover_commands()
    receipt = json.loads((state / "applications" / application_id / "receipt.json").read_text())
    assert receipt["status"] == "APPLIED" and saved(application_id) == ("APPLIED", "APPLIED", 0)


def test_competing_outcomes_record_exactly_one_claim_result(claimed, state):
    application_id, _ = claimed

    def finish(status):
        return status, submission.finish_attempt(
            application_id, status, {}, expect=submission.CLAIMED
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, ["APPLIED", "UNKNOWN_SUBMISSION"]))
    winners = [status for status, kept in results if kept]
    assert len(winners) == 1
    receipt = json.loads((state / "applications" / application_id / "receipt.json").read_text())
    assert receipt["status"] == winners[0]
    assert saved(application_id) == (winners[0], winners[0], 0)
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM application_events WHERE kind IN "
                "('submission_confirmed','submission_unknown')"
            ).fetchone()[0]
            == 1
        )
