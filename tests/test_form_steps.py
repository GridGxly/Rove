"""Real headless browser: dates, long wizards, twin questions, choice grids and uploads.

Dates are written the way each control takes them; a wizard of seven pages with varied
"next" wording ends on its review page; a video-interview step is handed to the owner by
name with its link; two questions with the same words keep their own answers; a grid of
checkboxes reads as one question per row; an upload that takes no PDF stops with a plain
reason; and the browser client reads a reply of any length within a time bound per action.
The pages are synthetic and served offline, as in test_frames.
"""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from test_frames import Board, Employer, freeze_resume, sites  # noqa: F401 -- the fixture

from rove import dates, form_reading, live_browser, worker, workflow

MONTH_OPTIONS = "".join(
    f'<option value="{i}">{name.title()}</option>' for i, name in enumerate(dates.MONTHS, start=1)
)
DATES = (
    """<!doctype html><title>Apply - Northwind</title><form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="s">Available start date</label><input id="s" name="start" type="date" required></div>
<div><label for="g">Graduation date</label><input id="g" name="grad" type="month" required></div>
<div><label for="m">Earliest start date</label><input id="m" name="start_text" placeholder="MM/DD/YYYY" required></div>
<fieldset><legend>Expected graduation date</legend>
<select name="gm" aria-label="Month" required><option value="">Month</option>"""
    + MONTH_OPTIONS
    + """</select>
<select name="gy" aria-label="Year" required><option value="">Year</option><option>2026</option><option>2027</option><option>2028</option></select>
</fieldset>
<button type="submit">Submit application</button></form>
<script>/* This box writes the slashes itself as digits are typed. */
const m = document.getElementById('m');
m.addEventListener('input', () => { const d = m.value.replace(/\\D/g, '').slice(0, 8);
  m.value = [d.slice(0, 2), d.slice(2, 4), d.slice(4)].filter(Boolean).join('/'); });</script>"""
)
# A mask the box does not enforce, and a day-precision box for a month-only fact.
LOOSE_DATES = """<!doctype html><title>Apply - Northwind</title><form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="x">Expected graduation</label><input id="x" name="grad_text" placeholder="MM/YYYY" required></div>
<div><label for="d">Graduation date</label><input id="d" name="grad_day" type="date" required></div>
<button type="submit">Submit application</button></form>"""

# One page per step, each moved on with different words, then a review with Edit links.
WIZARD = """<!doctype html><title>Apply - Northwind</title><div id="step"></div>
<script>
const steps = [
  ['<label for="a">First name</label><input id="a" name="first" required>', 'Proceed'],
  ['<label for="b">Last name</label><input id="b" name="last" required>', 'Next: Experience'],
  ['<label for="c">Email</label><input id="c" name="email" type="email" required>', 'Save &amp; Continue'],
  ['<label for="d">Phone</label><input id="d" name="phone" type="tel" required>', 'Continue to step 5'],
  ['<label for="e">Available start date</label><input id="e" name="start" type="date" required>', 'Next ›'],
  ['<label for="g">Full name</label><input id="g" name="full" required>', 'Review'],
];
let at = 0;
const show = () => {
  const box = document.getElementById('step');
  if (at < steps.length) {
    box.innerHTML = '<p>Step ' + (at + 1) + ' of 7</p><form onsubmit="return false">'
      + steps[at][0] + '<button type="button" id="go">' + steps[at][1] + '</button></form>';
    document.getElementById('go').onclick = () => { at++; show(); };
  } else {
    box.innerHTML = '<h2>Review your application</h2>'
      + '<div>First name: Alex <a href="#" class="edit">Edit</a></div>'
      + '<div>Email: alex@example.invalid <a href="#" class="edit">Edit</a></div>'
      + '<button type="button" id="send">Submit application</button>';
  }
};
show();
</script>"""
VIDEO_STEP = """<!doctype html><title>Apply - Northwind</title><div id="step">
<form onsubmit="return false"><label for="a">First name</label><input id="a" name="first" required>
<button type="button" id="go">Next</button></form></div>
<script>document.getElementById('go').onclick = () => {
  document.getElementById('step').innerHTML = '<h2>Video interview</h2>'
    + '<p>Answer three questions on camera.</p>'
    + '<a href="https://northwind.hirevue.com/interviews/synthetic">Start video interview</a>';
};</script>"""
LEAVES = """<!doctype html><title>Apply - Northwind</title>
<form onsubmit="return false"><label for="a">First name</label><input id="a" name="first" required>
<button type="button" onclick="location.assign('__BOARD__/elsewhere')">Continue</button></form>"""
ELSEWHERE = """<!doctype html><title>Elsewhere</title>
<form><label for="z">Last name</label><input id="z" name="last" required>
<button type="submit">Submit application</button></form>"""

# Two questions with the same words, in different sections, and a grid of checkboxes.
TWINS = """<!doctype html><title>Apply - Northwind</title><form id="application-form">
<h2>Your setup</h2><div><label>Favorite tool <input name="tool"></label></div>
<h2>Your team's setup</h2><div><label>Favorite tool <input name="tool"></label></div>
<h2>Contact</h2><div><label>Email <input name="email" type="email" required></label></div>
<h2>Someone we may contact</h2><div><label>Email <input name="email" type="email"></label></div>
<table><caption>When can you work?</caption>
<tr><th></th><th>Mornings</th><th>Afternoons</th><th>Evenings</th></tr>
<tr><th>Weekdays</th>
<td><input type="checkbox" name="avail[]" value="wd-m" aria-label="Weekday mornings"></td>
<td><input type="checkbox" name="avail[]" value="wd-a" aria-label="Weekday afternoons"></td>
<td><input type="checkbox" name="avail[]" value="wd-e" aria-label="Weekday evenings"></td></tr>
<tr><th>Weekends</th>
<td><input type="checkbox" name="avail[]" value="we-m" aria-label="Weekend mornings"></td>
<td><input type="checkbox" name="avail[]" value="we-a" aria-label="Weekend afternoons"></td>
<td><input type="checkbox" name="avail[]" value="we-e" aria-label="Weekend evenings"></td></tr>
</table>
<button type="submit">Submit application</button></form>"""
WORD_ONLY = """<!doctype html><title>Apply - Northwind</title><form id="application-form">
<div><label for="f">First name</label><input id="f" name="first" required></div>
<div><label for="r">Resume</label><input id="r" name="resume" type="file" accept=".doc,.docx" required></div>
<button type="submit">Submit application</button></form>"""


@pytest.fixture
def steps(sites):  # noqa: F811 -- the imported fixture, extended with this module's pages
    runtime, employer, board, state = sites
    Employer.pages.update(
        {
            "/dates/jobs/9": DATES,
            "/dates/jobs/10": LOOSE_DATES,
            "/wizard/jobs/11": WIZARD,
            "/wizard/jobs/12": VIDEO_STEP,
            "/wizard/jobs/13": LEAVES.replace("__BOARD__", board),
            "/twins/jobs/14": TWINS,
            "/upload/jobs/15": WORD_ONLY,
        }
    )
    Board.pages["/elsewhere"] = ELSEWHERE
    return runtime, employer, state


def prepared(runtime, state, url: str) -> tuple[dict, dict]:
    opened = runtime.open(url)
    freeze_resume(state, opened["run_id"])
    return opened, runtime.prepare(opened["run_id"])


def answer(run_id: str, key: str, value: str, message: str):
    worker.apply_command(
        {"kind": "answer", "application_id": run_id, "field_key": key, "value": value}, message
    )


# --- dates ---------------------------------------------------------------------------


def test_each_date_control_gets_the_approved_date_in_its_own_format(steps):
    runtime, employer, state = steps
    opened, result = prepared(runtime, state, f"{employer}/dates/jobs/9")
    labels = [f["label"] for f in opened["fields"]]
    assert (
        "Expected graduation date — Month" in labels and "Expected graduation date — Year" in labels
    )
    assert not result["pending"], result["pending"]
    filled = {f["label"]: f["value"] for f in result["filled"]}
    assert filled["Available start date"] == "2027-06-01"
    assert filled["Graduation date"] == "2027-12"
    assert filled["Earliest start date"] == "06/01/2027"
    assert filled["Expected graduation date — Month"] == "December"
    assert filled["Expected graduation date — Year"] == "2027"
    page = runtime.page
    assert page.locator("#s").input_value() == "2027-06-01"
    assert page.locator("#g").input_value() == "2027-12"
    assert page.locator("#m").input_value() == "06/01/2027"
    assert page.locator("select[name=gm]").input_value() == "12"
    assert page.locator("select[name=gy]").input_value() == "2027"


def test_a_mask_the_box_does_not_enforce_is_typed_whole_and_no_day_is_invented(steps):
    runtime, employer, state = steps
    _opened, result = prepared(runtime, state, f"{employer}/dates/jobs/10")
    filled = {f["label"]: f["value"] for f in result["filled"]}
    assert filled["Expected graduation"] == "12/2027"
    assert runtime.page.locator("#x").input_value() == "12/2027"
    # The approved graduation date is a month: a box that needs a day stays a question.
    pending = {q["label"]: q for q in result["pending"]}
    assert "way this field takes it" in pending["Graduation date"]["reason"]
    assert runtime.page.locator("#d").input_value() == ""


def test_dates_are_read_once_and_written_per_control():
    assert dates.parse("2027-06-01") == (2027, 6, 1)
    assert dates.parse("June 1st, 2027") == (2027, 6, 1)
    assert dates.parse("1 June 2027") == (2027, 6, 1)
    assert dates.parse("December 2027") == (2027, 12, None)
    assert dates.parse("12/2027") == (2027, 12, None)
    assert dates.parse("06/01/2027") == (2027, 6, 1)
    assert dates.parse("25/12/2027") == (2027, 12, 25)  # a first part above 12 is the day
    for vague in ("Spring 2027", "2027", "soon", "13/2027", "February 30, 2027", ""):
        assert dates.parse(vague) is None, vague
    date_box, month_box = {"kind": "date"}, {"kind": "month"}
    assert dates.for_input("June 1, 2027", date_box) == "2027-06-01"
    assert dates.for_input("December 2027", date_box) is None
    assert dates.for_input("December 2027", month_box) == "2027-12"
    assert dates.for_input("TBD", month_box) is None
    us = {"kind": "text", "placeholder": "mm/dd/yyyy"}
    iso = {"kind": "text", "placeholder": "YYYY-MM-DD"}
    labelled = {"kind": "text", "label": "Start date (DD.MM.YYYY)"}
    assert dates.for_input("June 1, 2027", us) == "06/01/2027"
    assert dates.for_input("June 1, 2027", iso) == "2027-06-01"
    assert dates.for_input("June 1, 2027", labelled) == "01.06.2027"
    assert dates.for_input("TBD", us) == "TBD"  # an unread answer goes as written
    plain = {"kind": "text", "label": "Favorite tool", "placeholder": "e.g. vim"}
    assert dates.for_input("June 2027", plain) == "June 2027" and not dates.is_date_box(plain)
    assert dates.same_date("06/01/2027", "06012027") and not dates.same_date("06/01/2027", "")


def test_month_day_and_year_dropdowns_take_their_one_option():
    months = [{"label": "Month", "value": ""}] + [
        {"label": f"{i:02d} - {n.title()}", "value": str(i)}
        for i, n in enumerate(dates.MONTHS, start=1)
    ]
    years = [{"label": "Year", "value": ""}] + [{"label": str(y)} for y in range(2024, 2031)]
    days = [{"label": "--"}] + [{"label": str(d)} for d in range(1, 32)]
    assert dates.part_of(months) == "month" and dates.part_of(years) == "year"
    assert dates.part_of(days) == "day"
    assert dates.part_of([{"label": "Fall 2027"}, {"label": "Spring 2028"}]) is None
    assert dates.part_of([{"label": str(h)} for h in range(1, 13)]) == "month"
    assert dates.option_for(months, "December 2027")["value"] == "12"
    assert dates.option_for(years, "December 2027")["label"] == "2027"
    assert dates.option_for(days, "December 2027") is None  # no day is invented
    assert dates.option_for(days, "2027-06-01")["label"] == "1"
    assert dates.question_label("Graduation month") == "graduation date"
    assert dates.question_label("Expected graduation date — Year") == "expected graduation date"
    assert dates.question_label("Month") is None
    assert dates.question_label("Graduation date") is None


# --- wizards -------------------------------------------------------------------------


def test_a_seven_page_wizard_with_varied_wording_ends_on_its_review_page(steps):
    runtime, employer, state = steps
    opened, result = prepared(runtime, state, f"{employer}/wizard/jobs/11")
    assert [c["label"] for c in opened["nav_controls"]] == ["Proceed"]
    assert result["status"] == "READY_FOR_REVIEW", result.get("reason")
    assert [f["label"] for f in result["filled"]] == [
        "First name",
        "Last name",
        "Email",
        "Phone",
        "Available start date",
        "Full name",
    ]
    assert len(result["pages"]) == 7
    assert result["fields"] == [] and result["form_state"] == []
    assert [c["label"] for c in result["final_controls"]] == ["Submit application"]
    with workflow.db() as conn:
        clicked = [
            json.loads(r[0])["clicked"]
            for r in conn.execute(
                "SELECT data FROM application_events WHERE application_id=? AND kind='form_step'",
                (opened["run_id"],),
            )
        ]
    assert clicked == [
        "Proceed",
        "Next: Experience",
        "Save & Continue",
        "Continue to step 5",
        "Next ›",
        "Review",
    ]


def test_a_video_interview_step_is_handed_to_the_owner_by_name_with_its_link(steps):
    runtime, employer, state = steps
    _opened, result = prepared(runtime, state, f"{employer}/wizard/jobs/12")
    assert result["status"] == "NEEDS_USER" and not result["pending"]
    assert result["owner_step"] and result["headline"] == "Video interview step"
    assert "recorded video interview on HireVue" in result["reason"]
    assert "https://northwind.hirevue.com/interviews/synthetic" in result["reason"]
    assert "Final step" not in result["reason"]


NAV_PAGE = """<!doctype html><title>Nav</title><form><label for="a">First name</label>
<input id="a"><div id="buttons"></div></form>"""


@pytest.mark.parametrize(
    "label,moves",
    [
        ("Next", True),
        ("Proceed", True),
        ("Review", True),
        ("Save & Continue", True),
        ("Save and Next", True),
        ("Next: Experience", True),
        ("Next – Education", True),
        ("Continue to Experience", True),
        ("Continue to step 3", True),
        ("Go to the next step", True),
        ("Next (2 of 5)", True),
        ("Continue ›", True),
        ("Review and submit", True),
        ("Continue to LinkedIn", False),
        ("Continue to site", False),
        ("Go to dashboard", False),
        ("Proceed to my profile", False),
        ("Continue with Google", False),
        ("Back", False),
        ("Save and exit", False),
    ],
)
def test_the_words_that_move_a_wizard_on(steps, label, moves):
    runtime, employer, _state = steps
    Employer.pages["/nav/jobs/16"] = NAV_PAGE.replace(
        '<div id="buttons"></div>', f'<button type="button">{label}</button>'
    )
    opened = runtime.open(f"{employer}/nav/jobs/16")
    assert [c["label"] for c in opened["nav_controls"]] == ([label] if moves else [])


def test_owner_steps_are_named_from_the_page_markers():
    assessment = {
        "url": "https://example.test/apply",
        "ats_markers": {
            "assessment": {
                "kind": "assessment",
                "host": "app.codesignal.com",
                "link": "https://app.codesignal.com/test/x",
            }
        },
    }
    step = form_reading.owner_step(assessment)
    assert step["headline"] == "Assessment step"
    assert "online assessment on CodeSignal" in step["reason"]
    assert form_reading.owner_step({"ats_markers": {}}) is None


def test_a_step_that_leaves_the_site_stops_the_typing(steps):
    runtime, employer, state = steps
    _opened, result = prepared(runtime, state, f"{employer}/wizard/jobs/13")
    assert [f["label"] for f in result["filled"]] == ["First name"]
    assert result["status"] == "NEEDS_USER"
    assert "moved to another site" in result["reason"]
    assert runtime.form.locator("#z").input_value() == ""


# --- twin questions and grids ----------------------------------------------------------


def test_twin_questions_keep_their_own_keys_and_answers(steps):
    runtime, employer, state = steps
    opened = runtime.open(f"{employer}/twins/jobs/14")
    tools = [f for f in opened["fields"] if f["label"] == "Favorite tool"]
    emails = [f for f in opened["fields"] if f["label"] == "Email"]
    assert len({f["key"] for f in tools + emails}) == 4
    # The first of each keeps the key it always had; the second says where it is.
    first = {k: tools[0][k] for k in ("label", "name", "id", "kind", "options")}
    assert tools[0]["key"] == workflow.field_key(first) and "occurrence" not in tools[0]
    assert tools[1]["section"] == "Your team's setup" and tools[1]["occurrence"] == 1
    assert emails[1]["section"] == "Someone we may contact"
    # A grid of checkboxes is one question per row, its options named by the columns.
    rows = [f for f in opened["fields"] if f["kind"] == "checkbox_group"]
    assert [r["label"] for r in rows] == [
        "When can you work? — Weekdays",
        "When can you work? — Weekends",
    ]
    assert [o["label"] for o in rows[0]["options"]] == ["Mornings", "Afternoons", "Evenings"]
    run_id = opened["run_id"]
    answer(run_id, tools[1]["key"], "A soldering iron", "msg-tool")
    answer(run_id, rows[1]["key"], "Mornings, Evenings", "msg-grid")
    freeze_resume(state, run_id)
    result = runtime.prepare(run_id)
    filled = {f["key"]: f["value"] for f in result["filled"]}
    assert filled[tools[1]["key"]] == "A soldering iron" and tools[0]["key"] not in filled
    assert [q["key"] for q in result["pending"] if q["label"] == "Favorite tool"] == [
        tools[0]["key"]
    ]
    assert filled[rows[1]["key"]] == "Mornings, Evenings"
    page = runtime.page
    assert page.locator("input[name=tool]").nth(0).input_value() == ""
    assert page.locator("input[name=tool]").nth(1).input_value() == "A soldering iron"
    ticked = [b.get_attribute("value") for b in page.locator("input[name='avail[]']:checked").all()]
    assert ticked == ["we-m", "we-e"]


# --- uploads -------------------------------------------------------------------------


def test_an_upload_that_takes_no_pdf_stops_with_a_plain_reason(steps):
    runtime, employer, state = steps
    _opened, result = prepared(runtime, state, f"{employer}/upload/jobs/15")
    (resume,) = [q for q in result["pending"] if q["label"] == "Resume"]
    assert resume["reason"] == (
        "This upload takes only .doc, .docx files, and the approved resume is a PDF"
    )
    assert runtime.page.locator("#r").evaluate("e => e.files.length") == 0
    assert result["status"] == "NEEDS_USER"
    assert live_browser.accepts_pdf("") and live_browser.accepts_pdf(".PDF, .docx")
    assert live_browser.accepts_pdf("application/pdf") and live_browser.accepts_pdf("*/*")
    assert not live_browser.accepts_pdf("image/*")


# --- the browser client --------------------------------------------------------------


@pytest.fixture
def daemon(monkeypatch):
    """A stand-in for the browser daemon on a Unix socket: replies with `reply` after `delay`."""
    folder = Path(tempfile.mkdtemp(prefix="rove-"))
    path = folder / "b.sock"
    monkeypatch.setattr(live_browser, "socket_path", lambda: path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    state = {"reply": b"", "delay": 0.0, "requests": []}

    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            with client:
                state["requests"].append(client.makefile("rb").readline())
                time.sleep(state["delay"])
                try:
                    client.sendall(state["reply"])
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    yield state
    server.close()
    path.unlink(missing_ok=True)
    folder.rmdir()


def test_the_client_reads_a_reply_of_any_length(daemon):
    text = "x" * 600_000
    daemon["reply"] = json.dumps({"result": {"text": text}}).encode() + b"\n"
    assert live_browser.browser_call("status")["text"] == text
    assert json.loads(daemon["requests"][0]) == {"action": "status"}


def test_the_client_waits_per_action_and_not_forever(daemon, monkeypatch):
    seconds = live_browser.CALL_SECONDS
    assert seconds["prepare"] >= 600 and seconds["submit"] > 45 and seconds["open"] >= 120
    assert seconds["status"] <= 30 and seconds["close"] <= 30
    monkeypatch.setitem(live_browser.CALL_SECONDS, "status", 0.5)
    daemon["reply"], daemon["delay"] = b'{"result": {}}\n', 2.0
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        live_browser.browser_call("status")
    assert time.monotonic() - began < 1.5
