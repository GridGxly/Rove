"""Pop-ups in front of the form: closed by code, asked of Qwen only when unclear, never
guessed on. The pages are synthetic fixtures served from a local HTTP server, the way the
submission tests serve theirs; nothing leaves the machine."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import test_workflow
from test_unattended_failure_modes import enabled_worker, scripted_browser, the_card

from rove import live_browser, overlays, reasoning, submission, worker, workflow
from rove.live_browser import RecruitingBrowser
from rove.onboarding import approve, digest, draft, propose

FIXTURES = Path(__file__).parent / "fixtures" / "overlays"
# The approved synthetic profile and private state root, as test_workflow builds them.
profile_state = test_workflow.state
CONFIRMATION = b"""<!doctype html><title>Harborview Logistics</title>
<p>Thank you for applying to Harborview Logistics. We received your application.</p>"""


class Site(BaseHTTPRequestHandler):
    posts: ClassVar[list[str]] = []

    def do_GET(self):
        name = self.path.rstrip("/").rsplit("/", 1)[-1]
        if name == "done":
            body = CONFIRMATION
        else:
            body = (FIXTURES / f"{name}.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Site.posts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args):
        pass


class LocalGenericV1(submission.GenericV1):
    schemes = ("http", "https")  # the synthetic site is plain http on loopback


@pytest.fixture
def site(tmp_path, monkeypatch):
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
        "eligibility",
        {"us_work_authorized": True, "sponsorship_now": False, "sponsorship_future": False},
        digest(draft()),
    )
    approve(digest(draft()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Site.posts.clear()
    identity = lambda value: value
    monkeypatch.setattr(live_browser, "validate_destination", identity)
    monkeypatch.setattr(live_browser, "public_link", identity)
    monkeypatch.setattr(live_browser, "lookup_job_link", lambda url: {"in_feed": False})
    monkeypatch.setattr(live_browser, "approved_ats", lambda url: True)
    monkeypatch.setattr(live_browser, "job_scope", lambda url: ("synthetic",))
    monkeypatch.setattr(submission, "ineligible", lambda url: "")  # loopback is a board here
    monkeypatch.setattr(workflow, "public_link", identity)
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
    monkeypatch.setattr(submission, "CONFIRMATION_TIMEOUT_MS", 3000)
    runtime = RecruitingBrowser(headless=True)
    try:
        yield runtime, f"http://127.0.0.1:{server.server_port}/acme/jobs", tmp_path / "state"
    finally:
        if runtime.context:
            runtime.context.close()
        if runtime.playwright:
            runtime.playwright.stop()
        server.shutdown()
        server.server_close()


def lines(run_id: str, kind: str = "overlay") -> list[dict]:
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? AND kind=? ORDER BY id",
            (run_id, kind),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def prepared(runtime, state, opened: dict) -> dict:
    run_id = opened["run_id"]
    directory = state / "applications" / run_id
    resume = directory / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 frozen synthetic resume")
    (directory / "resume-manifest.json").write_text(
        json.dumps(
            {"ready": True, "resume_sha256": hashlib.sha256(resume.read_bytes()).hexdigest()}
        )
    )
    return runtime.prepare(run_id)


def canned(answer: dict):
    """A harness result the way `reasoning.generate` returns one."""

    def generate(directory, context, basename, attempts=2):
        assert context["review_type"] == "overlay"
        assert "sign me up" in " ".join(context["buttons"]).lower()
        assert "@" not in context["overlay_text"] and len(context["overlay_text"]) <= 600
        return {
            "model": "m",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": json.dumps(answer),
            },
        }

    return generate


# --- what code decides on its own ----------------------------------------------------


def test_the_never_list_and_the_continue_list_decide_what_may_be_pressed():
    assert overlays.allowed("No thanks") and overlays.allowed("Apply manually")
    assert overlays.allowed("Continue without LinkedIn") and overlays.allowed("Close")
    for label in ("Subscribe", "Apply with LinkedIn", "Sign up", "Accept", "Allow", "Yes", "No"):
        assert not overlays.allowed(label), label
    chooser = {
        "text": "How would you like to apply?",
        "buttons": [
            {"label": "Apply with LinkedIn", "ref": "0"},
            {"label": "Apply manually", "ref": "1"},
        ],
    }
    assert overlays.choose(chooser)["button"]["label"] == "Apply manually"
    autofill = {
        "text": "Autofill from resume",
        "buttons": [{"label": "Upload resume", "ref": "0"}, {"label": "No thanks", "ref": "1"}],
    }
    assert overlays.choose(autofill)["button"]["label"] == "No thanks"
    informational = {
        "text": "Our privacy policy changed",
        "fields": 0,
        "buttons": [{"label": "OK", "ref": "0"}],
    }
    assert overlays.choose(informational)["button"]["label"] == "OK"
    # A bare OK beside a field could be the field's answer: not pressed by code.
    assert overlays.choose({**informational, "fields": 1}) is None
    unknown = {"text": "Stay in touch", "buttons": [{"label": "Keep browsing", "ref": "0"}]}
    assert overlays.choose(unknown) is None
    # Qwen's pick must be one of the buttons and pass the same never-list.
    assert (
        overlays.qwen_choice({"action": "click", "button": "Keep browsing"}, unknown)["ref"] == "0"
    )
    assert overlays.qwen_choice({"action": "click", "button": "Yes, sign me up"}, unknown) is None
    assert overlays.qwen_choice({"action": "leave", "button": ""}, unknown) is None
    assert overlays.qwen_choice({"action": "click", "button": "Something else"}, unknown) is None


def test_the_application_itself_is_never_a_pop_up():
    assert overlays.is_form({"holds_form": True, "buttons": []})
    assert overlays.is_form({"file": True, "buttons": []})
    assert overlays.is_form({"fields": 1, "buttons": [{"label": "Submit application", "ref": "0"}]})
    assert overlays.is_form({"fields": 3, "buttons": [{"label": "Next", "ref": "0"}]})
    assert not overlays.is_form({"fields": 1, "buttons": [{"label": "Skip", "ref": "0"}]})


# --- pages ---------------------------------------------------------------------------


def test_a_newsletter_modal_is_closed_with_its_dismissive_control(site):
    runtime, base, _state = site
    opened = runtime.open(f"{base}/newsletter")
    page = runtime.page
    assert page.locator("#newsletter").count() == 0 and page.locator("#backdrop").count() == 0
    assert page.evaluate("() => document.body.dataset.subscribed") is None
    assert [f["label"] for f in opened["fields"]] == ["First name", "Email"]
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “Join our newsletter”"
    assert "via button “No thanks”" in closed["detail"] and "qwen" not in closed["detail"]
    assert runtime.run["popups_closed"] == 1


def test_a_talent_community_dialog_is_skipped_and_its_field_never_typed_into(site):
    runtime, base, _state = site
    opened = runtime.open(f"{base}/talent")
    page = runtime.page
    assert page.locator("#talent-popup").count() == 0
    assert page.evaluate("() => document.body.dataset.joined") is None
    assert page.evaluate("() => document.body.dataset.typed") is None
    assert [f["label"] for f in opened["fields"]] == ["First name", "Email"]
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “Join our talent community”"
    assert "via button “Skip”" in closed["detail"]


def test_an_apply_chooser_takes_the_manual_way_in(site):
    runtime, base, _state = site
    opened = runtime.open(f"{base}/chooser")
    page = runtime.page
    assert page.locator("#chooser").count() == 0
    assert page.evaluate("() => document.body.dataset.linkedin") is None
    assert [f["label"] for f in opened["fields"]] == ["First name", "Email"]
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “How would you like to apply?”"
    assert "via button “Apply manually”" in closed["detail"]


def test_a_full_screen_interstitial_is_passed_with_continue_to_site(site):
    runtime, base, _state = site
    opened = runtime.open(f"{base}/interstitial")
    page = runtime.page
    assert page.locator("#promo-screen").count() == 0
    assert page.evaluate("() => document.body.dataset.download") is None
    assert [f["label"] for f in opened["fields"]] == ["First name", "Email"]
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “Get the Harborview app”"
    assert "via button “Continue to site”" in closed["detail"]


def test_a_modal_that_is_the_form_is_kept_filled_and_sent(site):
    runtime, base, state = site
    opened = runtime.open(f"{base}/form_modal")
    page = runtime.page
    assert (
        page.locator("#apply-modal").count() == 1 and page.locator("#apply-backdrop").count() == 1
    )
    assert lines(opened["run_id"]) == []
    result = prepared(runtime, state, opened)
    assert result["status"] == "READY_FOR_REVIEW" and not result["pending"]
    assert {f["label"] for f in result["filled"]} == {"First name", "Email", "Resume"}
    assert page.locator("#apply-modal").count() == 1
    run_id, package_hash = opened["run_id"], result["package_hash"]
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    sent = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert sent["status"] == "APPLIED", sent
    assert Site.posts == ["/acme/jobs/form_modal"]
    assert lines(run_id) == []


def test_a_pop_up_that_appears_mid_fill_is_closed_and_the_fill_finishes(site):
    runtime, base, state = site
    opened = runtime.open(f"{base}/late")
    assert lines(opened["run_id"]) == []
    result = prepared(runtime, state, opened)
    page = runtime.page
    filled = {f["label"]: f.get("value") for f in result["filled"]}
    assert filled["First name"] == "Alex" and filled["Email"] == "alex@example.invalid"
    assert filled["Are you legally authorized to work in the United States?"] == "Yes"
    assert page.locator("input[name=authorized][value=yes]").is_checked()
    assert [q["label"] for q in result["pending"]] == ["City"]
    assert page.locator("#save-modal").count() == 0
    assert page.evaluate("() => document.body.dataset.created") is None
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “Save your progress”"
    assert "via button “Not now”" in closed["detail"]


def test_a_chat_bubble_that_covers_nothing_is_left_alone(site):
    runtime, base, state = site
    opened = runtime.open(f"{base}/chat")
    result = prepared(runtime, state, opened)
    assert {f["label"] for f in result["filled"]} == {"First name", "Email"}
    assert runtime.page.locator("#chat-widget").count() == 1
    assert lines(opened["run_id"]) == []


def test_page_dialogs_on_submit_are_accepted_and_others_dismissed(site):
    runtime, base, state = site
    opened = runtime.open(f"{base}/dialogs")
    run_id = opened["run_id"]
    # Outside a send, the page's confirm is turned down and noted.
    assert runtime.page.evaluate("() => confirm('Leave this page?')") is False
    (turned_down,) = lines(run_id)
    assert turned_down["line"] == "Dismissed the page's confirm dialog: “Leave this page?”"
    result = prepared(runtime, state, opened)
    assert result["status"] == "READY_FOR_REVIEW"
    package_hash = result["package_hash"]
    worker.apply_command(
        {"kind": "submit", "application_id": run_id, "package_hash": package_hash}, "msg-1"
    )
    sent = submission.submit(runtime, run_id, package_hash, "msg-1")
    assert sent["status"] == "APPLIED", sent
    assert Site.posts == ["/acme/jobs/dialogs"]
    noted = [entry["line"] for entry in lines(run_id)]
    assert noted[1:] == [
        "Accepted the page's alert dialog: “Please double-check your details before sending.”",
        "Accepted the page's confirm dialog: “Send your application now?”",
    ]


def test_a_tab_the_page_opens_on_its_own_is_closed(site):
    runtime, base, _state = site
    runtime.ensure()
    before = set(runtime.context.pages)  # the context's own first tab, which has no opener
    opened = runtime.open(f"{base}/stray_tab")
    assert {p for p in runtime.context.pages if not p.is_closed()} - before == {runtime.page}
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a tab the page opened on its own"
    # A tab the owner opened by hand has no opener and stays.
    own = runtime.context.new_page()
    runtime.clear_overlays()
    assert not own.is_closed()


def test_unknown_wording_is_put_to_qwen_whose_pick_is_checked(site, monkeypatch):
    runtime, base, state = site
    monkeypatch.setattr(
        reasoning, "generate", canned({"action": "click", "button": "Keep browsing"})
    )
    opened = runtime.open(f"{base}/unknown")
    page = runtime.page
    assert page.locator("#keep-in-touch").count() == 0
    assert page.evaluate("() => document.body.dataset.signed") is None
    (closed,) = lines(opened["run_id"])
    assert closed["line"] == "Closed a pop-up: “We'd love to keep in touch”"
    assert "via button “Keep browsing”" in closed["detail"] and "chosen by qwen" in closed["detail"]
    assert (state / "applications" / opened["run_id"] / "popup-1.png").is_file()


def test_a_pick_from_the_never_list_is_refused_and_the_run_holds(site, monkeypatch):
    runtime, base, state = site
    monkeypatch.setattr(
        reasoning, "generate", canned({"action": "click", "button": "Yes, sign me up"})
    )
    with pytest.raises(overlays.OverlayInTheWay, match="did not want to guess"):
        runtime.open(f"{base}/unknown")
    page = runtime.page
    assert page.locator("#keep-in-touch").count() == 1
    assert page.evaluate("() => document.body.dataset.signed") is None
    run_id = runtime.run["id"]
    (held,) = lines(run_id)
    assert held["line"] == "A pop-up is in the way: “We'd love to keep in touch”"
    assert (state / "applications" / run_id / "failure.png").is_file()
    assert (state / "applications" / run_id / "popup-1.png").is_file()


def test_a_pop_up_hold_reaches_the_owner_in_plain_words(profile_state, monkeypatch):
    """The daemon's stop crosses the socket as its words; the worker's card repeats them."""
    calls = scripted_browser(monkeypatch, open=RuntimeError(overlays.HOLD_WORDS))
    discord_calls, _posted = enabled_worker(monkeypatch)
    app = workflow.enqueue("https://jobs.example.com/popup")["application_id"]
    result = worker.tick()
    assert result["status"] == "NEEDS_USER" and calls == ["open"]
    assert workflow.get(app)["status"] == "NEEDS_USER"
    card = the_card(discord_calls)
    assert card["description"].endswith(overlays.HOLD_WORDS)
    assert "RuntimeError" not in card["description"]


def test_qwen_away_means_a_hold_with_plain_words_not_a_guess(site, monkeypatch):
    runtime, base, state = site

    def away(*_args, **_kwargs):
        raise reasoning.ModelUnavailable("Local model server is not running")

    monkeypatch.setattr(reasoning, "generate", away)
    with pytest.raises(overlays.OverlayInTheWay) as stopped:
        runtime.open(f"{base}/unknown")
    assert str(stopped.value) == overlays.HOLD_WORDS
    assert "`go`" in overlays.HOLD_WORDS
    run_id = runtime.run["id"]
    noted = [entry["line"] for entry in lines(run_id)]
    assert noted == [
        "Asked Qwen about a pop-up and got no usable answer",
        "A pop-up is in the way: “We'd love to keep in touch”",
    ]
    assert (state / "applications" / run_id / "failure.png").is_file()
    assert runtime.page.evaluate("() => document.body.dataset.signed") is None
