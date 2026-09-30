"""Real headless browser: the prepare guard blocks submits until one owner-approved
submission is armed, and success needs the ATS contract, not just a click."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from erga_autopilot import live_browser, submission, worker, workflow
from erga_autopilot.live_browser import RecruitingBrowser
from erga_autopilot.onboarding import approve, digest, draft, propose

FORM = b"""<!doctype html><title>Synthetic Board</title><form id="application-form">
<label for="f">First name</label><input id="f" name="first" required>
<label for="e">Email</label><input id="e" name="email" required>
<label for="r">Resume</label><input id="r" name="resume" type="file">
<button type="submit">Submit application</button></form><p id="error"></p>
<script>document.querySelector('form').addEventListener('submit', async e => {
  e.preventDefault();
  const r = await fetch(location.pathname, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  if (r.ok) { location.assign(location.pathname + '/confirmation'); }
  else { document.querySelector('#error').textContent = 'rejected'; }
});</script>"""
CONFIRMATION = b"""<!doctype html><title>Thanks</title><div class="confirmation">
<div class="confirmation__content"><h1>Thank you for applying to Acme.</h1></div></div>"""


class Board(BaseHTTPRequestHandler):
    posts: ClassVar[list[str]] = []
    reject: ClassVar[set[str]] = set()

    def do_GET(self):
        body = CONFIRMATION if self.path.endswith("/confirmation") else FORM
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Board.posts.append(self.path)
        self.send_response(422 if self.path in Board.reject else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args):
        pass


class SyntheticV1(submission.GreenhouseV1):
    name = "synthetic_v1"
    board_hosts = ("127.0.0.1",)
    submit_host = "127.0.0.1"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    pdf = tmp_path / "approved.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic approved resume")
    propose(
        "identity",
        {"legal_first_name": "Alex", "legal_last_name": "Example", "email": "alex@example.invalid"},
        digest(draft()),
    )
    propose("evidence", {"resume_path": str(pdf)}, digest(draft()))
    approve(digest(draft()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Board.posts.clear()
    Board.reject.clear()
    identity = lambda value: value
    monkeypatch.setattr(live_browser, "validate_destination", identity)
    monkeypatch.setattr(live_browser, "public_link", identity)
    monkeypatch.setattr(live_browser, "lookup_job_link", lambda url: {"in_feed": False})
    monkeypatch.setattr(live_browser, "approved_ats", lambda url: True)
    monkeypatch.setattr(live_browser, "job_scope", lambda url: ("synthetic",))
    monkeypatch.setattr(workflow, "public_link", identity)
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": False, "submission_enabled": True, "submit_adapters": ["synthetic_v1"]},
    )
    monkeypatch.setitem(submission.ADAPTERS, "synthetic_v1", SyntheticV1)
    monkeypatch.setattr(
        submission, "erga_confirm", lambda app: {"synced": False, "warning": "test"}
    )
    monkeypatch.setattr(submission, "CONFIRMATION_TIMEOUT_MS", 3000)
    runtime = RecruitingBrowser(headless=True)
    try:
        yield runtime, f"http://127.0.0.1:{server.server_port}", tmp_path / "state"
    finally:
        if runtime.context:
            runtime.context.close()
        if runtime.playwright:
            runtime.playwright.stop()
        server.shutdown()
        server.server_close()


def prepared(runtime, base, state, job):
    opened = runtime.open(f"{base}/acme/jobs/{job}")
    run_id = opened["run_id"]
    directory = state / "applications" / run_id
    resume = directory / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 frozen synthetic resume")
    sha = hashlib.sha256(resume.read_bytes()).hexdigest()
    (directory / "resume-manifest.json").write_text(
        json.dumps({"ready": True, "resume_sha256": sha})
    )
    result = runtime.prepare(run_id)
    assert result["status"] == "READY_FOR_REVIEW" and not result["pending"]
    assert {f["label"] for f in result["filled"]} == {"First name", "Email", "Resume"}
    return run_id, result["package_hash"]


def test_guard_blocks_native_submit_until_one_approved_attempt_is_armed(board):
    runtime, base, state = board
    run_id, package_hash = prepared(runtime, base, state, 7)
    url = runtime.page.url
    runtime.page.locator('[data-autopilot-submit="0"]').click()
    runtime.page.wait_for_timeout(500)
    assert Board.posts == [] and runtime.page.url == url
    with pytest.raises(PermissionError, match="owner approval"):
        submission.submit(runtime, run_id, package_hash, "msg-0")
    assert Board.posts == []
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert result["status"] == "APPLIED", result
    assert result["checks"]["confirmed"] and Board.posts == ["/acme/jobs/7"]
    assert runtime.page.url.endswith("/acme/jobs/7/confirmation")
    assert workflow.get(run_id)["status"] == "APPLIED"
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert (
        receipt["confirmation_url"].endswith("/confirmation") and receipt["erga"]["synced"] is False
    )
    assert (state / "applications" / run_id / "form-before-submit.png").exists()
    with pytest.raises(PermissionError):
        submission.submit(runtime, run_id, package_hash, "msg-1")
    assert Board.posts == ["/acme/jobs/7"]


def test_rejected_submission_stays_unknown_until_the_owner_reconciles(board):
    runtime, base, state = board
    Board.reject.add("/acme/jobs/8")
    run_id, package_hash = prepared(runtime, base, state, 8)
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert result["status"] == "UNKNOWN_SUBMISSION"
    assert result["checks"]["post_rejected"] and not result["checks"]["confirmed"]
    assert "rejected" in result["reason"]
    assert workflow.get(run_id)["status"] == "UNKNOWN_SUBMISSION"
    with pytest.raises(PermissionError):
        submission.claim_attempt(run_id, package_hash, "msg-1")
    submission.reconcile(run_id, "not-submitted", "msg-2")
    assert workflow.get(run_id)["status"] == "NEEDS_USER"
    assert Board.posts == ["/acme/jobs/8"]
