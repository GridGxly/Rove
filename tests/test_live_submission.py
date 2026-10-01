"""Real headless browser: the prepare guard blocks submits until one owner-approved
submission is armed, and success needs the ATS contract, not just a click."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from rove import live_browser, submission, worker, workflow
from rove.live_browser import RecruitingBrowser
from rove.onboarding import approve, digest, draft, propose

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
YESNO = (
    '<div class="yesno"><button type="button" aria-pressed="false" data-option="yes">Yes</button>'
    '<button type="button" aria-pressed="false" data-option="no">No</button>'
    '<input type="checkbox" name="{name}" tabindex="-1" style="display:none"></div>'
)
ASHBY_FORM = (
    """<!doctype html><title>Synthetic Board</title>
<div class="entry"><label class="title required" for="_name">Name</label><input id="_name" name="name" required></div>
<div class="entry"><label class="title required" for="_email">Email</label><input id="_email" name="email" type="email" required></div>
<div class="entry"><label class="title required" for="_resume">Resume</label><input id="_resume" name="resume" type="file"></div>
<div class="entry"><label class="title required" for="_location">Location</label><div><input role="combobox" placeholder="Start typing..." aria-haspopup="listbox"></div></div>
<div class="entry"><label class="title required" for="q1">Are you legally authorized to work in the United States?</label>"""
    + YESNO.format(name="q1")
    + """</div>
<div class="entry"><label class="title required" for="q2">Will you now or in the future, require sponsorship for employment visa status (e.g., H1B visa status)?</label>"""
    + YESNO.format(name="q2")
    + """</div>
<div class="entry"><label class="title required" for="q3">Are you comfortable working out of our NYC Office 5 days/week?</label>"""
    + YESNO.format(name="q3")
    + """</div>
<div class="entry"><label class="title required" for="q4">Describe a project you built</label><textarea id="q4" name="q4"></textarea></div>
<fieldset class="entry"><label class="title required" for="g1">What is your expected graduation year?</label>
<div><input type="radio" id="g1-0" name="g1"><label for="g1-0">December 2026</label></div>
<div><input type="radio" id="g1-1" name="g1"><label for="g1-1">Spring 2027</label></div>
<div><input type="radio" id="g1-2" name="g1"><label for="g1-2">December 2027</label></div>
<div><input type="radio" id="g1-3" name="g1"><label for="g1-3">Other</label></div></fieldset>
<button type="button">Submit Application</button>
<script>document.querySelectorAll('.yesno').forEach(box=>box.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
box.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed','false'));b.setAttribute('aria-pressed','true');
box.querySelector('input').checked=b.dataset.option==='yes';})));</script>"""
).encode()
REGISTER = b"""<!doctype html><title>Create Account</title><h1>Create an account to apply</h1>
<form id="signup"><label for="e">Email</label><input id="e" type="email" name="email" required>
<label for="p">Password</label><input id="p" type="password" name="password" required>
<label for="c">Confirm Password</label><input id="c" type="password" name="confirm" required>
<input id="t" type="checkbox" name="terms"><label for="t">I agree to the terms</label>
<button type="button" id="go">Create Account</button></form>
<script>document.getElementById('go').onclick=()=>{document.body.innerHTML='<h1>Verify your email</h1><p>We sent a link to confirm your email address.</p>'}</script>"""
LOGIN = b"""<!doctype html><title>Sign in</title><form id="login"><label for="e">Email</label><input id="e" type="email" name="email">
<label for="p">Password</label><input id="p" type="password" name="password">
<button type="button" id="go">Sign in</button></form>
<script>document.getElementById('go').onclick=()=>{document.body.innerHTML='<label for="f">First name</label><input id="f"><button>Submit application</button>'}</script>"""
TWO_STEP = b"""<!doctype html><title>Two steps</title><form id="s1">
<label for="n">First name</label><input id="n" name="first" required>
<label for="e">Email</label><input id="e" name="email" type="email" required>
<button type="button" id="next">Continue</button></form>
<script>document.getElementById('next').onclick=()=>{document.body.innerHTML='<form><label for="l">Last name</label><input id="l" name="last" required><button type="button">Submit Application</button></form>'}</script>"""
LATE_FORM = b"""<!doctype html><title>Late Board</title>
<nav>Careers Explore Jobs Manufacturing Internships About Us Profile Help</nav>
<div id="consent"><p>Help us improve our website with cookies. We use cookies and process data from
your device to analyze website performance, personalize ad content, and improve your experience.
View cookie settings for more information.</p>
<button type="button" onclick="choose('accepted')">Accept</button>
<button type="button" onclick="choose('rejected')">Reject</button></div>
<div class="spinner" style="width:40px;height:40px">loading</div>
<script>
function choose(v){document.documentElement.dataset.consent=v;document.querySelector('#consent').remove();}
setTimeout(()=>{document.querySelector('.spinner').remove();
document.body.insertAdjacentHTML('beforeend','<form id="application-form"><label for="f">First name</label><input id="f" name="first" required><label for="e">Email</label><input id="e" name="email" required><label for="r">Resume</label><input id="r" name="resume" type="file"><button type="submit">Submit application</button></form>');},1500);
</script>"""
CONFIRMATION = b"""<!doctype html><title>Thanks</title><div class="confirmation">
<div class="confirmation__content"><h1>Thank you for applying to Acme.</h1></div></div>"""
# An employer's own site: no ATS contract, a plain thank-you page at a URL that does not
# look like a confirmation, and an inline validation message when the server says no.
GENERIC_FORM = b"""<!doctype html><title>Acme Careers</title><form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="e">Email</label><input id="e" name="email" required aria-describedby="error"></div>
<div><label for="r">Resume</label><input id="r" name="resume" type="file"></div>
<button type="submit">Submit application</button></form><p id="error" role="alert"></p>
<script>document.querySelector('form').addEventListener('submit', async e => {
  e.preventDefault();
  const r = await fetch(location.pathname, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  if (r.ok) { location.assign(location.pathname + '/done'); }
  else { document.querySelector('#e').setAttribute('aria-invalid', 'true');
         document.querySelector('#error').textContent = 'Email address is invalid. Please correct it and try again.'; }
});</script>"""
GENERIC_CONFIRMATION = b"""<!doctype html><title>Acme Careers</title>
<p>Thank you for applying to Acme. We received your application.</p>"""
# A public Lever posting: the apply page posts its multipart form natively once the CAPTCHA
# hands it a token, success is the /thanks page with its heading and no form, and a send the
# CAPTCHA rejected comes back as the emptied form under a verification error.
LEVER_POSTING = "0f3c7a1e-5b2d-4c8e-9a6f-1d2e3f4a5b6c"
LEVER_POSTING_REJECTED = "7b1e9d2c-3a4f-4e5b-8c6d-9e0f1a2b3c4d"
LEVER_FORM = b"""<!doctype html><title>Acme - Software Intern</title>
<form id="application-form" enctype="multipart/form-data" method="POST">
<label for="f">First name</label><input id="f" name="name" required>
<label for="e">Email</label><input id="e" name="email" type="email" required>
<label for="r">Resume</label><input id="r" name="resume" type="file">
<input id="hcaptchaResponseInput" type="hidden" name="h-captcha-response" value="">
<button id="hcaptchaSubmitBtn" type="submit" class="hidden" style="display:none"></button>
<button id="btn-submit" type="button" data-qa="btn-submit">Submit application</button></form>
<script>document.getElementById('btn-submit').addEventListener('click', () => {
  document.getElementById('hcaptchaResponseInput').value = 'synthetic-token';
  document.getElementById('hcaptchaSubmitBtn').click();
});</script>"""
LEVER_THANKS = b"""<!doctype html><title>Acme - Software Intern</title>
<div class="section page-centered"><h3 data-qa="msg-submit-success">Application submitted!</h3>
<p>Thanks for applying to Acme.</p></div>"""
LEVER_VERIFICATION_ERROR = LEVER_FORM.replace(
    b"<form ",
    b'<p class="error-message">There was an error verifying your application</p><form ',
)


class Board(BaseHTTPRequestHandler):
    posts: ClassVar[list[str]] = []
    reject: ClassVar[set[str]] = set()

    def do_GET(self):
        if self.path.endswith("/confirmation"):
            body = CONFIRMATION
        elif self.path.endswith("/done"):
            body = GENERIC_CONFIRMATION
        elif self.path.endswith(("/jobs/15", "/jobs/16")):
            body = GENERIC_FORM
        elif self.path.endswith("/jobs/9"):
            body = ASHBY_FORM
        elif self.path.endswith("/jobs/13"):
            body = TWO_STEP
        elif self.path.endswith("/jobs/11"):
            body = REGISTER
        elif self.path.endswith("/jobs/12"):
            body = LOGIN
        elif self.path.endswith("/jobs/14"):
            body = LATE_FORM
        elif self.path.endswith("/apply"):
            body = LEVER_FORM
        elif self.path.endswith("/thanks"):
            body = LEVER_THANKS
        else:
            body = FORM
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Board.posts.append(self.path)
        if self.path.endswith("/apply"):
            # Lever's form posts natively: a redirect to /thanks, or the re-rendered form.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path in Board.reject:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(LEVER_VERIFICATION_ERROR)
            else:
                self.send_response(303)
                self.send_header("Location", self.path.removesuffix("/apply") + "/thanks")
                self.end_headers()
            return
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


class LocalGenericV1(submission.GenericV1):
    schemes = ("http", "https")  # the synthetic board is plain http on loopback


class LocalLeverV1(submission.LeverV1):
    hosts = ("127.0.0.1",)


def lever_first(monkeypatch):
    """The private config lists Lever before the catch-all, both under their real names."""
    monkeypatch.setitem(submission.ADAPTERS, "lever_v1", LocalLeverV1)
    monkeypatch.setitem(submission.ADAPTERS, "generic_v1", LocalGenericV1)
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["lever_v1", "generic_v1"],
            "human_pacing": False,
        },
    )


def generic_only(monkeypatch):
    """The private config lists only the catch-all adapter, under its real name."""
    monkeypatch.setitem(submission.ADAPTERS, "generic_v1", LocalGenericV1)
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["generic_v1"],
            "human_pacing": False,
        },
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
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
    propose(
        "education",
        {"schools": [{"school": "Example University", "graduation_month": "2027-12"}]},
        digest(draft()),
    )
    propose(
        "eligibility",
        {"us_work_authorized": True, "sponsorship_now": False, "sponsorship_future": False},
        digest(draft()),
    )
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
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["synthetic_v1"],
            "human_pacing": False,
        },
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


def prepared(runtime, base, state, job, path=None):
    opened = runtime.open(path or f"{base}/acme/jobs/{job}")
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
    runtime.page.locator('[data-rove-submit="0"]').click()
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
    assert result["confirmation_url"].endswith("/acme/jobs/7/confirmation")
    assert run_id not in runtime.pages  # the finished application's tab is closed
    assert workflow.get(run_id)["status"] == "APPLIED"
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert (
        receipt["confirmation_url"].endswith("/confirmation") and receipt["erga"]["synced"] is False
    )
    assert (state / "applications" / run_id / "form-before-submit.png").exists()
    with pytest.raises((PermissionError, ValueError)):
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


def test_unassociated_labels_radio_groups_and_button_choices_resolve_from_approved_facts(board):
    runtime, base, state = board
    opened = runtime.open(f"{base}/acme/jobs/9")
    kinds = {f["label"]: f["kind"] for f in opened["fields"]}
    assert kinds["Location"] == "text"
    assert kinds["Are you legally authorized to work in the United States?"] == "choice"
    assert kinds["What is your expected graduation year?"] == "radio_group"
    run_id = opened["run_id"]
    directory = state / "applications" / run_id
    (directory / "resume.pdf").write_bytes(b"%PDF-1.4 frozen synthetic resume")
    sha = hashlib.sha256((directory / "resume.pdf").read_bytes()).hexdigest()
    (directory / "resume-manifest.json").write_text(
        json.dumps({"ready": True, "resume_sha256": sha})
    )
    result = runtime.prepare(run_id)
    filled = {f["label"]: f.get("value") for f in result["filled"]}
    assert filled["Are you legally authorized to work in the United States?"] == "Yes"
    assert (
        filled[
            "Will you now or in the future, require sponsorship for employment visa status (e.g., H1B visa status)?"
        ]
        == "No"
    )
    assert filled["What is your expected graduation year?"] == "December 2027"
    assert filled["Name"] == "Alex Example" and "Resume" in filled
    # A plain willingness question takes the owner's standing rule, and says so.
    office = "Are you comfortable working out of our NYC Office 5 days/week?"
    sources = {f["label"]: f.get("source") for f in result["filled"]}
    assert filled[office] == "Yes" and sources[office] == "policy.default.onsite_willing"
    assert runtime.page.locator("input[name=q3]").is_checked()
    pending = {q["label"]: q for q in result["pending"]}
    assert "Location" in pending and "Describe a project you built" in pending
    assert all(label not in pending for label in ("December 2026", "Spring 2027", "Other"))
    assert (
        runtime.page.locator("#g1-2").is_checked()
        and not runtime.page.locator("#g1-0").is_checked()
    )
    assert (
        runtime.page.locator("input[name=q1]").is_checked()
        and not runtime.page.locator("input[name=q2]").is_checked()
    )
    assert result["status"] == "NEEDS_USER"
    # The owner's own answer for the button question overrides the default on the next
    # preparation, and is what a later form asking the same thing gets. A question about
    # another office is another question.
    nyc = next(f for f in opened["fields"] if f["label"] == office)
    worker.apply_command(
        {"kind": "answer", "application_id": run_id, "field_key": nyc["key"], "value": "No"},
        "msg-nyc",
    )
    again = runtime.prepare(run_id)
    assert {f["label"]: f.get("value") for f in again["filled"]}[office] == "No"
    assert not runtime.page.locator("input[name=q3]").is_checked()
    assert workflow.recall_answer(office + " *", ["Yes", "No"]) == "No"
    assert workflow.recall_answer("Are you able to work on-site in Austin?", ["Yes", "No"]) is None


def test_account_creation_and_sign_in_use_the_encrypted_store_and_never_leak(board):
    from rove import credentials

    runtime, base, state = board
    opened = runtime.open(f"{base}/acme/jobs/11")
    assert opened["auth_page"] == "register" and opened["manual_takeover_required"]
    assert all("value" not in f for f in opened["fields"])
    run_id = opened["run_id"]
    after = runtime.register(run_id)
    assert "Verify your email" in after["text"]
    account = credentials.lookup("127.0.0.1")
    assert account["username"] == "alex@example.invalid" and len(account["password"]) == 20
    assert (state / "credentials/key").stat().st_mode & 0o777 == 0o600
    with workflow.db() as conn:
        rows = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM application_events WHERE kind='account_created'"
            )
        ]
    assert (
        rows
        and account["password"] not in json.dumps(rows)
        and "accepted: I agree to the terms" in rows[0]["filled"]
    )
    signin = runtime.open(f"{base}/acme/jobs/12")
    assert signin["auth_page"] == "login"
    landed = runtime.login(signin["run_id"])
    assert landed.get("auth_page") is None and any(
        f["label"] == "First name" for f in landed["fields"]
    )
    assert runtime.page.evaluate("document.body.innerText").find(account["password"]) == -1


def test_multi_page_forms_are_filled_step_by_step_until_the_final_control(board):
    runtime, base, _state = board
    opened = runtime.open(f"{base}/acme/jobs/13")
    assert [c["label"] for c in opened["nav_controls"]] == ["Continue"]
    run_id = opened["run_id"]
    result = runtime.prepare(run_id)
    assert {f["label"] for f in result["filled"]} == {"First name", "Email", "Last name"}
    assert result["pending"] == [] and result["final_controls"][0]["label"] == "Submit Application"
    assert len(result["pages"]) == 2 and result["status"] == "READY_FOR_REVIEW"
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM application_events WHERE application_id=? AND kind='form_step'",
                (run_id,),
            ).fetchone()[0]
            == 1
        )


def test_late_rendered_forms_wait_out_the_spinner_and_decline_cookies(board):
    runtime, base, _state = board
    opened = runtime.open(f"{base}/acme/jobs/14")
    assert {f["label"] for f in opened["fields"]} == {"First name", "Email", "Resume"}
    assert runtime.page.evaluate("document.documentElement.dataset.consent") == "rejected"


def test_generic_adapter_confirms_a_plain_thank_you_page(board, monkeypatch):
    runtime, base, state = board
    generic_only(monkeypatch)
    run_id, package_hash = prepared(runtime, base, state, 15)
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert result["status"] == "APPLIED" and result["adapter"] == "generic_v1", result
    checks = result["checks"]
    assert checks["confirmed"] and checks["confirmation_text"] and checks["no_form_error"]
    assert checks["url_changed"] and checks["form_gone"] and checks["post_accepted"]
    # "/done" is not a confirmation-looking URL; the thank-you sentence carried it.
    assert not checks["confirmation_url"] and not checks["confirmation_region"]
    assert result["confirmation_url"].endswith("/acme/jobs/15/done")
    assert Board.posts == ["/acme/jobs/15"]
    assert run_id not in runtime.pages and workflow.get(run_id)["status"] == "APPLIED"
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert receipt["adapter"] == "generic_v1"
    assert "Thank you for applying" in receipt["confirmation_text"]
    assert receipt["responses"] == [{"host": "127.0.0.1", "path": "/acme/jobs/15", "status": 200}]
    with pytest.raises((PermissionError, ValueError)):
        submission.submit(runtime, run_id, package_hash, "msg-1")
    assert Board.posts == ["/acme/jobs/15"]


def test_generic_adapter_treats_an_inline_form_error_as_a_rejected_form(board, monkeypatch):
    runtime, base, state = board
    generic_only(monkeypatch)
    Board.reject.add("/acme/jobs/16")
    run_id, package_hash = prepared(runtime, base, state, 16)
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert result["status"] == "NOT_SUBMITTED", result
    checks = result["checks"]
    assert not checks["confirmed"] and not checks["no_form_error"]
    assert not checks["url_changed"] and not checks["form_gone"] and checks["post_rejected"]
    assert "validation error" in result["reason"]
    assert "Email address is invalid" in result["reason"]
    # The tab stays open on the form for the owner to look at; nothing was closed.
    assert run_id in runtime.pages and runtime.page.url.endswith("/acme/jobs/16")
    assert workflow.get(run_id)["status"] == "NEEDS_USER"
    with workflow.db() as conn:
        states = [
            json.loads(r[0])["to"]
            for r in conn.execute(
                "SELECT data FROM application_events WHERE application_id=? AND kind='lifecycle'",
                (run_id,),
            )
        ]
    assert "APPLIED" not in states and "UNKNOWN_SUBMISSION" not in states
    assert "NEEDS_USER" in states
    assert not (state / "applications" / run_id / "receipt.json").read_text().count('"APPLIED"')
    with pytest.raises(PermissionError):
        submission.claim_attempt(run_id, package_hash, "msg-1")
    assert Board.posts == ["/acme/jobs/16"]


def test_lever_adapter_confirms_the_thanks_page_and_records_the_post(board, monkeypatch):
    runtime, base, state = board
    lever_first(monkeypatch)
    apply_path = f"/acme/{LEVER_POSTING}/apply"
    run_id, package_hash = prepared(runtime, base, state, None, path=base + apply_path)
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    # Lever is listed before the catch-all, so the posting gets Lever's contract.
    assert result["status"] == "APPLIED" and result["adapter"] == "lever_v1", result
    checks = result["checks"]
    assert checks["confirmed"] and checks["confirmation_url"] and checks["confirmation_content"]
    assert checks["form_gone"] and not checks["captcha_rejected"]
    assert checks["post_status"] == 303 and checks["post_accepted"] and not checks["post_rejected"]
    assert result["confirmation_url"].endswith(f"/acme/{LEVER_POSTING}/thanks")
    assert "Application submitted!" in result["confirmation_text"]
    assert Board.posts == [apply_path]
    assert run_id not in runtime.pages and workflow.get(run_id)["status"] == "APPLIED"
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert receipt["adapter"] == "lever_v1"
    assert receipt["responses"] == [{"host": "127.0.0.1", "path": apply_path, "status": 303}]
    with pytest.raises((PermissionError, ValueError)):
        submission.submit(runtime, run_id, package_hash, "msg-1")
    assert Board.posts == [apply_path]


def test_lever_captcha_rejection_is_not_submitted_and_hands_the_open_tab_to_the_owner(
    board, monkeypatch
):
    runtime, base, state = board
    lever_first(monkeypatch)
    apply_path = f"/acme/{LEVER_POSTING_REJECTED}/apply"
    Board.reject.add(apply_path)
    run_id, package_hash = prepared(runtime, base, state, None, path=base + apply_path)
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    result = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert result["status"] == "NOT_SUBMITTED" and result["adapter"] == "lever_v1", result
    checks = result["checks"]
    assert checks["captcha_rejected"] and not checks["confirmed"]
    assert not checks["url_changed"] and not checks["form_gone"] and checks["no_form_error"]
    assert checks["post_status"] == 200  # the page, not the status, says it was not sent
    assert result["reason"] == (
        "Lever's CAPTCHA rejected the send; open the recruiting browser, solve it and "
        "press Submit yourself, then reply applied"
    )
    # The tab stays open on the emptied form for the owner to finish by hand.
    assert run_id in runtime.pages and runtime.page.url.endswith(apply_path)
    assert workflow.get(run_id)["status"] == "MANUAL_TAKEOVER"
    with workflow.db() as conn:
        assert conn.execute("SELECT status FROM live_submission_attempts").fetchone()[0] == (
            "NOT_SUBMITTED"
        )
        holds = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM application_events WHERE application_id=? AND kind='needs_action'",
                (run_id,),
            )
        ]
    assert holds[-1]["commands"] == ["applied", "park it"]
    assert "CAPTCHA" in holds[-1]["headline"] and "nothing" not in holds[-1]["reason"]
    with pytest.raises(PermissionError):
        submission.claim_attempt(run_id, package_hash, "msg-1")
    assert Board.posts == [apply_path]
    # The owner solves it, presses Submit and replies applied: recorded, never re-sent.
    worker.apply_command(
        {"kind": "reconcile", "application_id": run_id, "outcome": "applied"}, "msg-2"
    )
    assert workflow.get(run_id)["status"] == "APPLIED"
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert receipt["status"] == "APPLIED" and "manually" in receipt["reason"]
    assert Board.posts == [apply_path]
