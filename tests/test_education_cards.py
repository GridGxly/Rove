"""Browser, server and database checks for imported education card repair."""

import json
from pathlib import Path

import pytest
import test_frames

from rove import profile_cards, questions, submission, worker, workflow
from rove.onboarding import approve, digest, draft, propose

sites = test_frames.sites
FORM = (Path(__file__).parent / "fixtures/oracle_education.html").read_text()
ENTRY = {
    "school": "Example College",
    "degree": "Bachelor of Applied Science",
    "major": "Systems Engineering",
}


def profile():
    propose(
        "education",
        {
            "schools": [
                ENTRY,
                {
                    "school": "Example University",
                    "degree": "Bachelor of Science",
                    "major": "Computer Science",
                    "apply_as": True,
                    "currently_enrolled": False,
                    "graduation_month": "2029-05",
                },
            ]
        },
        digest(draft()),
    )
    propose("eligibility", {"us_work_authorized": True}, digest(draft()))
    approve(digest(draft()))


def server(monkeypatch, *, reject=False, entry=ENTRY):
    received = []

    def post(handler):
        payload = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        received.append((handler.path, payload))
        valid = (
            payload == entry
            if handler.path == "/education/save"
            else payload
            == {
                "education": entry,
                "authorization": "Yes",
            }
        )
        handler.send_response(200 if valid and not reject else 422)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(b'{"ok":true}' if valid and not reject else b'{"ok":false}')

    monkeypatch.setattr(test_frames.Employer, "do_POST", post)
    return received


def test_education_card_uses_its_exact_school_then_submits_one_synthetic_application(
    sites, monkeypatch
):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch)
    test_frames.Employer.pages["/cards/jobs/12"] = FORM
    application = runtime.open(f"{employer}/cards/jobs/12")["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "READY_FOR_REVIEW", page["pending"]
    assert received == [("/education/save", ENTRY)]
    saved = json.loads((state / "applications" / application / "education-0.json").read_text())
    assert saved["saved"] and saved["requests"][0]["status"] == 200
    facts = {f["label"]: (f["value"], f["source"]) for f in page["filled"] if f.get("value")}
    assert facts["Degree"] == (ENTRY["degree"], "education.schools.0.degree")
    assert facts["Major"] == (ENTRY["major"], "education.schools.0.major")
    test_frames.approve_send(application, page["package_hash"])
    result = submission.submit(runtime, application, page["package_hash"], "msg-1")
    assert result["status"] == "APPLIED"
    assert received == [
        ("/education/save", ENTRY),
        (
            "/cards/jobs/12",
            {
                "education": ENTRY,
                "authorization": "Yes",
            },
        ),
    ]
    with workflow.db() as conn:
        assert (
            conn.execute(
                "SELECT status FROM application_queue WHERE id=?", (application,)
            ).fetchone()[0]
            == "APPLIED"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM live_submission_attempts WHERE application_id=?",
                (application,),
            ).fetchone()[0]
            == 1
        )


def test_unknown_import_is_preserved_and_blocks_submission(sites, monkeypatch):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch)
    test_frames.Employer.pages["/cards/jobs/13"] = FORM.replace("Example College", "Honors Program")
    application = runtime.open(f"{employer}/cards/jobs/13")["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "NEEDS_USER"
    assert any(q["label"] == "Education card: Honors Program" for q in page["pending"])
    assert runtime.page.locator('[aria-label="Delete"]').count() == 1
    assert not received


def test_owner_answer_resumes_the_same_open_school_editor(sites, monkeypatch):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch, entry={**ENTRY, "startYear": "2023"})
    test_frames.Employer.pages["/cards/jobs/17"] = FORM.replace(
        '<label>Major<input name="major" required></label>',
        '<label>Major<input name="major" required></label>'
        '<label>Education start year<input name="startYear" required></label>',
    )
    application = runtime.open(f"{employer}/cards/jobs/17")["run_id"]
    test_frames.freeze_resume(state, application)
    paused = runtime.prepare(application)
    assert paused["status"] == "NEEDS_USER"
    assert not received
    question = next(q for q in paused["pending"] if q["label"] == "Education start year")
    worker.apply_command(
        {
            "kind": "answer",
            "application_id": application,
            "field_key": question["key"],
            "value": "2023",
        },
        "synthetic-owner-message",
    )
    ready = runtime.prepare(application)
    assert ready["status"] == "READY_FOR_REVIEW", ready["pending"]
    assert received == [("/education/save", {**ENTRY, "startYear": "2023"})]
    assert "education_editor" not in runtime.run


def test_an_editor_for_a_different_school_is_not_filled_or_saved(sites, monkeypatch):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch)
    test_frames.Employer.pages["/cards/jobs/16"] = FORM.replace(
        'name="school" value="Example College"', 'name="school" value="Example University"'
    )
    application = runtime.open(f"{employer}/cards/jobs/16")["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "NEEDS_USER"
    assert not received
    assert runtime.page.locator('[name="degree"]').input_value() == ""


def test_site_rejecting_education_save_never_reaches_ready(sites, monkeypatch):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch, reject=True)
    test_frames.Employer.pages["/cards/jobs/14"] = FORM
    application = runtime.open(f"{employer}/cards/jobs/14")["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "NEEDS_USER"
    assert received == [("/education/save", ENTRY)]
    assert not (state / "applications" / application / "education-0.json").exists()


@pytest.mark.parametrize("index", [0, 1, 99])
def test_card_field_never_uses_the_primary_school_or_a_remembered_answer(index):
    schools = [
        ENTRY,
        {"school": "Example University", "degree": "Bachelor of Science", "apply_as": True},
    ]
    value, source = questions.resolve(
        {"label": "Degree", "education_profile_index": index},
        {"education": {"schools": schools}},
        recall=lambda *a, **kw: "Remembered wrong school",
    )
    assert value == (schools[index]["degree"] if index < 2 else None)
    assert source == (f"education.schools.{index}.degree" if index < 2 else None)


def test_duplicate_profile_schools_cannot_supply_a_card_fact():
    assert (
        profile_cards.school_index(
            {"school": ENTRY["school"]},
            {
                "education": {"schools": [ENTRY, ENTRY]},
            },
        )
        is None
    )


def test_a_changed_saved_card_is_refused_before_any_submit_click(sites, monkeypatch):
    runtime, employer, _board, state = sites
    profile()
    received = server(monkeypatch)
    test_frames.Employer.pages["/cards/jobs/15"] = FORM
    application = runtime.open(f"{employer}/cards/jobs/15")["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "READY_FOR_REVIEW"
    runtime.page.locator("[class*=summary-title]").evaluate("e=>e.textContent='Wrong major'")
    with pytest.raises(PermissionError, match="profile cards"):
        submission.preflight(application, page["package_hash"], runtime.observe())
    assert received == [("/education/save", ENTRY)]


def test_school_dates_and_enrollment_follow_the_card_not_the_application_default():
    owner = {
        "education": {
            "schools": [
                {**ENTRY, "currently_enrolled": True, "graduation_month": "2097-12"},
                {
                    "school": "Example University",
                    "apply_as": True,
                    "currently_enrolled": False,
                    "graduation_month": "2099-05",
                },
            ]
        }
    }
    assert questions.resolve(
        {"label": "Graduation date year", "education_profile_index": 0}, owner
    ) == (
        "2097",
        "education.schools.0.graduation_month",
    )
    for label, value in (("Graduated", "No"), ("Most recent or current education", "Yes")):
        assert questions.resolve({"label": label, "education_profile_index": 0}, owner)[0] == value
        assert questions.resolve({"label": label, "education_profile_index": 1}, owner) == (
            None,
            None,
        )
