"""What a live Ashby pass got wrong, replayed with its own labels and option lists.

The owner and the employer are synthetic. The owner attends a community college now and
applies as a university student (the entry marked `apply_as`, no start month), as the
real profile does. Dates are relative to today.
"""

from datetime import UTC, datetime

import pytest
import test_form_reading
import test_workflow
from test_security_inbound import KEYS, drafting
from test_security_inbound import proposal as drafted

from rove import common_questions, draft_guard, fastpath, questions, reasoning, workflow
from rove.questions import classify, resolve

state = test_workflow.state
reader = test_form_reading.reader

Y = datetime.now(UTC).year
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
    "graduation_month": f"{Y + 3}-05",
    "apply_as": True,
}
OWNER = {
    "identity": {
        "legal_first_name": "Alex",
        "legal_last_name": "Example",
        "city": "Springfield",
        "state_region": "Illinois",
        "country": "United States",
    },
    "education": {"schools": [COLLEGE, UNIVERSITY]},
    "eligibility": {"us_work_authorized": True, "us_citizen": None},
    "preferences": {"relocate": True, "work_styles": [], "excluded_locations": ["Seattle"]},
    "availability": {},
}
YES_NO = ["Yes", "No"]
ACME = "ashbyhq.com/acme"


def field(label, options=(), kind="text", **extra) -> dict:
    return {"label": label, "kind": kind, "options": [{"label": o} for o in options], **extra}


def answer(label, options=(), kind="text", profile=OWNER, **kwargs):
    return resolve(field(label, options, kind), profile, **kwargs)


def owner_only(label, options=()) -> bool:
    gate = questions.draft_gate({"label": label, "options": list(options)})
    return bool(gate) and fastpath.question_kind({"key": "k", "label": label}) == "owner"


# --- 1. education through the stated entry, never the model -------------------------
GRADUATION_MONTHS = ["April/May/June", "August/September", "December"]
DEGREE_LEVELS = ["Associate's", "Bachelor's", "Master's", "PhD", "Other"]


def test_the_school_typeahead_gets_the_stated_school():
    for label in ("School", "University", "College or University"):
        picker = {**field(label), "role": "combobox"}
        assert resolve(picker, OWNER, picker=True) == (
            "Example State University",
            "education.schools.1.school",
        ), label
        assert owner_only(label), label  # never a draft: the community college was one


def test_degree_level_currently_pursuing_is_the_stated_level():
    for label in ("Degree Level Currently Pursuing", "Degree level", "Current degree"):
        assert answer(label, DEGREE_LEVELS, "select-one") == (
            "Bachelor's",
            "education.schools.1.degree",
        ), label
        assert owner_only(label, DEGREE_LEVELS), label


def test_the_graduation_month_and_year_come_from_one_entry():
    month = answer("Expected Graduation Month", GRADUATION_MONTHS, "select-one")
    year = answer("Expected Graduation Year", kind="number")
    assert month == ("April/May/June", "education.schools.1.graduation_month")
    assert year == (str(Y + 3), "education.schools.1.graduation_month")
    assert answer("Expected graduation date") == (
        f"May {Y + 3}",
        "education.schools.1.graduation_month",
    )
    # December is the college's month: it is never offered for the stated graduation.
    assert answer("Expected Graduation Month", ["December", "May"]) == (
        "May",
        "education.schools.1.graduation_month",
    )
    for label in ("Expected Graduation Month", "Expected Graduation Year"):
        assert owner_only(label, GRADUATION_MONTHS), label
    assert questions.option_months("April/May/June") == {4, 5, 6}
    assert questions.option_months("May - August") == {5, 6, 7, 8}
    assert questions.option_months("November to February") == {11, 12, 1, 2}


def test_the_drafting_context_states_only_the_stated_school():
    education = draft_guard.drafting_profile(
        {
            **OWNER,
            "identity": {
                k: OWNER["identity"].get(k) for k in draft_guard.DRAFTING_FIELDS["identity"]
            },
            "availability": {},
            "preferences": {},
            "stories": {},
        }
    )["education"]
    assert "schools" not in education
    assert education["state_on_applications"]["school"] == "Example State University"
    assert "gpa" not in education["state_on_applications"]
    now = education["enrolled_now_not_the_degree_to_state"]
    assert now["school"] == "Lakeside Community College" and now["gpa"] == 3.9


# --- 2. picking a school in a typeahead ----------------------------------------------
def test_a_school_suggestion_is_the_name_or_the_name_with_a_campus():
    ucf = "University of Central Florida"
    listed = [
        "Central Florida Christian Academy",
        "University of Central Florida - Orlando, FL",
        "University of Central Florida (UCF)",
    ]
    # Two suggestions are the school; the owner's city decides, never the list's order.
    assert questions.school_option(listed, ucf, ["Orlando", "Florida"]) == 1
    assert questions.school_option(listed, ucf, []) is None
    assert questions.school_option([*listed, ucf], ucf) == 3  # the exact name first
    assert questions.school_option(listed[1:2], ucf) == 0
    assert questions.school_option(["Florida State University", "Rollins College"], ucf) is None
    assert questions.other_option(["Rollins College", "Other"]) == 1
    assert questions.other_option(["Rollins College"]) is None


TYPEAHEAD = """<title>Apply</title><form>
<label for="s">School</label>
<input id="s" role="combobox" aria-autocomplete="list" aria-controls="list" required>
<ul id="list" role="listbox"></ul>
<script>
const schools = __SCHOOLS__;
const input = document.getElementById('s'), list = document.getElementById('list');
input.addEventListener('input', () => {
  list.innerHTML = '';
  const typed = input.value.toLowerCase();
  if (!typed) return;
  for (const name of schools.filter(s => s.toLowerCase().includes(typed) || s === 'Other')) {
    const li = document.createElement('li');
    li.setAttribute('role', 'option'); li.textContent = name;
    li.addEventListener('click', () => { input.value = name; list.innerHTML = ''; });
    list.appendChild(li);
  }
});
</script></form>"""


def typeahead(schools: list[str]) -> str:
    import json

    return TYPEAHEAD.replace("__SCHOOLS__", json.dumps(schools))


def fill(reader, html: str, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False, "human_pacing": False})
    reader.run["profile_hash"] = "x"
    seen = reader.read(html)
    return reader._fill_page("abcdef012345", seen, {"profile": OWNER, "profile_hash": "x"}, {})


def test_the_school_typeahead_types_the_name_and_takes_the_campus_near_home(
    reader, state, monkeypatch
):
    campuses = [
        "Example State University - Riverside, CA",
        "Example State University - Springfield, IL",
        "Example Statewide College",
    ]
    filled, pending = fill(reader, typeahead(campuses), monkeypatch)
    assert pending == []
    assert filled[0]["value"] == "Example State University"
    assert reader.page.locator("#s").input_value() == "Example State University - Springfield, IL"


def test_a_school_not_listed_takes_other_or_waits_with_a_reason(reader, state, monkeypatch):
    filled, pending = fill(reader, typeahead(["Rollins College", "Other"]), monkeypatch)
    assert pending == [] and filled[0]["value"] == "Other"
    assert reader.page.locator("#s").input_value() == "Other"
    filled, pending = fill(reader, typeahead(["Rollins College"]), monkeypatch)
    assert filled == [] and pending[0]["label"] == "School"
    assert "is not in this form's list of schools" in pending[0]["reason"]
    assert reader.page.locator("#s").input_value() == ""


# --- 3. a willingness question behind a statement --------------------------------------
ONSITE = (
    "We require all employees onsite, in-person. Are you able to accommodate an onsite "
    "full-time work schedule?"
)


def test_a_willingness_question_behind_a_statement_takes_the_standing_default():
    assert answer(ONSITE, YES_NO, "select-one") == ("Yes", "policy.default.onsite_willing")
    assert classify(ONSITE).canonical_id == "onsite_willing"
    onsite = {**OWNER, "preferences": {**OWNER["preferences"], "work_styles": ["onsite"]}}
    assert answer(ONSITE, YES_NO, profile=onsite) == ("Yes", "preferences.work_styles")
    for label in (
        "This internship is unpaid. Are you willing to work onsite?",  # money
        "Our office is in Seattle. Are you willing to work onsite in our office?",  # excluded
        "We require all employees onsite. Are you unable to work onsite?",  # negated
        "We value candor. Are you willing to sign a non-disclosure agreement?",  # legal
    ):
        assert answer(label, YES_NO) == (None, None), label
    remote = {**OWNER, "preferences": {**OWNER["preferences"], "work_styles": ["remote"]}}
    assert answer(ONSITE, YES_NO, profile=remote) == (None, None)


# --- 4. follow-ups that hang on a yes ------------------------------------------------
RELATED = "Are you related to any current Acme employees?"
EXPLAIN = "If yes, please provide the employee's name and your relationship"


def page(*fields):
    return [{**f, "key": f"k{i}"} for i, f in enumerate(fields)]


@pytest.mark.parametrize(
    "label",
    [
        EXPLAIN,
        "If so, which team?",
        "If you answered yes above, please explain.",
        'If you answered "Yes" to the above question, please provide additional information here:',
        "Please explain if yes",
    ],
)
def test_a_follow_up_is_asked_only_after_a_yes(label):
    fields = page(field(RELATED, YES_NO, "select-one"), field(label))
    assert questions.follow_up(label)
    for parent, idle in (("No", True), ("Yes", False), ("Prefer not to say", True), (None, True)):
        filled = [{"key": "k0", "value": parent}] if parent else []
        assert questions.idle_follow_ups(fields, filled) == ({"k1"} if idle else set()), parent
    # A yes to an earlier question does not wake a follow-up of a later one.
    fields = page(
        field("Do you have a car?", YES_NO), field(RELATED, YES_NO), field(label, required=True)
    )
    assert questions.idle_follow_ups(fields, [{"key": "k0", "value": "Yes"}]) == {"k2"}


FOLLOW_UP_FORM = f"""<title>Apply</title><form>
<label for="r">{RELATED}</label>
<select id="r" name="r" required><option value="">Select...</option>
<option>Yes</option><option>No</option></select>
<label for="e">{EXPLAIN}</label><input id="e" name="e" required>
</form>"""


def test_a_follow_up_to_a_no_is_left_blank_without_a_word(reader, state, monkeypatch):
    general = common_questions.BY_ID["related_to_employee"].label
    assert workflow.remember_answer(general, YES_NO, "No", "memory:m1")
    filled, pending = fill(reader, FOLLOW_UP_FORM, monkeypatch)
    assert [(f["label"], f["value"]) for f in filled] == [(RELATED, "No")]
    assert pending == []
    assert workflow.forget_answer(general)
    assert workflow.remember_answer(general, YES_NO, "Yes", "memory:m2")
    filled, pending = fill(reader, FOLLOW_UP_FORM, monkeypatch)
    assert [q["label"] for q in pending] == [EXPLAIN]  # a yes: a question like any other


# --- 5. a free pick among offered options -------------------------------------------------
TEAMS = ["Infrastructure", "Product Engineering", "Developer Tools", "Data Platform"]


@pytest.mark.parametrize(
    "label",
    [
        "Which software team(s) are you most interested in?",
        "Which office do you prefer?",
        "What areas interest you?",
    ],
)
def test_a_preference_is_a_pick_among_the_offered_options(label):
    question = {"label": label, "options": TEAMS}
    assert questions.preference_choice(question)
    assert questions.draft_gate(question) is None
    assert "Do not answer needs_user" in questions.drafting_hints(question)["answer_rule"]
    assert not questions.preference_choice({"label": label, "options": []})


def test_a_preference_never_covers_a_fact_or_a_legal_question():
    for label, options in (
        ("Which gender do you prefer to be identified as?", ["Man", "Woman"]),
        ("Preferred work arrangement", ["Remote", "Hybrid", "On-site"]),
        ("Which visa status do you prefer to list?", ["H-1B", "F-1"]),
    ):
        assert not questions.preference_choice({"label": label, "options": options}), label


def test_the_drafting_context_tells_the_model_to_pick(state, monkeypatch):
    from rove.onboarding import read_approved

    team = {"key": KEYS[0], "label": "Which software team(s) are you most interested in?"}
    team["options"] = TEAMS
    sent = drafting(monkeypatch, [[{**drafted(KEYS[0], "Developer Tools")}]])
    app = workflow.enqueue("https://jobs.ashbyhq.com/acme/1")["application_id"]
    (state / "applications" / app).mkdir(parents=True)
    result = reasoning.review_application(
        app, {"profile_hash": read_approved()["profile_hash"], "pending": [team], "text": ""}
    )
    (asked,) = sent[0]["questions"]
    assert asked["answer_rule"] == questions.PREFERENCE_RULE
    assert result["answers"][0]["kind"] == "proposal"
    assert result["answers"][0]["value"] == "Developer Tools"


# --- 6. ties to the employer, answered once ---------------------------------------------
@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        (RELATED, "related_to_employee"),
        ("Do you have any relatives who currently work at Acme?", "related_to_employee"),
        ("Do you know anyone who currently works at Acme?", "knows_employee"),
        ("Were you referred by a current Acme employee?", "referred_by_employee"),
        ("Have you ever interviewed with Acme before?", "previously_interviewed_here"),
        ("Have you ever applied to Acme before?", "previously_applied_here"),
    ],
)
def test_ties_to_the_employer_have_their_own_ids(label, canonical):
    question = classify(label)
    assert (question.canonical_id, question.scope, question.sensitivity) == (
        canonical,
        "employer",
        questions.PLAIN,
    )


def test_a_general_no_holds_until_the_owner_or_rove_says_otherwise(state):
    for identifier in ("related_to_employee", "previously_applied_here"):
        entry = common_questions.BY_ID[identifier]
        assert workflow.remember_answer(entry.label, YES_NO, "No", f"memory:{identifier}")
    assert answer(RELATED, YES_NO, recall=workflow.recall_answer, employer=ACME) == (
        "No",
        questions.REMEMBERED,
    )
    # His answer for one company replaces the general one there only.
    assert workflow.remember_answer(RELATED, YES_NO, "Yes", "m1", employer=ACME)
    assert answer(RELATED, YES_NO, recall=workflow.recall_answer, employer=ACME)[0] == "Yes"
    other = "ashbyhq.com/globex"
    assert answer(RELATED, YES_NO, recall=workflow.recall_answer, employer=other)[0] == "No"
    # "Applied before?": the general no stands until Rove itself has sent one there.
    applied = "Have you ever applied to Acme before?"
    assert answer(applied, YES_NO, recall=workflow.recall_answer, employer=ACME)[0] == "No"
    sent = workflow.enqueue("https://jobs.ashbyhq.com/acme/77")["application_id"]
    workflow.set_state(sent, "APPLIED")
    assert answer(applied, YES_NO, recall=workflow.recall_answer, employer=ACME) == (None, None)
    assert answer(applied, YES_NO, recall=workflow.recall_answer, employer=other)[0] == "No"


# --- 7. U.S. person: asked once, then remembered -------------------------------------------
US_PERSON = (
    "Are you a U.S. Person (U.S. citizen, U.S. national, lawful permanent resident, "
    "refugee, or asylee)?"
)
ITAR = (
    "Are you a 'U.S. Person' as defined by the International Traffic in Arms Regulations "
    "(22 CFR 120.62)?"
)


def test_a_us_person_answer_comes_from_citizenship_or_is_asked_once(state):
    for label in (US_PERSON, ITAR):
        question = classify(label)
        assert (question.canonical_id, question.sensitivity) == ("us_person", questions.SENSITIVE)
        assert questions.draft_gate({"label": label})["code"].startswith("sensitive:")
    citizen = {**OWNER, "eligibility": {**OWNER["eligibility"], "us_citizen": True}}
    assert answer(US_PERSON, YES_NO, profile=citizen) == ("Yes", "eligibility.us_citizen")
    # Not a citizen, or not said: never a default, never a draft; his answer, kept once.
    not_citizen = {**OWNER, "eligibility": {**OWNER["eligibility"], "us_citizen": False}}
    assert answer(US_PERSON, YES_NO, profile=not_citizen) == (None, None)
    assert answer(US_PERSON, YES_NO, recall=workflow.recall_answer) == (None, None)
    assert workflow.remember_answer(US_PERSON, YES_NO, "Yes", "m1")
    assert answer(ITAR, YES_NO, recall=workflow.recall_answer) == ("Yes", questions.REMEMBERED)
    # Other export-control wordings are kept too, each for its own words.
    sanctions = "Are you a citizen or resident of Cuba, Iran, North Korea or Syria?"
    assert workflow.remember_answer(sanctions, YES_NO, "No", "m2")
    assert workflow.recall_answer(sanctions, YES_NO) == "No"


# --- 8. one fact in two fields --------------------------------------------------------------
PAIR = page(
    field("Expected Graduation Month", GRADUATION_MONTHS, "select-one"),
    field("Expected Graduation Year", kind="number"),
    field("City"),
    field("State"),
)


def test_both_halves_of_a_fact_come_from_one_record():
    stated = "education.schools.1.graduation_month"
    agree = [
        {
            "key": "k0",
            "label": "Expected Graduation Month",
            "value": "April/May/June",
            "source": stated,
        },
        {"key": "k1", "label": "Expected Graduation Year", "value": str(Y + 3), "source": stated},
        {"key": "k2", "label": "City", "value": "Springfield", "source": "identity.city"},
        {"key": "k3", "label": "State", "value": "Illinois", "source": "identity.state_region"},
    ]
    assert questions.pair_problems(PAIR, agree) == []
    # The live failure: a drafted month beside the profile's year.
    drafted_month = [{**agree[0], "value": "December", "source": questions.USED_DRAFT}, agree[1]]
    (problem,) = questions.pair_problems(PAIR, drafted_month)
    assert problem["key"] == "k0" and "came from your profile" in problem["reason"]
    filled, pending = questions.settled_page(PAIR, drafted_month, [])
    assert [f["key"] for f in filled] == ["k1"] and [q["key"] for q in pending] == ["k0"]
    # Two schools' halves: both wait.
    mixed = [{**agree[0], "source": "education.schools.0.graduation_month"}, agree[1]]
    assert {p["key"] for p in questions.pair_problems(PAIR, mixed)} == {"k0", "k1"}
    # A state from anywhere but the profile beside the profile's city waits too.
    guessed = [agree[2], {**agree[3], "source": questions.REMEMBERED}]
    assert [p["key"] for p in questions.pair_problems(PAIR, guessed)] == ["k3"]
    # His own answers for both halves agree with each other: nothing to hold.
    his = [{**agree[0], "source": questions.REMEMBERED}, {**agree[1], "source": "owner m1"}]
    assert questions.pair_problems(PAIR, his) == []
    for label in ("City", "State"):
        assert owner_only(label), label
