"""Steps a board puts before its form: a Next that is a form submit, a picture check only
the owner passes, and a code mailed to his address.

The fixture site is a single-page careers site of the kind a live run met: Apply swaps
the view in place, the first step asks for an email and a consent, its Next is a submit
of that step's form, a picture check comes up, and then six boxes ask for a mailed code.
"""

import json

import pytest
import test_browser_guards
import test_live_submission as live
import test_workflow
from test_browser_guards import Site
from test_frames import freeze_resume
from test_unattended_failure_modes import enabled_worker, posts, the_card

from rove import gates, mail, worker, workflow

board = live.board
site = test_browser_guards.site
state = test_workflow.state

CODE = "482913"
JOB = "/acme/jobs/700"
CAREERS = b"""<!doctype html><title>Careers</title>
<style>.input-row--invisible{position:absolute;left:-9999px}</style>
<div id="app"><h1>Mobile Developer Intern</h1>
<input type="search" aria-label="Search jobs"><button id="apply">Apply Now</button></div>
<script>
const app = document.getElementById('app'), base = location.pathname;
const pin = [1,2,3,4,5,6].map(n => `<input id="pin-code-${n}" class="pin-code-input__input"
  type="text" inputmode="numeric" autocomplete="off"
  aria-label="Verification code digit ${n}">`).join('');
const views = {
  email: `<h2>You don't need to have an account</h2><form id="start">
    <div class="input-row"><label for="primary-email">Email Address <span>*</span></label>
      <input type="email" id="primary-email" name="primary-email" aria-required="true"></div>
    <div class="input-row input-row--invisible" aria-hidden="true">
      <label for="honey-pot">honeypot</label>
      <input type="text" id="honey-pot" name="honey-pot" tabindex="-1"></div>
    <div><input type="checkbox" id="legal" required
        style="position:absolute;opacity:0;width:0;height:0">
      <label for="legal">I agree to receive Northwind recruiting related messages via the
      communication method I have selected above. Please review our
      <a href="/terms">Terms and Conditions and
      Privacy Policy.</a> <span>*</span></label></div>
    <button type="button">Cancel</button><button type="submit">Next</button></form>
    <div id="check"></div>`,
  pin: `<h2>Verify your email</h2><p>Enter the verification code we sent to your email address.</p>
    <form id="pin"><fieldset class="pin-code-fieldset">${pin}</fieldset>
    <button type="submit">Verify</button></form><p id="bad"></p>`,
  form: `<form id="application-form">
    <label for="f">First name</label><input id="f" name="first" required>
    <label for="e">Email</label><input id="e" name="email" required>
    <label for="r">Resume</label><input id="r" name="resume" type="file">
    <button type="submit">Submit application</button></form>`,
};
const show = (name, path) => {
  history.pushState({}, '', base + path); app.innerHTML = views[name];
};
document.getElementById('apply').addEventListener(
  'click', () => setTimeout(() => show('email', '/apply/email'), 300));
document.addEventListener('submit', e => {
  e.preventDefault();
  if (e.target.id === 'start') {
    const email = document.getElementById('primary-email').value;
    if (!email || !document.getElementById('legal').checked) return;
    window.typedTrap = document.getElementById('honey-pot').value;
    document.documentElement.setAttribute('data-trap', window.typedTrap);
    document.getElementById('check').innerHTML =
      '<iframe title="challenge" width="320" height="420" src="' + base +
      '/hcaptcha.com/challenge"></iframe>';
  } else if (e.target.id === 'pin') {
    const boxes = [...document.querySelectorAll('.pin-code-input__input')];
    const typed = boxes.map(i => i.value).join('');
    if (typed === '__CODE__') show('form', '/apply/section/1');
    else document.getElementById('bad').textContent = 'That code is not right.';
  }
});
// The owner solving the check: the test marks the document, as his clicks would end it.
const solved = setInterval(() => {
  if (document.documentElement.getAttribute('data-solved') !== '1') return;
  clearInterval(solved); show('pin', '/apply/verify');
}, 40);
</script>""".replace(b"__CODE__", CODE.encode())


def at_the_picture_check(runtime, site, state_root):
    """Open the posting, follow Apply, fill the first step and press its Next."""
    Site.pages[JOB] = CAREERS
    opened = runtime.open(site + JOB)
    run_id = opened["run_id"]
    (link,) = opened["application_links"]
    landed = runtime.follow(run_id, opened["observation_id"], link["ref"])
    assert landed["fields"][0]["label"].startswith("Email Address")
    assert landed["fields"][0]["required"] is True
    freeze_resume(state_root, run_id)
    return run_id, runtime.prepare(run_id)


def everything_written(state_root) -> str:
    return "".join(
        path.read_text(errors="ignore")
        for path in state_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".log", ".md", ".txt"}
    )


def test_a_next_that_submits_its_step_is_taken_and_a_picture_check_is_the_owners(board, site):
    runtime, _base, state_root = board
    run_id, result = at_the_picture_check(runtime, site, state_root)
    # The step's Next is a submit of its own form: Rove's guard against accidental
    # submits lets this one observed click through, and the step is taken.
    assert result["captcha"] is True and result["status"] == "NEEDS_USER"
    assert result["reason"] == gates.CAPTCHA_WORDS
    assert result["filled"][0]["label"].startswith("Email Address")
    assert result["filled"][1]["value"] == "Yes"  # the standing contact consent
    # The board's trap for scripts stayed empty.
    assert runtime.page.locator("html").get_attribute("data-trap") == ""
    # The guard is back on once the step was taken.
    assert runtime.page.locator("html").get_attribute("data-rove-submit-armed") is None
    seen = runtime.challenge(run_id)
    assert seen == {"open": True, "showing": True, "moved": False}
    assert runtime.challenge("0" * 12) == {"open": False, "showing": False, "moved": False}


def test_after_the_owner_solves_it_rove_carries_on_in_the_same_tab_with_the_mailed_code(
    board, site, monkeypatch
):
    runtime, _base, state_root = board
    run_id, _result = at_the_picture_check(runtime, site, state_root)
    asked = []

    def mailbox(senders, since, digits, names=()):
        asked.append((list(senders), digits, list(names)))
        return CODE

    monkeypatch.setattr(mail, "verification_code", mailbox)
    runtime.page.evaluate("() => document.documentElement.setAttribute('data-solved', '1')")
    runtime.page.locator("#pin-code-1").wait_for()
    assert runtime.challenge(run_id) == {"open": True, "showing": False, "moved": True}
    # The tab is read where he left it: a fresh load would bring the first step back.
    resumed = runtime.open(site + JOB, in_place=True)
    assert resumed["url"].endswith("/apply/verify") and resumed["code_step"] is True
    assert not resumed.get("manual_takeover_required")
    result = runtime.prepare(run_id)
    assert result["status"] == "READY_FOR_REVIEW" and result["pending"] == []
    assert result["url"].endswith("/apply/section/1")
    assert [f["label"] for f in result["filled"]] == ["First name", "Email", "Resume"]
    # One box per digit, the site's own senders, and the code is written nowhere.
    assert asked[0][1] == (6, 6) and len(asked) == 1
    with workflow.db() as conn:
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM application_events WHERE application_id=?", (run_id,)
            )
        ]
    assert "mailed_code" in kinds
    assert workflow.event_embeds(run_id, "mailed_code", {}) == [
        "→ Entered the code the site mailed to your address"
    ]
    assert CODE not in everything_written(state_root)
    # Without `in_place` the same call starts from a fresh load of the posting.
    again = runtime.open(site + JOB)
    assert again["url"].endswith(JOB) and again["application_links"]


def test_a_code_that_never_comes_or_is_refused_leaves_the_step_to_the_owner(
    board, site, monkeypatch
):
    runtime, _base, state_root = board
    run_id, _result = at_the_picture_check(runtime, site, state_root)
    monkeypatch.setattr(test_browser_guards.live_browser, "CODE_WAIT_SECONDS", 0)
    runtime.page.evaluate("() => document.documentElement.setAttribute('data-solved', '1')")
    runtime.page.locator("#pin-code-1").wait_for()
    for answer in (None, "000000"):
        monkeypatch.setattr(mail, "verification_code", lambda *a, answer=answer: answer)
        runtime.open(site + JOB, in_place=True)
        result = runtime.prepare(run_id)
        (waiting,) = result["pending"]
        assert waiting["manual"] is True and waiting["reason"] == gates.NO_CODE_WORDS
        assert result["status"] == "NEEDS_USER" and result["url"].endswith("/apply/verify")


def test_only_a_mailed_code_is_roves_to_enter():
    one = {"count": 1, "segmented": False}
    assert gates.code_step("Enter the verification code we sent to your email address.", one)
    assert gates.code_step("We emailed you a 6-digit code. Enter the code below.", one)
    assert not gates.code_step("Enter the code we sent by text message to your phone.", one)
    assert not gates.code_step("Enter the code from your authenticator app.", one)
    assert not gates.code_step("Promo code", one)
    assert not gates.code_step("Enter the verification code we sent to your email.", {"count": 0})
    form = "https://fa-abcd-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/job/1"
    assert gates.code_senders(form, form) == ["oraclecloud.com", "oracle.com"]
    assert gates.code_senders("https://careers.example.com/jobs/1") == ["example.com"]


# --- the worker: one card, then carrying on by itself -------------------------------------


def stub_browser(monkeypatch, answers: dict) -> list:
    calls = []

    def browser(action, **kwargs):
        calls.append((action, kwargs))
        answer = answers[action]
        return json.loads(json.dumps(answer(kwargs) if callable(answer) else answer))

    monkeypatch.setattr(worker, "browser_call", browser)
    return calls


def test_a_picture_check_is_one_plain_card_and_rove_picks_it_up_once_it_is_solved(
    state, monkeypatch
):
    discord_calls, _posted = enabled_worker(monkeypatch)
    seen = {"open": True, "showing": True, "moved": False}
    page = {
        "url": "https://jobs.example.com/form",
        "observation_id": "o1",
        "fields": [],
        "text": "",
        "application_links": [],
        "ats_markers": {"captcha_challenge": True},
    }
    calls = stub_browser(monkeypatch, {"open": page, "challenge": lambda _kw: seen})
    app = workflow.enqueue(
        "https://jobs.example.com/check", source="owner_link", title="Northwind — Intern"
    )["application_id"]
    assert worker.tick()["status"] == "MANUAL_TAKEOVER"
    card = the_card(discord_calls)
    assert "CAPTCHA needs you" in card["description"]
    assert "I carry on by myself in the same tab" in card["description"]
    hold = workflow.latest_hold(app)
    assert hold["watch"] == "captcha" and hold["in_place"] is True
    # Still on screen, or gone without the page moving on: nothing happens.
    assert worker.carry_on_after_captcha() == []
    seen.update(showing=False)
    assert worker.carry_on_after_captcha() == []
    assert workflow.get(app)["status"] == "MANUAL_TAKEOVER"
    # Solved: the card leaves, and the next pass reads the tab where he left it.
    seen.update(moved=True)
    calls.clear()
    worker.tick()
    assert ("open", {"url": "https://jobs.example.com/check", "in_place": True}) in calls
    with workflow.db() as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM application_events")]
    assert "captcha_cleared" in kinds
    assert len(posts(discord_calls)) >= 1


def test_the_first_pass_never_reads_a_tab_in_place(state, monkeypatch):
    enabled_worker(monkeypatch)
    page = {"url": "https://jobs.example.com/x", "fields": [], "text": "", "application_links": []}
    calls = stub_browser(monkeypatch, {"open": page})
    workflow.enqueue("https://jobs.example.com/first", source="owner_link", title="A — B")
    worker.tick()
    assert calls[0] == ("open", {"url": "https://jobs.example.com/first", "in_place": False})


@pytest.mark.parametrize("kind", ["mailed_code", "captcha_cleared"])
def test_the_thread_lines_for_these_steps_carry_nothing_private(kind):
    (line,) = workflow.event_embeds("a" * 12, kind, {"host": "careers.example.com"})
    assert line.startswith("→ ") and "example" not in line and "a" * 12 not in line
