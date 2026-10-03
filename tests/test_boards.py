"""Paylocity, Workable, JazzHR and BambooHR: recognition, job keys, reading and results.

The fixtures under `tests/fixtures/boards/` are reduced copies of each board's public
pages with invented employers and questions. They are loaded in a real headless browser
at the board's own addresses; every request is answered from a fixture file or refused,
so nothing leaves the machine and nothing is ever sent to a board.
"""

import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from patchright.sync_api import sync_playwright

from rove import boards, live_browser, submission
from rove.boards import bamboohr, jazzhr, paylocity, workable
from rove.boards.base import affirms
from rove.live_browser import RecruitingBrowser, approved_ats, job_scope

FIXTURES = Path(__file__).parent / "fixtures" / "boards"

PAYLOCITY = "https://recruiting.paylocity.com/Recruiting/Jobs"
WORKABLE = "https://apply.workable.com/larkspur-labs/j/A1B2C3D4E5"
JAZZHR = "https://pinecrest.applytojob.com/apply"
BAMBOOHR = "https://tidewater.bamboohr.com/careers/17"

# Where each fixture lives on its board. A form drawn in place shares its posting's URL.
PAGES = {
    "paylocity": {
        "posting": f"{PAYLOCITY}/Details/990001",
        "apply": f"{PAYLOCITY}/Apply/990001",
        "review": f"{PAYLOCITY}/Apply/990001",
        "error": f"{PAYLOCITY}/Apply/990001",
        "confirmation": f"{PAYLOCITY}/Success/990001",
    },
    "workable": {
        "posting": f"{WORKABLE}/",
        "apply": f"{WORKABLE}/apply/",
        "error": f"{WORKABLE}/apply/",
        "confirmation": f"{WORKABLE}/apply/",
    },
    "jazzhr": {
        "apply": f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician",
        "error": f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician",
        "confirmation": f"{JAZZHR}/confirm/Zx9Kq2LmNp",
    },
    "bamboohr": {
        "posting": BAMBOOHR,
        "apply": BAMBOOHR,
        "error": BAMBOOHR,
        "confirmation": BAMBOOHR,
    },
}

# The page the Submit control is on, the form's own request, and how a refusal answers.
SENDS = {
    "paylocity": {
        "adapter": paylocity.PaylocityV1,
        "form": "review",
        "post": ("recruiting.paylocity.com", "/Recruiting/Jobs/Apply/990001"),
        "accepted": 200,
        "refused": 200,  # the reply to a refused form is still a 200, with messages in it
    },
    "workable": {
        "adapter": workable.WorkableV1,
        "form": "apply",
        "post": ("apply.workable.com", "/api/v1/jobs/A1B2C3D4E5/apply"),
        "accepted": 200,
        "refused": 422,
    },
    "jazzhr": {
        "adapter": jazzhr.JazzHRV1,
        "form": "apply",
        "post": ("pinecrest.applytojob.com", "/apply/Zx9Kq2LmNp/Field-Technician"),
        "accepted": 302,
        "refused": None,  # the page's own script stops the form before any request
    },
    "bamboohr": {
        "adapter": bamboohr.BambooHRV1,
        "form": "apply",
        "post": ("tidewater.bamboohr.com", "/careers/17/add"),
        "accepted": 200,
        "refused": None,
    },
}
BOARD_NAMES = sorted(SENDS)


def response(board: str, status: int) -> dict:
    host, path = SENDS[board]["post"]
    return {"host": host, "path": path, "status": status}


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def show(chromium, tmp_path, monkeypatch):
    """Load a fixture at its board address and return Rove's own observation of it."""
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(live_browser.workflow, "config", lambda: {"human_pacing": False})
    context = chromium.new_context()
    page = context.new_page()
    runtime = RecruitingBrowser(headless=True)
    runtime.page = page
    runtime.run = {"id": "abcdef012345", "profile_hash": "x"}
    served, posts = {}, {}

    def serve(route):
        request = route.request
        if request.method == "POST" and urlsplit(request.url).path in posts:
            status = posts[urlsplit(request.url).path]
            route.fulfill(status=status, content_type="application/json", body="[]")
        elif request.method == "GET" and request.url in served:
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=served[request.url]
            )
        else:
            route.abort()

    context.route("**/*", serve)

    def load(board: str, name: str, html: str | None = None) -> dict:
        url = PAGES[board][name]
        served.clear()
        posts.clear()
        served[url] = html if html is not None else (FIXTURES / board / f"{name}.html").read_text()
        page.goto(url)
        return runtime.observe()

    load.page = page
    load.observe = runtime.observe
    load.served = served
    load.posts = posts
    yield load
    context.close()


def field(observation: dict, text: str, **wanted) -> dict | None:
    """The first observed field whose label contains the text and matches the rest."""
    for item in observation["fields"]:
        if text.lower() in (item["label"] or "").lower() and all(
            item.get(k) == v for k, v in wanted.items()
        ):
            return item
    return None


# --- recognition ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,board",
    [
        (f"{PAYLOCITY}/Details/990001", paylocity),
        ("https://recruiting.paylocity.com/recruiting/jobs/Apply/990001/Northwind", paylocity),
        (f"{WORKABLE}/apply/", workable),
        ("https://apply.workable.com/j/A1B2C3D4E5", workable),
        (f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician", jazzhr),
        (BAMBOOHR, bamboohr),
        ("https://tidewater.bamboohr.com/jobs/view.php?id=17", bamboohr),
    ],
)
def test_board_pages_are_recognised_by_their_exact_hosts(url, board):
    assert boards.board_for(url) is board
    assert boards.scope(url) is not None
    assert approved_ats(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://recruiting.paylocity.com.example.net/Recruiting/Jobs/Apply/990001",
        "https://recruiting-paylocity.com/Recruiting/Jobs/Apply/990001",
        "https://paylocity.com/Recruiting/Jobs/Apply/990001",
        "https://jobs.example/recruiting.paylocity.com/Recruiting/Jobs/Apply/990001",
        "http://recruiting.paylocity.com/Recruiting/Jobs/Apply/990001",
        "https://recruiting.paylocity.com:8443/Recruiting/Jobs/Apply/990001",
        "https://recruiting.paylocity.com:port/Recruiting/Jobs/Apply/990001",
        "https://owner@recruiting.paylocity.com/Recruiting/Jobs/Apply/990001",
        "https://apply.workable.com.example.net/larkspur-labs/j/A1B2C3D4E5",
        "https://applyworkable.com/larkspur-labs/j/A1B2C3D4E5",
        "https://applytojob.com/apply/Zx9Kq2LmNp",
        "https://www.applytojob.com/apply/Zx9Kq2LmNp",
        "https://pinecrest.applytojob.com.example.net/apply/Zx9Kq2LmNp",
        "https://pinecrest.jobs.applytojob.com/apply/Zx9Kq2LmNp",
        "https://pinecrest-applytojob.com/apply/Zx9Kq2LmNp",
        "https://bamboohr.com/careers/17",
        "https://www.bamboohr.com/careers/17",
        "https://images7.bamboohr.com/careers/17",
        "https://tidewater.bamboohr.com.example.net/careers/17",
        "https://tidewater.bamboohr.co/careers/17",
        "https://tidewaterbamboohr.com/careers/17",
    ],
)
def test_look_alike_hosts_are_not_a_board(url):
    assert boards.board_for(url) is None
    assert boards.scope(url) is None
    assert boards.apply_url(url) is None
    assert not approved_ats(url)
    assert not any(adapter.matches(url) for adapter in boards.ADAPTERS.values())


@pytest.mark.parametrize(
    "url",
    [
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/00000000-0000-4000-8000-000000000001",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Apply/not-a-number",
        "https://apply.workable.com/larkspur-labs/",
        "https://apply.workable.com/api/v1/jobs/A1B2C3D4E5/form",
        "https://pinecrest.applytojob.com/apply",
        "https://pinecrest.applytojob.com/apply/confirm",
        "https://tidewater.bamboohr.com/careers",
        "https://tidewater.bamboohr.com/careers/17/detail",
    ],
)
def test_a_board_page_that_is_not_a_job_has_no_job_key(url):
    assert boards.board_for(url) is not None
    assert boards.scope(url) is None
    assert not any(adapter.matches(url) for adapter in boards.ADAPTERS.values())


# --- job keys ------------------------------------------------------------------------


def test_a_posting_its_form_and_its_confirmation_share_one_job_key():
    for pages in PAGES.values():
        assert len({job_scope(url) for url in pages.values()}) == 1
    assert job_scope(f"{PAYLOCITY}/Apply/990001/Northwind-Analytics") == job_scope(
        f"{PAYLOCITY}/Details/990001"
    )
    # The short link redirects to the account's path: the shortcode alone names the job.
    assert job_scope("https://apply.workable.com/j/a1b2c3d4e5") == job_scope(f"{WORKABLE}/apply/")
    assert job_scope("https://tidewater.bamboohr.com/jobs/view.php?id=17") == job_scope(BAMBOOHR)


def test_job_keys_differ_across_boards_tenants_and_jobs():
    keys = [
        job_scope(f"{PAYLOCITY}/Apply/990001"),
        job_scope(f"{PAYLOCITY}/Apply/990002"),
        job_scope(f"{WORKABLE}/apply/"),
        job_scope("https://apply.workable.com/larkspur-labs/j/F6E7D8C9B0/apply/"),
        job_scope(f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician"),
        job_scope(f"{JAZZHR}/zx9kq2lmnp/Field-Technician"),  # codes are case-sensitive
        job_scope(f"{JAZZHR}/Ab3Cd4Ef5G/Field-Technician"),
        job_scope("https://oakfield.applytojob.com/apply/Zx9Kq2LmNp/Field-Technician"),
        job_scope(BAMBOOHR),
        job_scope("https://tidewater.bamboohr.com/careers/18"),
        job_scope("https://oakfield.bamboohr.com/careers/17"),
    ]
    assert len(set(keys)) == len(keys)
    assert all(key[0] in {"paylocity", "workable", "jazzhr", "bamboohr"} for key in keys)


def test_existing_job_keys_are_unchanged():
    assert job_scope("https://job-boards.greenhouse.io/acme/jobs/1234567") == (
        "greenhouse",
        "acme",
        "1234567",
    )
    assert job_scope("https://careers.example.com/jobs/REQ12345/apply") == (
        "careers.example.com",
        "REQ12345",
    )


# --- where the form lives ------------------------------------------------------------


def test_apply_urls_are_derived_from_a_posting():
    assert boards.apply_url(f"{PAYLOCITY}/Details/990001") == f"{PAYLOCITY}/Apply/990001"
    assert boards.apply_url(f"{WORKABLE}/") == f"{WORKABLE}/apply/"
    assert (
        boards.apply_url("https://apply.workable.com/j/A1B2C3D4E5")
        == "https://apply.workable.com/j/A1B2C3D4E5/apply/"
    )
    # JazzHR and BambooHR show the form at the posting's own address.
    assert boards.apply_url(f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician?source=feed") == (
        f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician"
    )
    assert boards.apply_url(f"{JAZZHR}/confirm/Zx9Kq2LmNp") is None
    assert boards.apply_url("https://tidewater.bamboohr.com/jobs/view.php?id=17") == BAMBOOHR
    assert workable.form_definition_url(f"{WORKABLE}/") == (
        "https://apply.workable.com/api/v1/jobs/A1B2C3D4E5/form"
    )
    assert bamboohr.form_definition_url(BAMBOOHR) == f"{BAMBOOHR}/detail"


def test_the_application_link_is_found_on_each_posting(show):
    seen = show("paylocity", "posting")
    links = seen["application_links"]
    assert links and all(link["label"] == paylocity.APPLY_LINK["label"] for link in links)
    assert {link["url"] for link in links} == {f"{PAYLOCITY}/Apply/990001"}
    assert boards.apply_url(seen["url"]) == links[0]["url"]
    assert show.page.locator(paylocity.APPLY_LINK["selector"]).count() == 3
    assert not seen["fields"]

    seen = show("workable", "posting")
    (link,) = seen["application_links"]
    assert link["label"] == workable.APPLY_LINK["label"]
    assert link["url"] == boards.apply_url(seen["url"]) == f"{WORKABLE}/apply/"
    assert show.page.locator(workable.APPLY_LINK["selector"]).count() == 1

    # BambooHR swaps the form in at the same address: the control is a button, not a link.
    seen = show("bamboohr", "posting")
    (link,) = seen["application_links"]
    assert link["label"] == bamboohr.APPLY_LINK["label"]
    assert link["url"] is None and link["kind"] == "button"
    assert boards.apply_url(seen["url"]) == seen["url"]
    assert seen["ats_markers"]["bamboohr_form"] is False

    # JazzHR has no link to follow: the posting carries its own form.
    seen = show("jazzhr", "apply")
    assert boards.apply_url(seen["url"]) == seen["url"]
    assert seen["fields"] and seen["ats_markers"]["jazzhr_form"] is True


# --- reading the forms ---------------------------------------------------------------


def test_paylocity_form_is_read_step_by_step(show):
    seen = show("paylocity", "apply")
    for label in ("First Name", "Last Name", "Email Address", "Mobile Number", "LinkedIn"):
        assert field(seen, label, kind="text"), label
    assert field(seen, "Address Line 1", role="combobox", required=True)
    assert field(seen, "City", required=True)
    assert field(seen, "Zip Code", required=True)
    (heard,) = [f for f in seen["fields"] if f["kind"] == "radio_group"]
    assert heard["name"] == "info.howDidYouHearAboutUs"
    assert [o["label"] for o in heard["options"]] == [
        "Online Job Board",
        "Company Website",
        "Other",
    ]
    assert any(f["kind"] == "file" and f["id"] == "btn-resume" for f in seen["fields"])
    # The first step ends in "Next Step"; only the review step carries the final control.
    assert [c["label"] for c in seen["nav_controls"]] == ["Next Step"]
    assert seen["final_controls"] == []
    assert seen["ats_markers"]["paylocity_form"] is True
    assert seen["ats_markers"]["paylocity_errors"] == ""

    review = show("paylocity", "review")
    assert [c["label"] for c in review["final_controls"]] == [paylocity.FINAL_CONTROL["label"]]
    assert review["nav_controls"] == [] and review["fields"] == []
    assert show.page.locator(paylocity.FINAL_CONTROL["selector"]).count() == 1


def test_workable_form_is_read(show):
    seen = show("workable", "apply")
    assert field(seen, "First name", kind="text", required=True)
    assert field(seen, "Last name", kind="text", required=True)
    assert field(seen, "Email", kind="email", required=True)
    assert field(seen, "Code sample or repository link", kind="textarea", required=True)
    assert field(seen, "Notice period", role="combobox")
    assert field(seen, "Tell us about a garden", kind="textarea", required=True)
    assert any(f["kind"] == "file" for f in seen["fields"])
    assert [c["label"] for c in seen["final_controls"]] == [workable.FINAL_CONTROL["label"]]
    assert show.page.locator(workable.FINAL_CONTROL["selector"]).count() == 1
    assert seen["ats_markers"]["workable_form"] is True


def test_jazzhr_form_is_read_and_its_human_check_is_noticed(show):
    seen = show("jazzhr", "apply")
    assert field(seen, "First Name", kind="text", required=True)
    assert field(seen, "Email Address", kind="email", required=True)
    assert field(seen, "Phone", kind="tel", required=True)
    assert field(seen, "LinkedIn Profile URL", required=False)
    assert field(seen, "Resume", kind="file", required=True)
    ladders = field(seen, "comfortable working on ladders", tag="select", required=True)
    assert [o["label"] for o in ladders["options"]] == ["-- No answer --", "Yes", "No"]
    assert field(seen, "Which sorting machines", kind="textarea", required=True)
    # The reCAPTCHA checkbox is on the form: preparation and the send both stop for it.
    assert seen["ats_markers"]["captcha_challenge"] is True
    assert seen["ats_markers"]["jazzhr_form"] is True
    assert show.page.locator(jazzhr.FINAL_CONTROL["selector"]).count() == 1


def test_bamboohr_form_is_read(show):
    seen = show("bamboohr", "apply")
    assert field(seen, "First Name", kind="text", required=True)
    assert field(seen, "Last Name", kind="text", required=True)
    assert field(seen, "Email", required=True)
    assert field(seen, "Phone", required=True)
    assert field(seen, "City", required=True)
    assert field(seen, "Desired Pay", required=False)
    assert field(seen, "Which shift patterns", kind="textarea", required=True)
    assert any(f["kind"] == "file" and f["required"] for f in seen["fields"])
    assert [c["label"] for c in seen["final_controls"]] == [bamboohr.FINAL_CONTROL["label"]]
    assert seen["ats_markers"]["bamboohr_form"] is True
    assert show.page.locator(bamboohr.FINAL_CONTROL["selector"]).count() == 1


def test_the_bamboohr_honeypot_is_never_offered_as_a_question(show):
    """An answer in the trap field marks the application as a bot's, so it is not a field."""
    seen = show("bamboohr", "apply")
    assert show.page.locator(bamboohr.HONEYPOT).count() == 1
    assert field(seen, "leave this field blank") is None
    assert not any(f["id"].startswith("nickname_") for f in seen["fields"])
    # The reader itself leaves the trap out: it is parked off the page. No reference is
    # put on it, so nothing can type into it.
    raw = show.page.evaluate(live_browser.OBSERVE)["fields"]
    assert not any(bamboohr.is_trap(f) for f in raw)
    assert show.page.locator(bamboohr.HONEYPOT).get_attribute("data-rove-field") is None
    # The board's own filter is the second line of defence, for the day the markup moves
    # the trap somewhere the reader does not look. Only that board applies it.
    trap = {"label": "Please leave this field blank", "id": "nickname_hpxmpl", "name": ""}
    assert bamboohr.is_trap(trap) and bamboohr.is_trap({"label": "", "name": "nickname_ab12"})
    assert boards.fillable(BAMBOOHR, [*raw, trap]) == raw
    assert boards.fillable(f"{JAZZHR}/Zx9Kq2LmNp/Field-Technician", [*raw, trap]) == [*raw, trap]
    assert boards.fillable("https://careers.example.com/jobs/12345", [*raw, trap]) == [*raw, trap]
    assert not bamboohr.is_trap({"label": "Nickname", "id": "nickname", "name": "nickname"})


def resume_upload(observation: dict) -> bool:
    """The reader would hand the frozen resume to one of the file fields."""
    return any(
        f["kind"] == "file" and "resume" in f"{f['label']} {f['name']} {f['id']}".lower()
        for f in observation["fields"]
    )


def options(item: dict) -> list[str]:
    return [o["label"] for o in item["options"]]


def marked(show, item: dict) -> str | None:
    """The id of the element the field's reference sits on: the thing a click would hit."""
    return show.page.locator(f'[data-rove-field="{item["ref"]}"]').get_attribute("id")


def test_paylocity_required_marks_and_custom_dropdowns_are_read(show):
    seen = show("paylocity", "apply")
    # Required is said in the label, "(required)", not by an attribute or an asterisk.
    for label in ("First Name", "Last Name", "Email Address", "Mobile Number", "LinkedIn"):
        assert field(seen, label, kind="text")["required"], label
    assert field(seen, "School Name")["required"]
    assert not field(seen, "Preferred First Name")["required"]
    assert not field(seen, "Skills")["required"]
    # A react-widgets dropdown has no input element: the widget itself is the field.
    sms = field(seen, "permission to text you")
    assert sms["kind"] == "combobox" and sms["role"] == "combobox" and sms["widget"] == "listbox"
    assert sms["required"] and sms["selected"] is None and sms["value"] == ""
    assert options(sms) == ["--", "Yes*", "No"] and marked(show, sms) == "info.smsOptedIn"
    graduated = field(seen, "Did you Graduate?")
    assert graduated["widget"] == "listbox" and not graduated["required"]
    assert graduated["selected"] is None and options(graduated) == []
    # The radio question's text sits in a label outside the radiogroup.
    heard = field(seen, "How did you hear about us?", kind="radio_group")
    assert heard["label"] == "How did you hear about us?" and heard["required"]
    assert options(heard) == ["Online Job Board", "Company Website", "Other"]
    # A select box shows its value on top of its search input: the label is the question
    # alone, the value is read from the box, and the reference is on the box, which is what
    # a click can reach.
    country = field(seen, "Country")
    assert country["label"] == "Country" and country["selected"] == "United States"
    assert country["widget"] == "shell" and country["role"] == "combobox" and country["required"]
    assert country["id"] == "public-site-address-country"
    assert marked(show, country) == "public-site-address-country-select-wrapper"
    state = field(seen, "State")
    assert state["label"] == "State" and state["selected"] is None and state["widget"] == "shell"
    # The upload controls are named by their buttons; the resume one is found by its id.
    assert resume_upload(seen)
    assert field(seen, "Select Resume to Upload", kind="file")["id"] == "btn-resume"
    assert field(seen, "Upload Cover Letter", kind="file")["id"] == "btn-coverLetter"


def test_workable_styled_radios_and_resume_upload_are_read(show):
    seen = show("workable", "apply")
    # The real radios are hidden; the role=radio wrappers around them are what is clicked.
    service = field(seen, "maintained a production service", kind="radio_group")
    assert service["label"] == "Have you maintained a production service before?"
    assert service["required"] and options(service) == ["YES", "NO"] and service["value"] == ""
    members = [f for f in seen["fields"] if f["ref"] in service["member_refs"]]
    assert [marked(show, m) for m in members] == ["wrapper_q1yes", "wrapper_q1no"]
    assert all(m["in_group"] and m["kind"] == "radio" and not m["checked"] for m in members)
    # The helper input that carries a dropdown's value is not a question.
    assert not any(f["name"] == "CA_20002" for f in seen["fields"])
    # The upload is named by what its ARIA name points at, without "Choose file".
    resume = field(seen, "Resume", kind="file")
    assert resume["label"] == "Resume" and resume["required"] and resume_upload(seen)


def test_jazzhr_submit_link_is_the_final_control(show):
    seen = show("jazzhr", "apply")
    assert [c["label"] for c in seen["final_controls"]] == ["Submit Application"]
    link = show.page.locator('[data-rove-submit="0"]')
    assert link.get_attribute("id") == "resumator-submit-resume"
    assert show.page.locator(jazzhr.FINAL_CONTROL["selector"]).count() == 1


def test_bamboohr_select_toggle_and_resume_upload_are_read(show):
    seen = show("bamboohr", "apply")
    # A toggle button over a hidden select: label and name from the select, value and
    # reference from the button.
    country = field(seen, "Country")
    assert country["label"] == "Country *" and country["required"]
    assert country["widget"] == "toggle" and country["role"] == "combobox"
    assert country["name"] == "countryId.value" and country["selected"] == "United States"
    toggle = show.page.locator(f'[data-rove-field="{country["ref"]}"]')
    assert toggle.get_attribute("aria-label") == "Country United States"
    # The upload's own name is "file-input"; the paragraph above it says what it is.
    resume = field(seen, "Resume", kind="file")
    assert resume["label"] == "Resume" and resume["required"] and resume_upload(seen)


# --- what a send looks like ----------------------------------------------------------


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_a_confirmed_send_needs_the_request_the_page_and_the_form_gone(show, board):
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    after = show(board, "confirmation")

    checks = adapter.confirmed(
        package_url, after, [response(board, send["accepted"])], before=before
    )
    assert checks["confirmed"] is True
    assert checks["confirmation_content"] and checks["form_gone"]
    assert not adapter.rejected(checks)

    # The confirmation page with no accepted request of the form's own is not proof:
    # Paylocity and JazzHR serve theirs to anyone who asks for the address.
    for responses in (
        [],
        [response(board, 500)],
        [{"host": "tracker.example", "path": send["post"][1], "status": 200}],
        [{"host": send["post"][0], "path": "/some/other/request", "status": 200}],
    ):
        unproven = adapter.confirmed(package_url, after, responses, before=before)
        assert unproven["confirmed"] is False, responses
        assert not adapter.rejected(unproven)


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_a_refused_form_is_not_confirmed_and_counts_as_not_sent(show, board):
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    after = show(board, "error")
    responses = [response(board, send["refused"])] if send["refused"] else []

    checks = adapter.confirmed(package_url, after, responses, before=before)
    assert checks["confirmed"] is False
    assert checks["confirmation_content"] is False and checks["form_gone"] is False
    assert adapter.rejected(checks)
    reason = adapter.reason(checks, after)
    assert adapter.label in reason
    assert not any(token in reason for token in ("_", "{", "http"))


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_a_bare_url_change_is_never_a_confirmation(show, board):
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    # The confirmation address with nothing on it, even after an accepted request.
    after = show(board, "confirmation", "<!doctype html><title>Careers</title><p>Loading</p>")

    checks = adapter.confirmed(
        package_url, after, [response(board, send["accepted"])], before=before
    )
    assert checks["confirmed"] is False
    assert checks["confirmation_content"] is False
    assert not adapter.rejected(checks)
    assert "no confirmation" in adapter.reason(checks, after)


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_an_unchanged_form_after_the_click_is_unknown_not_confirmed(show, board):
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    after = show.observe()

    for responses in ([], [response(board, send["accepted"])], [response(board, 503)]):
        checks = adapter.confirmed(package_url, after, responses, before=before)
        assert checks["confirmed"] is False
        assert not adapter.rejected(checks), responses


@pytest.mark.parametrize(
    "wording",
    [
        "Your application is incomplete",
        "Submission unsuccessful",
        "An error occurred",
        "Your application has not been received",
        "Your application has been received with errors. Please try again.",
        "We were unable to confirm that your application has been submitted successfully",
        "Your application was submitted successfully? We couldn't tell.",
    ],
)
@pytest.mark.parametrize("board", BOARD_NAMES)
def test_negative_wording_in_the_confirmation_spot_is_not_a_confirmation(show, board, wording):
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    show(board, "confirmation")
    target = {
        "paylocity": paylocity.SUCCESS_SELECTOR,
        "workable": workable.SUCCESS_SELECTOR + " h3",
        "jazzhr": jazzhr.SUCCESS_SELECTOR,
        "bamboohr": "p[data-fabric-component='BodyText']",
    }[board]
    show.page.locator(target).evaluate("(e, text) => { e.textContent = text; }", wording)
    after = show.observe()

    checks = adapter.confirmed(
        package_url, after, [response(board, send["accepted"])], before=before
    )
    assert checks["confirmation_content"] is False
    assert checks["confirmed"] is False


def test_confirmation_wording_must_be_said_and_not_taken_back():
    assert affirms("Your application has been received!", paylocity.SUCCESS_TEXT)
    assert affirms("Your application has been submitted successfully.", workable.SUCCESS_TEXT)
    assert affirms("Your application was submitted successfully", bamboohr.SUCCESS_TEXT)
    assert not affirms("", paylocity.SUCCESS_TEXT)
    assert not affirms(None, paylocity.SUCCESS_TEXT)
    assert not affirms("Thank you", paylocity.SUCCESS_TEXT)
    assert not affirms("Your application has not been received", paylocity.SUCCESS_TEXT)
    assert not affirms("Error: application has been received twice", paylocity.SUCCESS_TEXT)
    assert not affirms("Your application has been received but is incomplete", jazzhr.SUCCESS_TEXT)


def test_a_job_switch_on_the_way_to_the_result_is_not_a_confirmation(show):
    """A confirmation for some other job or employer does not confirm this package."""
    before = show("paylocity", "review")
    after = show("paylocity", "confirmation")
    other = f"{PAYLOCITY}/Apply/990002"
    assert not paylocity.PaylocityV1.confirmed(
        other,
        after,
        [
            {
                "host": "recruiting.paylocity.com",
                "path": "/Recruiting/Jobs/Apply/990002",
                "status": 200,
            }
        ],
        before=before,
    )["confirmed"]

    after = show("jazzhr", "confirmation")
    other = "https://oakfield.applytojob.com/apply/Zx9Kq2LmNp/Field-Technician"
    assert not jazzhr.JazzHRV1.confirmed(
        other,
        after,
        [{"host": "oakfield.applytojob.com", "path": "/apply/Zx9Kq2LmNp/x", "status": 302}],
    )["confirmed"]

    after = show("bamboohr", "confirmation")
    other = "https://tidewater.bamboohr.com/careers/18"
    assert not bamboohr.BambooHRV1.confirmed(
        other,
        after,
        [{"host": "tidewater.bamboohr.com", "path": "/careers/18/add", "status": 200}],
    )["confirmed"]


def test_a_refused_human_check_is_handed_to_the_owner(show):
    """Only the human check is missing: nothing was sent and the owner finishes it."""
    before = show("jazzhr", "apply")
    show("jazzhr", "apply")
    show.page.locator("#resumator-recaptcha-label").evaluate(
        "e => e.insertAdjacentHTML('afterend',"
        ' \'<div id="recaptcha-required-error" class="dv_error"><span>Please verify.</span></div>\')'
    )
    after = show.observe()
    url = PAGES["jazzhr"]["apply"]
    checks = jazzhr.JazzHRV1.confirmed(url, after, [], before=before)
    assert checks["captcha_rejected"] and jazzhr.JazzHRV1.rejected(checks)
    assert "human check" in jazzhr.JazzHRV1.reason(checks, after)
    # Marked questions as well are an ordinary refusal: the owner is asked to fix them.
    mixed = jazzhr.JazzHRV1.confirmed(url, show("jazzhr", "error"), [], before=before)
    assert mixed["rejected_form"] and not mixed["captcha_rejected"]

    # Workable answers 409 when its Turnstile check refuses the request.
    before = show("workable", "apply")
    after = show("workable", "error")
    checks = workable.WorkableV1.confirmed(
        PAGES["workable"]["apply"], after, [response("workable", 409)], before=before
    )
    assert checks["captcha_rejected"] and workable.WorkableV1.rejected(checks)
    assert not checks["confirmed"]


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_the_wait_ends_on_the_boards_own_signal(show, board):
    send = SENDS[board]
    adapter = send["adapter"]
    before = show(board, send["form"])

    # Nothing happened: the wait gives up at its bound instead of raising.
    started = time.monotonic()
    adapter.await_result(show.page, before, 400)
    assert 0.3 <= time.monotonic() - started < 5

    # The result is on the page: the wait returns at once.
    show(board, "confirmation")
    started = time.monotonic()
    adapter.await_result(show.page, before, 20000)
    assert time.monotonic() - started < 5

    # So does a refusal the form shows.
    show(board, "error")
    started = time.monotonic()
    adapter.await_result(show.page, before, 20000)
    assert time.monotonic() - started < 5


# The board's own client, reduced to what matters for reading a result: the final control
# posts the form's request and, when it is accepted, the result page is shown the way the
# board shows it (a new address, or the result drawn in place).
BOARD_CLIENT_JS = """({control, post, result, in_place}) => {
  document.querySelector(control).addEventListener('click', async event => {
    event.preventDefault();
    const reply = await fetch(post, {method: 'POST', body: '{}'});
    if (!reply.ok) return;
    if (!in_place) { location.href = result; return; }
    const html = await (await fetch(result)).text();
    document.body.innerHTML = new DOMParser().parseFromString(html, 'text/html').body.innerHTML;
  });
}"""


def click_through(show, board: str, status: int, timeout_ms: int) -> tuple[dict, dict]:
    """Press the final control the way the submission code does and read what follows."""
    send = SENDS[board]
    adapter = send["adapter"]
    package_url = PAGES[board][send["form"]]
    before = show(board, send["form"])
    confirmation = PAGES[board]["confirmation"]
    in_place = confirmation == package_url
    host, path = send["post"]
    result = f"https://{host}/fixture/confirmation" if in_place else confirmation
    show.served[result] = (FIXTURES / board / "confirmation.html").read_text()
    show.posts[path] = status
    module = boards.board_for(package_url)
    show.page.evaluate(
        BOARD_CLIENT_JS,
        {
            "control": module.FINAL_CONTROL["selector"],
            "post": f"https://{host}{path}",
            "result": result,
            "in_place": in_place,
        },
    )
    responses = []
    hosts = adapter.response_hosts(package_url)

    def observe_response(reply):  # the same record submission keeps: host, path, status
        url = urlsplit(reply.url)
        if reply.request.method == "POST" and submission.host_matches(url.hostname, hosts):
            responses.append({"host": url.hostname, "path": url.path, "status": reply.status})

    show.page.on("response", observe_response)
    show.page.locator(module.FINAL_CONTROL["selector"]).click()
    adapter.await_result(show.page, before, timeout_ms)
    show.page.wait_for_load_state("domcontentloaded")
    after = show.observe()
    show.page.remove_listener("response", observe_response)
    return adapter.confirmed(package_url, after, responses, before=before), after


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_a_click_is_followed_to_the_boards_result(show, board):
    started = time.monotonic()
    checks, after = click_through(show, board, 200, 20000)
    assert time.monotonic() - started < 10
    assert checks["confirmed"] is True, checks
    assert checks["post_accepted"] and not checks["post_rejected"]
    assert job_scope(after["url"]) == job_scope(PAGES[board][SENDS[board]["form"]])


@pytest.mark.parametrize("board", BOARD_NAMES)
def test_a_click_the_server_fails_stays_unknown(show, board):
    adapter = SENDS[board]["adapter"]
    checks, after = click_through(show, board, 503, 700)
    assert checks["confirmed"] is False
    assert checks["post_rejected"] and not checks["post_accepted"]
    # A server failure says nothing about what was stored: never "not sent".
    assert not adapter.rejected(checks)
    assert "answered the send with an error" in adapter.reason(checks, after)


# --- switching the adapters on -------------------------------------------------------


def test_board_adapters_are_used_only_when_the_owner_lists_them(monkeypatch):
    names = {"paylocity_v1", "workable_v1", "jazzhr_v1", "bamboohr_v1"}
    assert set(boards.ADAPTERS) == names
    assert not names & set(submission.ADAPTERS)
    urls = {board: PAGES[board][SENDS[board]["form"]] for board in BOARD_NAMES}

    def listed(*adapters):
        monkeypatch.setattr(
            submission.workflow,
            "config",
            lambda: {"submission_enabled": True, "submit_adapters": list(adapters)},
        )

    listed("greenhouse_v1", "lever_v1")
    for url in urls.values():
        with pytest.raises(PermissionError):
            submission.enabled_adapter(url)

    # The catch-all keeps working for a board whose own adapter is not listed.
    listed("greenhouse_v1", "lever_v1", "generic_v1")
    for url in urls.values():
        assert submission.enabled_adapter(url) is submission.GenericV1

    listed("paylocity_v1", "workable_v1", "jazzhr_v1", "bamboohr_v1", "generic_v1")
    for board, url in urls.items():
        assert submission.enabled_adapter(url) is SENDS[board]["adapter"]
    assert (
        submission.enabled_adapter("https://careers.example.com/jobs/12345/apply")
        is submission.GenericV1
    )

    listed("jazzhr_v1")
    assert submission.enabled_adapter(urls["jazzhr"]) is jazzhr.JazzHRV1
    with pytest.raises(PermissionError):
        submission.enabled_adapter(urls["paylocity"])

    monkeypatch.setattr(
        submission.workflow,
        "config",
        lambda: {"submission_enabled": False, "submit_adapters": sorted(names)},
    )
    with pytest.raises(PermissionError):
        submission.enabled_adapter(urls["paylocity"])
