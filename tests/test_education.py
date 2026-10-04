"""Two schools, one stated: the school applications state, and the one attended now.

A synthetic owner is enrolled at a community college today and transfers to a university
whose degree his applications state. Dates are relative to today, so the college is
always the one attended now and the university always starts later.
"""

from datetime import UTC, datetime

import pytest
import test_form_reading
import test_workflow
from pydantic import ValidationError
from test_intake import PREFERENCES, W, job

from rove import (
    common_questions,
    education,
    fastpath,
    intake,
    memory_channel,
    questions,
    reasoning,
    workflow,
)
from rove.onboarding import Candidate, approve, digest, draft, propose, read_approved
from rove.questions import resolve

state = test_workflow.state
reader = test_form_reading.reader

NOW = datetime.now(UTC)
Y = NOW.year
COLLEGE = {
    "school": "Lakeside Community College",
    "degree": "Associate of Arts",
    "major": "General Studies",
    "start_month": f"{Y - 1}-08",
    "graduation_month": f"{Y + 1}-12",
    "currently_enrolled": True,
    "gpa": 3.9,
    "gpa_scale": 4.0,
    "disclose_gpa": True,
}
UNIVERSITY = {
    "school": "Example State University",
    "degree": "Bachelor of Science",
    "major": "Computer Science",
    "start_month": f"{Y + 2}-01",
    "graduation_month": f"{Y + 3}-05",
    "currently_enrolled": False,
    "apply_as": True,
}
PROFILE = {
    "identity": {"legal_first_name": "Alex", "legal_last_name": "Example"},
    "education": {"schools": [COLLEGE, UNIVERSITY]},
    "eligibility": {},
    "preferences": {"relocate": None, "work_styles": []},
    "availability": {},
}
UNMARKED = {
    **PROFILE,
    "education": {"schools": [COLLEGE, {k: v for k, v in UNIVERSITY.items() if k != "apply_as"}]},
}
DEGREES = ["Associate's Degree", "Bachelor's Degree", "Master's Degree", "Other"]
DISCIPLINES = ["Computer Science", "General Studies", "Mathematics", "Other"]
MONTHS = [datetime(2000, n, 1, tzinfo=UTC).strftime("%B") for n in range(1, 13)]


def field(label, options=(), kind="text", **extra) -> dict:
    return {"label": label, "kind": kind, "options": [{"label": o} for o in options], **extra}


def answer(label, options=(), kind="text", profile=PROFILE, **extra):
    return resolve(field(label, options, kind, **extra), profile)


# --- the rule, in one place ----------------------------------------------------------
def test_the_stated_school_is_the_marked_one_else_the_only_one():
    assert education.primary(PROFILE) is UNIVERSITY and education.primary_index(PROFILE) == 1
    assert [i for i, _ in education.ordered(PROFILE)] == [1, 0]  # stated first, then the rest
    assert education.graduation(PROFILE) == f"{Y + 3}-05"
    # Where he is enrolled today follows the dates, not the mark.
    assert education.current(PROFILE) is COLLEGE
    # Several schools and no mark: none is stated, nothing is guessed.
    assert education.primary(UNMARKED) is None and education.ordered(UNMARKED) == []
    assert education.graduation(UNMARKED) is None
    assert education.current(UNMARKED) is COLLEGE
    # One school is the stated one without a mark.
    assert education.primary({"education": {"schools": [COLLEGE]}}) is COLLEGE
    # Two entries that could both be today's: enrollment is not guessed either.
    undated = {"education": {"schools": [{"school": "A"}, {"school": "B"}]}}
    assert education.current(undated) is None


def test_apply_as_is_optional_and_left_out_of_a_profile_that_lacks_it(state):
    approved_before = Candidate.model_validate({"education": {"schools": [COLLEGE]}}).model_dump()
    assert "apply_as" not in approved_before["education"]["schools"][0]
    marked = Candidate.model_validate({"education": {"schools": [COLLEGE, UNIVERSITY]}})
    assert [s.get("apply_as") for s in marked.model_dump()["education"]["schools"]] == [None, True]
    with pytest.raises(ValidationError, match="Only one school"):
        Candidate.model_validate(
            {"education": {"schools": [{**COLLEGE, "apply_as": True}, UNIVERSITY]}}
        )
    # Approved through the profile flow, it reads back with the same hash.
    propose("education", {"schools": [COLLEGE, UNIVERSITY]}, digest(draft()))
    approve(digest(draft()))
    approved = read_approved()
    assert education.primary(approved["profile"])["school"] == "Example State University"
    assert approved["profile_hash"] == digest(approved["profile"])


# --- the resolver ----------------------------------------------------------------------
def test_application_questions_state_the_university_and_enrollment_questions_the_college():
    assert answer("School") == ("Example State University", "education.schools.1.school")
    assert answer("Degree", DEGREES, "select-one")[0] == "Bachelor's Degree"
    assert answer("Discipline", DISCIPLINES, "select-one")[0] == "Computer Science"
    assert answer("Expected graduation date") == (
        f"May {Y + 3}",
        "education.schools.1.graduation_month",
    )
    assert answer("Graduation year")[0] == str(Y + 3)
    # Where he is enrolled now is the college, whatever applications state.
    for label in ("Current school", "Which school are you attending this semester?"):
        assert answer(label) == ("Lakeside Community College", "education.schools.0.school")
    assert answer("Enrollment status", ["Currently enrolled", "Not enrolled"])[0] == (
        "Currently enrolled"
    )
    assert answer("Are you currently enrolled?", ["Yes", "No"])[0] == "Yes"
    at = "Are you currently enrolled at {}?"
    assert answer(at.format("Lakeside Community College"), ["Yes", "No"])[0] == "Yes"
    assert answer(at.format("Example State University"), ["Yes", "No"])[0] == "No"
    assert answer(at.format("Some Other College"), ["Yes", "No"]) == (None, None)
    # A program kind the answer hangs on is never a yes for whatever school he attends.
    bachelors = "Are you currently enrolled in a bachelor's degree program?"
    assert answer(bachelors, ["Yes", "No"]) == (None, None)
    assert questions.draft_gate({"label": bachelors, "options": ["Yes", "No"]})["code"] == (
        "profile_fact:enrollment_status"
    )
    # The stated degree begins later: its start is given as it is, never moved earlier.
    assert answer("Degree start date") == (f"January {Y + 2}", "education.schools.1.start_month")
    # His start at the college is the warm-up's question.
    assert answer(questions.SCHOOL_START_LABEL)[0] == f"August {Y - 1}"


def test_without_a_stated_school_only_today_is_answered():
    assert answer("School", profile=UNMARKED) == (None, None)
    assert answer("Degree", DEGREES, profile=UNMARKED) == (None, None)
    assert answer("Expected graduation date", profile=UNMARKED) == (None, None)
    assert answer("Current school", profile=UNMARKED)[0] == "Lakeside Community College"


def test_a_gpa_belongs_to_the_school_it_was_earned_at():
    # Asked plainly, the GPA is the one he discloses: the college's.
    for label in ("GPA", "Cumulative GPA", "GPA (Undergraduate)"):
        assert answer(label) == ("3.9", "education.schools.0.gpa"), label
    # Inside an education block, a GPA is that block's school's: none for the university.
    blocks = [
        field("School", id="school-a"),
        field("GPA", id="gpa-a"),
        field("School", id="school-b"),
        field("GPA", id="gpa-b"),
    ]
    questions.number_education_blocks(blocks)
    assert [f["education_block"] for f in blocks] == [0, 0, 1, 1]
    assert resolve(blocks[1], PROFILE) == (None, None)
    assert resolve(blocks[3], PROFILE) == ("3.9", "education.schools.0.gpa")
    # Two schools disclosing a GPA: a plain question cannot say whose.
    both = {
        **PROFILE,
        "education": {
            "schools": [
                COLLEGE,
                {**UNIVERSITY, "gpa": 3.5, "gpa_scale": 4.0, "disclose_gpa": True},
            ]
        },
    }
    assert answer("Cumulative GPA", profile=both) == (None, None)


def test_class_standing_comes_from_the_stated_graduation():
    # The owner's case: graduating May 2029 makes a rising junior in summer 2027.
    assert education.rising("2029-05", 2027) == "junior"
    owner = {"education": {"schools": [{**UNIVERSITY, "graduation_month": "2029-05"}]}}
    assert education.class_years(owner, 2027) == {"sophomore", "junior"}
    autumn = datetime(2026, 10, 3, tzinfo=UTC)
    assert education.year_in_school(owner, autumn) == "sophomore"
    assert education.year_in_school(owner, datetime(2028, 10, 3, tzinfo=UTC)) == "senior"
    # The college's earlier graduation never decides it.
    assert education.class_years(PROFILE, Y + 1) == {"sophomore", "junior"}
    years = ["Freshman", "Sophomore", "Junior", "Senior"]
    now = education.year_in_school(PROFILE)
    assert answer("What is your current year in school?", years, "select-one")[0] == now.title()
    rising = ["Rising Sophomore", "Rising Junior", "Rising Senior"]
    coming = education.rising(UNIVERSITY["graduation_month"], education.internship_year(""))
    assert answer("Class standing", rising, "select-one")[0] == f"Rising {coming.title()}"


def test_education_facts_today_are_never_drafted():
    for label in ("Current school", "Enrollment status", "What is your current year in school?"):
        gate = questions.draft_gate({"label": label, "options": []})
        assert gate and gate["code"].startswith("profile_fact:"), label
        assert fastpath.question_kind({"key": "k", "label": label}) == "owner"


# --- the fit review and intake ---------------------------------------------------------
def window(start: str, end: str) -> dict:
    words = [
        datetime.strptime(m, "%Y-%m").replace(tzinfo=UTC).strftime("%B %Y") for m in (start, end)
    ]
    return {
        "kind": "graduation_window",
        "requirement": f"Expected graduation between {words[0]} and {words[1]}",
        "graduation_start": start,
        "graduation_end": end,
        "status": "unknown",
    }


def test_the_fit_review_goes_by_the_stated_graduation_and_degree():
    posting = f"Summer {Y + 1} software engineering internship"
    checked = reasoning.evaluate_requirements(
        [
            window(f"{Y + 3}-01", f"{Y + 3}-12"),
            window(f"{Y + 1}-01", f"{Y + 1}-12"),  # the college's year: not his graduation
            {"kind": "other", "requirement": "Rising juniors only", "status": "unknown"},
            {
                "kind": "degree",
                "requirement": "Pursuing a degree in computer science or a related field",
                "status": "unknown",
            },
        ],
        PROFILE,
        posting,
    )
    assert [c["status"] for c in checked] == ["satisfied", "conflict", "satisfied", "satisfied"]
    assert all(c["checked_by"] == "code" for c in checked)
    # Applying as the college instead, the degree is not a computing one.
    as_college = {
        **PROFILE,
        "education": {
            "schools": [{**COLLEGE, "apply_as": True}, UNMARKED["education"]["schools"][1]]
        },
    }
    degree = reasoning.evaluate_requirements([checked[3] | {"status": "unknown"}], as_college)
    assert degree[0]["checked_by"] == "qwen"
    assert reasoning.internship_year("Summer 2027 internship") == 2027


def test_intake_scores_class_and_graduation_from_the_stated_school():
    profile = {"preferences": PREFERENCES, "education": {"schools": [COLLEGE, UNIVERSITY]}}
    today = NOW.date()

    def score(record):
        return intake.score_job(record, profile, today=today)

    base = score(job("base"))["score"]
    juniors = score(job("juniors", "Software Engineer Intern - Rising Juniors"))
    assert juniors["score"] == base + W["class_fits"]
    sophomores = score(job("sophomores", "Sophomore Software Engineer Intern"))
    assert sophomores["score"] == base + W["class_fits"]
    seniors = score(job("seniors", "Software Engineer Intern - Rising Seniors"))
    assert "meant for senior students" in seniors["reason"]
    stated = {"requirement_level": "required", "graduation_start": f"{Y + 3}-01"}
    fits = score(job("fits", academic_eligibility={**stated, "graduation_end": f"{Y + 3}-12"}))
    assert fits["score"] == base + W["class_fits"]
    earlier = {"requirement_level": "required", "graduation_start": f"{Y + 1}-01"}
    missed = score(job("missed", academic_eligibility={**earlier, "graduation_end": f"{Y + 1}-12"}))
    assert "different graduation date" in missed["reason"]
    assert intake.graduation_month(profile) == f"{Y + 3}-05"


# --- #memory and the warm-up -------------------------------------------------------------
def test_the_memory_profile_section_names_both_schools():
    lines = memory_channel.school_lines(PROFILE)
    stated = "Example State University · Bachelor of Science in Computer Science"
    now = "Lakeside Community College · Associate of Arts in General Studies"
    assert lines == [
        f"School on applications: {stated} · graduating May {Y + 3}",
        f"Enrolled now: {now} · graduating December {Y + 1}",
    ]
    facts = {name: read for name, _pattern, read in memory_channel.PROFILE_FACTS}
    assert facts["School"](PROFILE) == "Example State University"
    assert facts["Graduation"](PROFILE) == f"May {Y + 3}"
    single = {**PROFILE, "education": {"schools": [COLLEGE]}}
    assert memory_channel.school_lines(single)[0].startswith("School: Lakeside Community College")


def test_the_warm_up_asks_when_he_started_where_he_is_enrolled_now(state):
    entry = common_questions.BY_ID["school_start"]
    assert common_questions.applies(entry, PROFILE)
    assert entry.id not in {e.id for e in common_questions.open_questions(PROFILE, None)}
    undated = {**PROFILE, "education": {"schools": [{**COLLEGE, "start_month": None}, UNIVERSITY]}}
    assert entry.id in {e.id for e in common_questions.open_questions(undated, None)}
    assert workflow.remember_answer(entry.label, [], f"August {Y - 1}", "memory:m1")
    # It fills the college's block, never the university's.
    college_year = field("Start date year", kind="number", id="start-year--1")
    assert resolve(college_year, undated, recall=workflow.recall_answer) == (
        str(Y - 1),
        questions.REMEMBERED,
    )
    university_year = field("Start date year", kind="number", id="start-year--0")
    assert resolve(university_year, undated, recall=workflow.recall_answer)[0] == str(Y + 2)


# --- whole forms -------------------------------------------------------------------------
def select(key, label, options, required=True, attributes=True):
    listed = "".join(f"<option>{o}</option>" for o in options)
    star = " required" if required else ""
    if not attributes:
        return f"<label>{label}<select{star}><option value=''>Select...</option>{listed}</select></label>"
    return (
        f'<label for="{key}">{label}</label><select id="{key}" name="{key}"{star}>'
        f'<option value="">Select...</option>{listed}</select>'
    )


def text_input(key, label, kind="text", required=True, attributes=True):
    star = " required" if required else ""
    if not attributes:
        return f'<label>{label}<input type="{kind}"{star}></label>'
    return f'<label for="{key}">{label}</label><input id="{key}" name="{key}" type="{kind}"{star}>'


def greenhouse_block(n: int) -> str:
    return (
        text_input(f"school--{n}", "School")
        + select(f"degree--{n}", "Degree", DEGREES)
        + select(f"discipline--{n}", "Discipline", DISCIPLINES)
        + select(f"start-month--{n}", "Start date month", MONTHS)
        + text_input(f"start-year--{n}", "Start date year", "number")
        + select(f"end-month--{n}", "End date month", MONTHS)
        + text_input(f"end-year--{n}", "End date year", "number")
    )


TWO_BLOCKS = (
    '<title>Apply</title><form id="application-form">'
    + greenhouse_block(0)
    + greenhouse_block(1)
    + '<button type="submit">Submit application</button></form>'
)


def test_a_greenhouse_form_states_the_university_first_and_the_college_second(
    reader, state, monkeypatch
):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False, "human_pacing": False})
    reader.run["profile_hash"] = "x"
    seen = reader.read(TWO_BLOCKS)
    approved = {"profile": PROFILE, "profile_hash": "x"}
    filled, pending = reader._fill_page("abcdef012345", seen, approved, {})
    assert pending == []
    got = {}
    for entry in filled:
        got.setdefault(entry["label"], []).append(entry["value"])
    assert got == {
        "School": ["Example State University", "Lakeside Community College"],
        "Degree": ["Bachelor's Degree", "Associate's Degree"],
        "Discipline": ["Computer Science", "General Studies"],
        "Start date month": ["January", "August"],
        "Start date year": [str(Y + 2), str(Y - 1)],
        "End date month": ["May", "December"],
        "End date year": [str(Y + 3), str(Y + 1)],
    }
    page = reader.page
    assert page.locator("#school--0").input_value() == "Example State University"
    assert page.locator("#degree--1 option:checked").inner_text() == "Associate's Degree"
    assert page.locator("#start-year--0").input_value() == str(Y + 2)  # ahead, as it is


REPEATED_BLOCKS = (
    '<title>Apply</title><form id="application-form">'
    + "".join(
        text_input("", "School or University", attributes=False)
        + select("", "Degree", DEGREES, attributes=False)
        + text_input("", "Field of Study", attributes=False)
        + text_input("", "GPA", required=False, attributes=False)
        for _ in range(2)
    )
    + '<button type="submit">Submit application</button></form>'
)


def test_repeated_blocks_with_the_same_words_get_one_school_each(reader, state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False, "human_pacing": False})
    reader.run["profile_hash"] = "x"
    seen = reader.read(REPEATED_BLOCKS)
    assert [f.get("occurrence") for f in seen["fields"]][4:] == [1, 1, 1, 1]
    approved = {"profile": PROFILE, "profile_hash": "x"}
    filled, pending = reader._fill_page("abcdef012345", seen, approved, {})
    got = {}
    for entry in filled:
        got.setdefault(entry["label"], []).append(entry["value"])
    assert got["School or University"] == ["Example State University", "Lakeside Community College"]
    assert got["Degree"] == ["Bachelor's Degree", "Associate's Degree"]
    assert got["Field of Study"] == ["Computer Science", "General Studies"]
    # The college's GPA goes in the college's block only.
    assert got["GPA"] == ["3.9"]
    boxes = reader.page.locator("input[type=text]")
    assert [boxes.nth(i).input_value() for i in (2, 5)] == ["", "3.9"]
    assert [q["label"] for q in pending] == ["GPA"]
