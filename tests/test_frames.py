"""Real headless browser: forms inside frames, shadow roots and rich-text editors.

An employer page embeds a synthetic board form in an iframe served from a second loopback
origin; another page builds its form from web components with nested shadow roots; a third
asks a long question in a rich-text editor. Reading, filling, the submit guard, sending
and the confirmation all work where the form actually is. Every page is synthetic and
served offline from local HTTP servers.
"""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from rove import form_frames, live_browser, submission, worker, workflow
from rove.live_browser import RecruitingBrowser
from rove.onboarding import approve, digest, draft, propose

THANKS = """<!doctype html><title>Thanks</title>
<p>Thank you for applying to Northwind. We received your application.</p>"""

# The board's embedded form: it posts to its own address, then shows a thank-you page.
BOARD_FORM = """<!doctype html><title>Job Application for Software Engineering Intern</title>
<h1>Apply for this job</h1>
<form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="l">Last name</label><input id="l" name="last" required></div>
<div><label for="e">Email</label><input id="e" name="email" type="email" required></div>
<div><label for="r">Resume</label><input id="r" name="resume" type="file"></div>
<button type="submit">Submit application</button></form><p id="error" role="alert"></p>
<script>document.querySelector('form').addEventListener('submit', async e => {
  e.preventDefault();
  const r = await fetch('/embed/job_app', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  if (r.ok) location.assign('/embed/confirmation?for=northwind&token=4471');
  else document.querySelector('#error').textContent = 'Something went wrong';
});</script>"""

# The employer's own page: a job search box in its header, the posting, the board's frame.
EMPLOYER = """<!doctype html><title>Software Engineering Intern - Northwind Careers</title>
<header><form role="search"><label for="q">Search jobs</label><input id="q" name="q" type="search"></form></header>
<h1>Software Engineering Intern</h1>
<p>Northwind builds routing software for regional freight carriers. Interns ship code.</p>
__FRAME__"""
FRAME = (
    '<div id="grnhse_app"><iframe id="grnhse_iframe" title="Greenhouse Job Board" '
    'src="__BOARD__/embed/job_app?for=northwind&amp;token=4471" '
    'style="width:100%;height:900px;border:0"></iframe></div>'
)
# The same frame inside the employer's own apply dialog, which has a close control.
DIALOG = (
    '<div role="dialog" aria-modal="true" aria-label="Apply" id="apply-dialog" '
    'style="position:fixed;inset:5%;background:#fff">'
    '<button type="button" class="close" aria-label="Close" '
    "onclick=\"document.getElementById('apply-dialog').remove()\">×</button>" + FRAME + "</div>"
)
# An embed that shows the posting first, with its own Apply link to the form inside it.
EMBED_POSTING = """<!doctype html><title>Northwind - Platform Intern</title>
<h1>Platform Intern</h1><p>Help run the routing platform.</p>
<a href="/embed/apply/application">Apply for this job</a>"""
# A chat window in a small frame beside a form the page holds itself.
CHAT = """<!doctype html><title>Chat</title><div><label for="m">Type your message</label>
<textarea id="m"></textarea><button type="button">Send</button></div>"""
OWN_FORM_WITH_CHAT = """<!doctype html><title>Apply - Northwind</title>
<form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="e">Email</label><input id="e" name="email" required></div>
<button type="submit">Submit application</button></form>
<iframe src="__BOARD__/chat" style="position:fixed;right:8px;bottom:8px;width:320px;height:360px;border:0"></iframe>
<div role="dialog" aria-label="Support chat" style="position:fixed;left:8px;bottom:8px;width:240px">
<label for="ask">Ask us anything</label><textarea id="ask"></textarea><button type="button">Send</button></div>"""

# A form made of web components: each field's label and input sit in the field's own
# shadow root (with the same id in every one), inside the application's shadow root.
SHADOW = """<!doctype html><title>Acme Robotics - Apply</title>
<h1>Robotics Intern</h1>
<acme-application></acme-application>
<script>
customElements.define('acme-field', class extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<label for="input">' + this.getAttribute('text') + '</label>'
      + '<input id="input" name="' + this.getAttribute('name') + '" type="'
      + (this.getAttribute('type') || 'text') + '"' + (this.hasAttribute('required') ? ' required' : '') + '>';
  }
});
customElements.define('acme-application', class extends HTMLElement {
  connectedCallback() {
    const root = this.attachShadow({mode: 'open'});
    root.innerHTML = '<form id="application-form"><h2>About you</h2>'
      + '<acme-field text="First name" name="first" required></acme-field>'
      + '<acme-field text="Email" name="email" type="email" required></acme-field>'
      + '<label for="r">Resume</label><input id="r" name="resume" type="file">'
      + '<fieldset><legend>Are you legally authorized to work in the United States?</legend>'
      + '<label><input type="radio" name="auth" value="yes"> Yes</label>'
      + '<label><input type="radio" name="auth" value="no"> No</label></fieldset>'
      + '<button type="submit">Submit application</button></form>';
    root.querySelector('form').addEventListener('submit', async e => {
      e.preventDefault();
      const r = await fetch(location.pathname, {method: 'POST', body: '{}'});
      if (r.ok) location.assign(location.pathname + '/done');
    });
  }
});
</script>"""

# A long question asked in a rich-text editor (Quill's markup), named by aria-labelledby.
RICH = """<!doctype html><title>Apply - Northwind</title><form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div class="field"><div id="why">Why do you want to work at Northwind?</div>
<div class="ql-container"><div class="ql-editor" contenteditable="true" role="textbox"
 aria-multiline="true" aria-labelledby="why"><p><br></p></div></div></div>
<button type="submit">Submit application</button></form>"""


class Pages(BaseHTTPRequestHandler):
    pages: ClassVar[dict[str, str]] = {}
    posts: ClassVar[list[str]] = []

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith("/slow"):
            time.sleep(1.5)  # an embed that arrives after the employer's page
            path = "/embed/job_app"
        body = THANKS if path.endswith(("/done", "/confirmation")) else self.pages.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).posts.append(urlsplit(self.path).path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args):
        pass


class Employer(Pages):
    pages: ClassVar[dict[str, str]] = {}
    posts: ClassVar[list[str]] = []


class Board(Pages):
    pages: ClassVar[dict[str, str]] = {}
    posts: ClassVar[list[str]] = []


class LocalGenericV1(submission.GenericV1):
    schemes = ("http", "https")  # the synthetic sites are plain http on loopback


def approved_profile(tmp_path):
    pdf = tmp_path / "approved.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic approved resume")
    propose(
        "identity",
        {
            "legal_first_name": "Alex",
            "legal_last_name": "Example",
            "email": "alex@example.invalid",
            "phone": "555 010 0199",
        },
        digest(draft()),
    )
    propose("evidence", {"resume_path": str(pdf)}, digest(draft()))
    propose(
        "education",
        {"schools": [{"school": "Example University", "graduation_month": "2027-12"}]},
        digest(draft()),
    )
    propose("availability", {"earliest_start": "2027-06-01"}, digest(draft()))
    propose(
        "eligibility",
        {"us_work_authorized": True, "sponsorship_now": False, "sponsorship_future": False},
        digest(draft()),
    )
    approve(digest(draft()))


@pytest.fixture
def sites(tmp_path, monkeypatch):
    """An employer site on 127.0.0.1 and a board on localhost: two origins, two servers."""
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    approved_profile(tmp_path)
    servers = []
    for handler in (Employer, Board):
        handler.pages.clear()
        handler.posts.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    employer = f"http://127.0.0.1:{servers[0].server_port}"
    board = f"http://localhost:{servers[1].server_port}"
    identity = lambda value: value
    monkeypatch.setattr(live_browser, "validate_destination", identity)
    monkeypatch.setattr(live_browser, "public_link", identity)
    monkeypatch.setattr(live_browser, "lookup_job_link", lambda url: {"in_feed": False})
    monkeypatch.setattr(live_browser, "approved_ats", lambda url: True)
    monkeypatch.setattr(live_browser, "job_scope", lambda url: ("synthetic",))
    monkeypatch.setattr(workflow, "public_link", identity)
    # The two loopback origins count as sites a form may be filled on.
    monkeypatch.setattr(submission, "ineligible", lambda url: "")
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
    monkeypatch.setitem(submission.ADAPTERS, "generic_v1", LocalGenericV1)
    monkeypatch.setattr(submission, "erga_confirm", lambda app: {"synced": False, "warning": "x"})
    monkeypatch.setattr(submission, "CONFIRMATION_TIMEOUT_MS", 4000)
    runtime = RecruitingBrowser(headless=True)
    Board.pages["/embed/job_app"] = BOARD_FORM
    Board.pages["/chat"] = CHAT
    Board.pages["/embed/apply"] = EMBED_POSTING
    Board.pages["/embed/apply/application"] = BOARD_FORM
    Employer.pages["/careers/jobs/4471"] = EMPLOYER.replace("__FRAME__", FRAME).replace(
        "__BOARD__", board
    )
    Employer.pages["/careers/jobs/4472"] = EMPLOYER.replace("__FRAME__", DIALOG).replace(
        "__BOARD__", board
    )
    Employer.pages["/careers/jobs/4473"] = OWN_FORM_WITH_CHAT.replace("__BOARD__", board)
    Employer.pages["/careers/jobs/4475"] = EMPLOYER.replace(
        "__FRAME__", FRAME.replace("/embed/job_app?for=northwind&amp;token=4471", "/embed/slow")
    ).replace("__BOARD__", board)
    Employer.pages["/careers/jobs/4474"] = EMPLOYER.replace(
        "__FRAME__", FRAME.replace("/embed/job_app?for=northwind&amp;token=4471", "/embed/apply")
    ).replace("__BOARD__", board)
    Employer.pages["/acme/jobs/7"] = SHADOW
    Employer.pages["/acme/jobs/8"] = RICH
    try:
        yield runtime, employer, board, tmp_path / "state"
    finally:
        if runtime.context:
            runtime.context.close()
        if runtime.playwright:
            runtime.playwright.stop()
        for server in servers:
            server.shutdown()
            server.server_close()


def freeze_resume(state, run_id: str):
    directory = state / "applications" / run_id
    resume = directory / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 frozen synthetic resume")
    (directory / "resume-manifest.json").write_text(
        json.dumps(
            {"ready": True, "resume_sha256": hashlib.sha256(resume.read_bytes()).hexdigest()}
        )
    )


def approve_send(run_id: str, package_hash: str, message: str = "msg-1"):
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, message
    )


def overlay_lines(run_id: str) -> list[dict]:
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? AND kind='overlay'",
            (run_id,),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


# --- frames ----------------------------------------------------------------------------


def test_an_embedded_board_is_read_filled_and_sent_through_its_frame(sites):
    runtime, employer, board, state = sites
    opened = runtime.open(f"{employer}/careers/jobs/4471")
    # The application's questions come from the board's frame, not the page's search box.
    assert [f["label"] for f in opened["fields"]] == ["First name", "Last name", "Email", "Resume"]
    assert opened["url"].startswith(f"{board}/embed/job_app?for=northwind")
    assert opened["page_url"] == f"{employer}/careers/jobs/4471"
    assert opened["title"] == "Software Engineering Intern - Northwind Careers"
    assert [c["label"] for c in opened["final_controls"]] == ["Submit application"]
    run_id = opened["run_id"]
    freeze_resume(state, run_id)
    result = runtime.prepare(run_id)
    assert result["status"] == "READY_FOR_REVIEW" and not result["pending"], result
    assert {f["label"] for f in result["filled"]} == {"First name", "Last name", "Email", "Resume"}
    assert result["url"] == opened["url"]
    frame = runtime.form
    assert frame is not runtime.page.main_frame
    assert frame.locator("#f").input_value() == "Alex"
    assert frame.locator("#e").input_value() == "alex@example.invalid"
    assert runtime.page.locator("#q").input_value() == ""  # the search box was never typed in
    # The guard holds inside the frame: a click that nobody armed sends nothing.
    frame.locator('[data-rove-submit="0"]').click()
    runtime.page.wait_for_timeout(500)
    assert Board.posts == []
    approve_send(run_id, result["package_hash"])
    sent = submission.submit(runtime, run_id, result["package_hash"], "msg-1")
    assert sent["status"] == "APPLIED", sent
    assert sent["checks"]["confirmation_text"] and sent["checks"]["form_gone"]
    assert sent["confirmation_url"].startswith(f"{board}/embed/confirmation")
    assert Board.posts == ["/embed/job_app"] and Employer.posts == []
    receipt = json.loads((state / "applications" / run_id / "receipt.json").read_text())
    assert receipt["responses"] == [{"host": "localhost", "path": "/embed/job_app", "status": 200}]
    assert workflow.get(run_id)["status"] == "APPLIED"


def test_an_apply_dialog_that_holds_the_frame_is_the_application_and_stays_open(sites):
    runtime, employer, _board, state = sites
    opened = runtime.open(f"{employer}/careers/jobs/4472")
    assert runtime.page.locator("#apply-dialog").count() == 1
    assert overlay_lines(opened["run_id"]) == []
    assert [f["label"] for f in opened["fields"]] == ["First name", "Last name", "Email", "Resume"]
    freeze_resume(state, opened["run_id"])
    result = runtime.prepare(opened["run_id"])
    assert result["status"] == "READY_FOR_REVIEW", result
    assert runtime.page.locator("#apply-dialog").count() == 1


def test_a_chat_frame_and_a_chat_window_are_not_the_application(sites):
    runtime, employer, _board, state = sites
    opened = runtime.open(f"{employer}/careers/jobs/4473")
    assert [f["label"] for f in opened["fields"]] == ["First name", "Email"]
    assert "page_url" not in opened and runtime.form is runtime.page.main_frame
    freeze_resume(state, opened["run_id"])
    result = runtime.prepare(opened["run_id"])
    assert {f["label"] for f in result["filled"]} == {"First name", "Email"}
    assert runtime.page.locator("#ask").input_value() == ""


def test_an_embed_that_loads_after_the_page_is_waited_for_once(sites):
    runtime, employer, board, _state = sites
    opened = runtime.open(f"{employer}/careers/jobs/4475")
    assert opened["url"] == f"{board}/embed/slow"
    assert [f["label"] for f in opened["fields"]] == ["First name", "Last name", "Email", "Resume"]


def test_an_apply_link_inside_the_frame_is_followed_there(sites):
    runtime, employer, board, state = sites
    opened = runtime.open(f"{employer}/careers/jobs/4474")
    # The page's search box is not the way in; the frame's own Apply link is.
    assert opened["fields"] == [] and opened["url"] == f"{board}/embed/apply"
    (link,) = opened["application_links"]
    assert link["label"] == "Apply for this job"
    landed = runtime.follow(opened["run_id"], opened["observation_id"], link["ref"])
    assert landed["url"] == f"{board}/embed/apply/application"
    assert landed["page_url"] == f"{employer}/careers/jobs/4474"
    assert [f["label"] for f in landed["fields"]] == ["First name", "Last name", "Email", "Resume"]
    freeze_resume(state, opened["run_id"])
    result = runtime.prepare(opened["run_id"])
    assert result["status"] == "READY_FOR_REVIEW", result.get("reason")
    assert runtime.form.locator("#l").input_value() == "Example"


def test_the_frame_choice_weighs_questions_not_controls():
    form = {"fields": [{"ref": str(i)} for i in range(4)], "final_controls": [{"ref": "0"}]}
    search = {"fields": [{"ref": "0"}], "application_links": [], "auth_controls": []}
    chat = {"fields": [{"ref": "0"}], "final_controls": []}
    assert form_frames.choose(search, [chat, form]) == 1
    assert form_frames.choose(form, [chat]) is None
    # Options of one radio group are one question.
    radios = {"fields": [{"ref": str(i), "_group": {"key": "0"}} for i in range(5)]}
    assert form_frames.weight(radios) == 1 and not form_frames.holds_form(radios)
    # Nothing to act on in the page: the frame with the Apply link is the way in.
    posting = {"fields": [], "application_links": [], "auth_controls": []}
    described = {"fields": [], "application_links": [{"ref": "0"}]}
    assert form_frames.choose(posting, [described]) == 0
    assert form_frames.choose(search, [described]) == 0
    assert form_frames.choose({**search, "application_links": [{"ref": "0"}]}, [described]) is None
    assert not form_frames.candidate("https://www.google.com/recaptcha/api2/anchor?k=x")
    assert not form_frames.candidate("https://newassets.hcaptcha.com/captcha/v1/x")
    assert not form_frames.candidate("about:blank")
    assert form_frames.candidate("https://boards.greenhouse.io/embed/job_app?for=x&token=1")


# --- shadow roots --------------------------------------------------------------------


def test_a_form_of_nested_web_components_is_read_filled_guarded_and_sent(sites):
    runtime, employer, _board, state = sites
    opened = runtime.open(f"{employer}/acme/jobs/7")
    asked = {f["label"]: f for f in opened["fields"] if not f.get("in_group")}
    assert list(asked) == [
        "First name",
        "Email",
        "Resume",
        "Are you legally authorized to work in the United States?",
    ]
    assert asked["First name"]["required"] and asked["Email"]["kind"] == "email"
    authorized = asked["Are you legally authorized to work in the United States?"]
    assert [o["label"] for o in authorized["options"]] == ["Yes", "No"]
    assert "Robotics Intern" in opened["text"] and "About you" in opened["text"]
    run_id = opened["run_id"]
    freeze_resume(state, run_id)
    result = runtime.prepare(run_id)
    assert result["status"] == "READY_FOR_REVIEW" and not result["pending"], result
    values = {f["label"]: f.get("value") for f in result["filled"]}
    assert values["First name"] == "Alex" and values["Email"] == "alex@example.invalid"
    assert values["Are you legally authorized to work in the United States?"] == "Yes"
    page = runtime.page
    assert page.locator("acme-field input[name=first]").input_value() == "Alex"
    assert page.locator("input[name=auth][value=yes]").is_checked()
    # A submit inside a shadow root is held by the guard until the send arms it.
    page.locator('[data-rove-submit="0"]').click()
    page.wait_for_timeout(500)
    assert Employer.posts == []
    approve_send(run_id, result["package_hash"])
    sent = submission.submit(runtime, run_id, result["package_hash"], "msg-1")
    assert sent["status"] == "APPLIED", sent
    assert Employer.posts == ["/acme/jobs/7"]


# --- rich-text editors -----------------------------------------------------------------


def test_a_rich_text_editor_is_a_long_answer_typed_by_keyboard_and_read_back(sites):
    runtime, employer, _board, _state = sites
    opened = runtime.open(f"{employer}/acme/jobs/8")
    why = next(f for f in opened["fields"] if f["label"] == "Why do you want to work at Northwind?")
    assert why["kind"] == "textarea" and why["rich"] and why["value"] == ""
    run_id = opened["run_id"]
    first = runtime.prepare(run_id)
    assert [q["label"] for q in first["pending"]] == ["Why do you want to work at Northwind?"]
    answer = "Northwind ships routing code that drivers use every day.\nI want to work on it."
    worker.apply_command(
        {"kind": "answer", "application_id": run_id, "field_key": why["key"], "value": answer},
        "msg-why",
    )
    result = runtime.prepare(run_id)
    assert not result["pending"], result
    assert {f["label"]: f["value"] for f in result["filled"]}[why["label"]] == answer
    typed = runtime.page.locator(".ql-editor").inner_text()
    assert " ".join(typed.split()) == " ".join(answer.split())
