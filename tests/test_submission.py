import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from erga_autopilot import submission, worker, workflow
from erga_autopilot.onboarding import approve, digest, draft, propose, read_approved
from erga_autopilot.submission import GreenhouseV1, package_digest

URL = "https://job-boards.greenhouse.io/example/jobs/123"
FINAL = [{"ref": "0", "label": "Submit application"}]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["greenhouse_v1"],
        },
    )
    return tmp_path / "state"


def field(key, label, value, **extra):
    base = {
        "key": key,
        "label": label,
        "name": key,
        "kind": "text",
        "role": None,
        "required": True,
        "disabled": False,
        "readonly": False,
        "value": value,
        "checked": False,
        "selected": None,
        "selection_code": None,
        "options": [],
    }
    return {**base, **extra}


def ready_application(state, url=URL):
    application_id = workflow.enqueue(url)["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)
    approved = read_approved()
    (directory / "profile.json").write_text(json.dumps(approved))
    (directory / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic resume")
    sha = hashlib.sha256((directory / "resume.pdf").read_bytes()).hexdigest()
    (directory / "resume-manifest.json").write_text(
        json.dumps({"ready": True, "resume_sha256": sha})
    )
    fields = [
        field("aaaaaaaaaaaa", "First name", "Alex"),
        field("bbbbbbbbbbbb", "Country", "", role="combobox", selected="United States"),
        field("cccccccccccc", "Resume", None, kind="file", required=False),
    ]
    package = {
        "run_id": application_id,
        "url": url,
        "profile_hash": approved["profile_hash"],
        "resume_sha256": sha,
        "filled": [
            {"label": "First name", "value": "Alex", "source": "identity.legal_first_name"},
            {"label": "Resume", "source": "frozen approved base resume", "sha256": sha},
        ],
        "pending": [],
        "form_state": fields,
        "final_controls": FINAL,
        "submission_enabled": False,
    }
    package["package_hash"] = package_digest(package)
    (directory / "package.json").write_text(json.dumps(package))
    workflow.set_state(application_id, "READY_FOR_REVIEW", package_hash=package["package_hash"])
    current = {"url": url, "fields": [dict(f) for f in fields], "final_controls": FINAL}
    return application_id, package, current


def after_page(url, marker=True, fields=()):
    return {
        "url": url,
        "fields": list(fields),
        "final_controls": [],
        "ats_markers": {"greenhouse_confirmation": marker},
    }


def test_greenhouse_confirmation_requires_every_signal():
    accepted = [{"host": "boards.greenhouse.io", "path": "/example/jobs/123", "status": 200}]
    confirmation = URL + "/confirmation?utm=1"
    assert GreenhouseV1.confirmed(URL, after_page(confirmation), accepted)["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation), [])["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(URL), accepted)["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation, marker=False), accepted)[
        "confirmed"
    ]
    still_form = after_page(confirmation, fields=[field("a", "Email", "")])
    assert not GreenhouseV1.confirmed(URL, still_form, accepted)["confirmed"]
    other_job = [{"host": "boards.greenhouse.io", "path": "/example/jobs/999", "status": 200}]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation), other_job)["confirmed"]
    rejected = accepted + [
        {"host": "boards.greenhouse.io", "path": "/example/jobs/123", "status": 422}
    ]
    checks = GreenhouseV1.confirmed(URL, after_page(confirmation), rejected)
    assert checks["post_rejected"] and not checks["confirmed"]
    assert not GreenhouseV1.matches("https://job-boards.greenhouse.io/example")
    assert not GreenhouseV1.matches("https://attacker.example/example/jobs/123")


def test_preflight_accepts_only_the_reviewed_unchanged_form(state):
    application_id, package, current = ready_application(state)
    assert submission.preflight(application_id, package["package_hash"], current)["url"] == URL
    with pytest.raises(PermissionError, match="package changed"):
        submission.preflight(application_id, "0" * 64, current)
    changed = {
        **current,
        "fields": [{**current["fields"][0], "value": "Injected"}, *current["fields"][1:]],
    }
    with pytest.raises(PermissionError, match="form changed"):
        submission.preflight(application_id, package["package_hash"], changed)
    with pytest.raises(PermissionError, match="destination"):
        submission.preflight(
            application_id, package["package_hash"], {**current, "url": URL + "?x"}
        )
    unselected = {
        **current,
        "fields": [
            current["fields"][0],
            {**current["fields"][1], "selected": None},
            current["fields"][2],
        ],
    }
    with pytest.raises(PermissionError, match="form changed"):
        submission.preflight(application_id, package["package_hash"], unselected)
    (state / "applications" / application_id / "resume.pdf").write_bytes(b"tampered")
    with pytest.raises(PermissionError, match="resume"):
        submission.preflight(application_id, package["package_hash"], current)


def test_claim_needs_exact_owner_approval_and_never_repeats(state):
    application_id, package, _current = ready_application(state)
    package_hash = package["package_hash"]
    with pytest.raises(PermissionError, match="owner approval"):
        submission.claim_attempt(application_id, package_hash, "msg-1")
    with pytest.raises(PermissionError, match="exact package"):
        worker.apply_command(
            {"kind": "submit", "application_id": application_id, "package_hash": "1" * 64}, "msg-0"
        )
    worker.apply_command(
        {"kind": "submit", "application_id": application_id, "package_hash": package_hash}, "msg-1"
    )
    submission.claim_attempt(application_id, package_hash, "msg-1")
    assert workflow.get(application_id)["status"] == "SUBMITTING"
    with pytest.raises(PermissionError):
        submission.claim_attempt(application_id, package_hash, "msg-1")
    submission.finish_attempt(
        application_id, "UNKNOWN_SUBMISSION", {"package_hash": package_hash, "reason": "timeout"}
    )
    assert workflow.get(application_id)["status"] == "UNKNOWN_SUBMISSION"
    with pytest.raises(PermissionError, match="cannot be prepared"):
        worker.apply_command({"kind": "resume", "application_id": application_id}, "msg-2")
    worker.apply_command(
        {"kind": "reconcile", "application_id": application_id, "outcome": "not-submitted"}, "msg-3"
    )
    assert workflow.get(application_id)["status"] == "NEEDS_USER"
    workflow.set_state(application_id, "READY_FOR_REVIEW", package_hash=package_hash)
    worker.apply_command(
        {"kind": "submit", "application_id": application_id, "package_hash": package_hash}, "msg-4"
    )
    submission.claim_attempt(application_id, package_hash, "msg-4")
    submission.finish_attempt(application_id, "APPLIED", {"package_hash": package_hash})
    assert workflow.get(application_id)["status"] == "APPLIED"
    with workflow.db() as conn:
        assert (
            conn.execute("SELECT status FROM live_submission_attempts").fetchone()[0] == "APPLIED"
        )
        kinds = [r[0] for r in conn.execute("SELECT kind FROM application_events ORDER BY id")]
    assert (
        "submission_unknown" in kinds and "submission_confirmed" in kinds and "lifecycle" in kinds
    )
    assert (state / "applications" / application_id / "receipt.json").exists()


def test_owner_commands_for_submission_lifecycle_parse_strictly():
    good = lambda text: {"author": {"id": "owner"}, "content": text}
    parse = lambda text: worker.parse_command(good(text), "owner", "control", {"control"})
    assert parse("submit abcdef012345 " + "a" * 64)["kind"] == "submit"
    assert parse("submit abcdef012345 " + "a" * 7) is None
    assert parse("submit abcdef012345 " + "a" * 12)["package_hash"] == "a" * 12
    assert parse("proceed abcdef012345") == {"kind": "proceed", "application_id": "abcdef012345"}
    assert parse("reconcile abcdef012345 applied")["outcome"] == "applied"
    assert parse("reconcile abcdef012345 maybe") is None
    assert parse("please submit abcdef012345 " + "a" * 64) is None


def test_tick_executes_one_approved_submission_and_recovers_crashed_runs(state, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "submission_enabled": True, "submit_adapters": ["greenhouse_v1"]},
    )
    monkeypatch.setattr(worker, "poll_commands", lambda: None)
    monkeypatch.setattr(worker, "discord", lambda *a, **k: {})
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {})
    application_id, package, _ = ready_application(state)
    stale = workflow.enqueue("https://jobs.example.com/crashed")["application_id"]
    workflow.set_state(stale, "PREPARING")
    with workflow.db() as conn:
        conn.execute(
            "UPDATE application_queue SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), stale),
        )
    calls = []
    monkeypatch.setattr(
        worker,
        "browser_call",
        lambda action, **kw: calls.append((action, kw)) or {"status": "APPLIED"},
    )
    worker.apply_command(
        {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": package["package_hash"],
        },
        "msg-1",
    )
    result = worker.tick()
    assert [c[0] for c in calls] == ["submit"]
    assert calls[0][1]["package_hash"] == package["package_hash"]
    assert result["submissions"][0]["outcome"] == "executed"
    assert workflow.get(stale)["status"] == "NEEDS_USER"
    assert workflow.get(stale)["error"] == "preparation_interrupted"
    with workflow.db() as conn:
        assert (
            conn.execute("SELECT status FROM owner_commands WHERE message_id='msg-1'").fetchone()[0]
            == "executed"
        )
    assert worker.tick() == {
        "idle": True
    }  # nothing approved; the recovered run now waits on the owner


def test_failed_submission_request_leaves_the_package_reviewable(state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True})
    monkeypatch.setattr(worker, "poll_commands", lambda: None)
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {})
    application_id, package, _ = ready_application(state)
    worker.apply_command(
        {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": package["package_hash"],
        },
        "msg-1",
    )

    def refuse(action, **kw):
        raise RuntimeError("The visible form changed after review")

    monkeypatch.setattr(worker, "browser_call", refuse)
    result = worker.tick()
    assert result["submissions"][0]["outcome"] == "failed"
    assert workflow.get(application_id)["status"] == "READY_FOR_REVIEW"
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM live_submission_attempts").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM owner_commands WHERE message_id='msg-1'").fetchone()[0]
            == "failed"
        )


def test_queue_holds_for_waiting_applications_unless_owner_resumes(state):
    first = workflow.enqueue("https://jobs.example.com/a")["application_id"]
    second = workflow.enqueue("https://jobs.example.com/b")["application_id"]
    workflow.set_state(first, "NEEDS_USER")
    assert worker.next_queued(1) is None
    assert worker.next_queued(2) == second
    worker.apply_command({"kind": "resume", "application_id": first}, "msg-1")
    assert worker.next_queued(1) == first
