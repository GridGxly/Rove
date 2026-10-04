"""The warm-up corpus: every entry is a question code knows, and the parsing is exact."""

import pytest

from rove import common_questions, questions
from rove.common_questions import BY_ID, CORPUS, Unreadable

NOBODY = lambda *args, **kwargs: None

FULL_PROFILE = {
    "identity": {
        "legal_first_name": "Alex",
        "legal_last_name": "Example",
        "preferred_name": "Al",
        "email": "alex@example.invalid",
        "phone": "+1 555-010-0199",
        "city": "Springfield",
        "state_region": "Illinois",
        "country": "United States",
        "postal_code": "62701",
        "linkedin": "https://www.linkedin.com/in/alex-example",
        "github": "https://github.com/alex-example",
        "portfolio": "https://alex.example",
    },
    "education": {
        "schools": [
            {
                "school": "Example University",
                "start_month": "2024-08",
                "graduation_month": "2027-12",
                "gpa": 3.8,
                "gpa_scale": 4.0,
                "disclose_gpa": True,
            }
        ]
    },
    "eligibility": {
        "us_work_authorized": True,
        "sponsorship_now": False,
        "sponsorship_future": False,
        "us_citizen": None,
        "at_least_18": True,
        "export_control_questions": "ask_each_time",
    },
    "availability": {
        "earliest_start": "2027-06-01",
        "latest_end": "2027-08-20",
        "hours_per_week": 40,
    },
    "preferences": {
        "relocate": True,
        "relocation_support_required": None,
        "work_styles": ["onsite"],
        "preferred_locations": ["Austin, TX", "Chicago, IL"],
        "excluded_locations": [],
    },
    "application_policy": {"demographics": "ask", "salary_questions": "ask"},
}
REQUIRED_IDS = {
    "start_availability",
    "availability_end",
    "hours_per_week",
    "graduation_date",
    "gpa",
    "salary_expectation",
    "relocate_willing",
    "preferred_locations",
    "work_style_preference",
    "work_authorization_us",
    "sponsorship_us_now_or_future",
    "security_clearance",
    "references_available",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "languages_spoken",
    "drivers_license",
    "at_least_18",
    "essential_functions",
    "background_check_consent",
    "contact_consent",
    "pronouns",
    "preferred_name",
    "street_address",
    "country",
    "school_start",
    "past_employers",
}
# Left out on purpose: per employer, a policy already answers it better, or free text.
LEFT_OUT = {"previously_employed_here", "how_did_you_hear", "gender"}
# Ties to an employer: one general answer for every company, overridden per company.
EMPLOYER_TIES = {
    "related_to_employee",
    "knows_employee",
    "referred_by_employee",
    "previously_interviewed_here",
    "previously_applied_here",
}


def test_the_corpus_covers_the_common_questions_and_nothing_per_employer():
    assert {entry.id for entry in CORPUS} >= REQUIRED_IDS | EMPLOYER_TIES
    for identifier in EMPLOYER_TIES:
        # Kept as the general answer: the key the employer-specific one falls back to.
        question = questions.classify(BY_ID[identifier].label)
        assert question.scope == "employer" and questions.memory_key(question), identifier
    assert not {entry.id for entry in CORPUS} & LEFT_OUT
    assert len({entry.id for entry in CORPUS}) == len(CORPUS)
    assert len({entry.label for entry in CORPUS}) == len(CORPUS)
    kinds = {
        common_questions.SHORT_TEXT,
        common_questions.NUMBER,
        common_questions.DATE,
        common_questions.MONTH,
        common_questions.YES_NO,
        common_questions.CHOICE,
    }
    assert all(entry.kind in kinds for entry in CORPUS)
    assert all(entry.options for entry in CORPUS if entry.kind == common_questions.CHOICE)


@pytest.mark.parametrize("entry", CORPUS, ids=[entry.id for entry in CORPUS])
def test_each_entry_is_a_question_code_knows_under_the_same_id(entry):
    """The label is kept under the canonical id a form's own wording gets, with the same
    class, so the live resolver and the warm-up agree on what is answered."""
    question = questions.classify(entry.label, "", common_questions.choices(entry))
    assert question.canonical_id == entry.id
    assert question.known and question.answerable
    assert (question.sensitivity == questions.SENSITIVE) == entry.sensitive
    assert questions.memory_key(question)


def test_the_profile_answers_what_the_corpus_says_it_does():
    """With every named profile field filled, only the entries with no profile field and
    no policy default are open; with an empty profile, all but the defaulted one are."""
    open_ids = {entry.id for entry in common_questions.open_questions(FULL_PROFILE, NOBODY)}
    assert open_ids == {
        entry.id for entry in CORPUS if not entry.profile and entry.id != "contact_consent"
    }
    for entry in CORPUS:
        if entry.profile:
            value, source = questions.resolve(
                common_questions.field(entry), FULL_PROFILE, recall=NOBODY
            )
            assert value is not None and source, entry.id
            assert source.split(".")[-1].split("+")[0] in entry.profile, (entry.id, source)
    bare = {entry.id for entry in common_questions.open_questions({}, NOBODY)}
    assert bare == {entry.id for entry in CORPUS} - {"contact_consent", "gpa", "school_start"}
    # GPA is asked only when the owner discloses it; with it disclosed and unknown, it is.
    # The school start is asked only of a profile with one school.
    disclosing = {"education": {"schools": [{"school": "U", "disclose_gpa": True}]}}
    assert "gpa" in {entry.id for entry in common_questions.open_questions(disclosing, NOBODY)}


def test_an_earlier_answer_closes_a_question():
    def remembered(label, options, **kwargs):
        return "January 15, 2027" if "start" in label.lower() else None

    assert "start_availability" not in {
        entry.id for entry in common_questions.open_questions({}, remembered)
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jan 15 2027", "January 15, 2027"),
        ("January 15, 2027", "January 15, 2027"),
        ("15 January 2027", "January 15, 2027"),
        ("the 15th of Jan 2027", "January 15, 2027"),
        ("2027-01-15", "January 15, 2027"),
        ("01/15/2027", "January 15, 2027"),
        ("1/15/27", "January 15, 2027"),
        ("Jan 2027", "January 2027"),
        ("mid January 2027", "January 2027"),
        ("2027-01", "January 2027"),
        ("1/2027", "January 2027"),
        ("asap", "Immediately"),
    ],
)
def test_dates_are_read_as_written_and_kept_the_way_the_profile_writes_them(text, expected):
    assert common_questions.normalize(BY_ID["start_availability"], text) == expected


@pytest.mark.parametrize(
    ("text", "need"),
    [
        ("Jan 15", "year"),
        ("Jan 27", "year"),
        ("soon", "date"),
        ("Feb 30 2027", "real date"),
        ("Mart 2027", "date"),
        ("15 16 Jan 2027", "date"),
        ("Jan 2027 or Feb 2027", "date"),
        ("1/15/1999", "year like 2027"),
    ],
)
def test_a_date_that_is_not_one_is_refused_with_what_would_be(text, need):
    with pytest.raises(Unreadable, match=need):
        common_questions.normalize(BY_ID["start_availability"], text)


def test_a_month_question_keeps_the_month_and_asks_for_the_year():
    assert common_questions.normalize(BY_ID["graduation_date"], "May 15 2027") == "May 2027"
    assert common_questions.normalize(BY_ID["graduation_date"], "december 2027") == "December 2027"
    with pytest.raises(Unreadable, match="year"):
        common_questions.normalize(BY_ID["graduation_date"], "May")


def test_numbers_are_numbers_within_the_questions_range():
    hours, gpa = BY_ID["hours_per_week"], BY_ID["gpa"]
    assert common_questions.normalize(hours, "40 hours") == "40"
    assert common_questions.normalize(hours, "about 20") == "20"
    assert common_questions.normalize(gpa, "3.80") == "3.8"
    assert common_questions.normalize(gpa, "3,7") == "3.7"
    assert common_questions.normalize(gpa, "GPA 3.9/4.0") == "3.9"
    with pytest.raises(Unreadable, match="between 1 and 80"):
        common_questions.normalize(hours, "500")
    with pytest.raises(Unreadable, match="number"):
        common_questions.normalize(hours, "full time")


def test_yes_no_and_choices_take_only_an_answer_that_names_one():
    clearance = BY_ID["security_clearance"]
    for word in ("yes", "Y", "yep", "sure"):
        assert common_questions.normalize(clearance, word) == "Yes"
    for word in ("no", "N", "nope", "never"):
        assert common_questions.normalize(clearance, word) == "No"
    with pytest.raises(Unreadable, match="yes or no"):
        common_questions.normalize(clearance, "maybe")
    style = BY_ID["work_style_preference"]
    assert common_questions.normalize(style, "onsite") == "On-site"
    assert common_questions.normalize(style, "On-Site") == "On-site"
    assert common_questions.normalize(style, "rem") == "Remote"
    for wrong in ("tuesday", "hybrid please", "remote or hybrid"):
        with pytest.raises(Unreadable, match="one of Remote / Hybrid / On-site"):
            common_questions.normalize(style, wrong)


def test_short_text_is_kept_as_typed_within_a_limit():
    languages = BY_ID["languages_spoken"]
    assert common_questions.normalize(languages, "  English,  Spanish ") == "English, Spanish"
    with pytest.raises(Unreadable, match="at most 300"):
        common_questions.normalize(languages, "x" * 301)
    with pytest.raises(Unreadable, match="an answer"):
        common_questions.normalize(languages, "   ")


def test_pay_is_also_kept_under_the_unit_it_names():
    pay = BY_ID["salary_expectation"]
    assert common_questions.alias_labels(pay, "$32/hour") == ["Expected hourly rate"]
    assert common_questions.alias_labels(pay, "32 an hour") == ["Expected hourly rate"]
    assert common_questions.alias_labels(pay, "$70k per year") == ["Desired annual salary"]
    assert common_questions.alias_labels(pay, "70,000 annually") == ["Desired annual salary"]
    assert common_questions.alias_labels(pay, "35 USD") == []
    for label in ("Expected hourly rate", "Desired annual salary"):
        assert questions.classify(label).canonical_id.startswith("salary_expectation_")


@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        ("How many hours per week are you available?", "hours_per_week"),
        ("Hours per week available", "hours_per_week"),
        ("Weekly hours", "hours_per_week"),
        ("Availability end date", "availability_end"),
        ("Until when are you available?", "availability_end"),
        ("How long are you available for?", "availability_end"),
        ("Internship end date", "availability_end"),
        ("Can you provide references upon request?", "references_available"),
        ("References available upon request?", "references_available"),
        ("Do you consent to a background check?", "background_check_consent"),
        ("Are you willing to undergo a background check?", "background_check_consent"),
        ("Background check consent", "background_check_consent"),
        (
            "Can you perform the essential functions of this job with or without accommodation?",
            "essential_functions",
        ),
        ("Do you have a valid driver's license?", "drivers_license"),
        ("Do you hold a current drivers licence and reliable transportation?", "drivers_license"),
        ("Preferred work arrangement", "work_style_preference"),
        ("Remote, hybrid, or on-site?", "work_style_preference"),
        ("Preferred office location", "preferred_locations"),
        ("What languages do you speak?", "languages_spoken"),
        ("Address Line 1 *", "street_address"),
        ("Street address", "street_address"),
    ],
)
def test_forms_word_the_corpus_questions_in_their_own_ways(label, canonical):
    assert questions.classify(label).canonical_id == canonical


@pytest.mark.parametrize(
    "label",
    [
        "End date",  # a work-history field
        "Expected end date",
        "Languages",  # programming languages, on a developer form
        "Programming languages",
        "References",  # a list of people, not a yes or no
        "May we contact your references?",
        "Email address",
        "Hours",
        "What is your preferred location for the interview?",
    ],
)
def test_wordings_that_mean_something_else_are_not_taken_for_a_corpus_question(label):
    question = questions.classify(label)
    assert question.canonical_id not in {entry.id for entry in CORPUS} - {"email"}, label


def test_the_profile_fills_the_new_questions_as_text_and_as_an_option():
    assert questions.resolve(
        {"label": "Hours per week available", "kind": "text"}, FULL_PROFILE
    ) == (
        "40",
        "profile.availability.hours_per_week",
    )
    assert questions.resolve({"label": "Availability end date", "kind": "text"}, FULL_PROFILE) == (
        "August 20, 2027",
        "profile.availability.latest_end",
    )
    offices = {
        "label": "Preferred office location",
        "kind": "select-one",
        "options": [{"label": "Austin, TX"}, {"label": "Seattle, WA"}],
    }
    assert questions.resolve(offices, FULL_PROFILE) == (
        "Austin, TX",
        "preferences.preferred_locations",
    )
    assert questions.resolve({"label": "Preferred locations", "kind": "text"}, FULL_PROFILE)[0] == (
        "Austin, TX, Chicago, IL"
    )
    arrangement = {
        "label": "Preferred work arrangement",
        "kind": "select-one",
        "options": [{"label": "Remote"}, {"label": "Onsite"}, {"label": "Hybrid"}],
    }
    assert questions.resolve(arrangement, FULL_PROFILE) == ("Onsite", "preferences.work_styles")
    two_styles = {
        **FULL_PROFILE,
        "preferences": {**FULL_PROFILE["preferences"], "work_styles": ["onsite", "hybrid"]},
    }
    assert questions.resolve(arrangement, two_styles) == (None, None)  # two fit: the owner picks
    assert questions.resolve({"label": "Preferred work arrangement", "kind": "text"}, two_styles)[
        0
    ] == ("On-site, Hybrid")


def test_the_school_start_and_past_employers_are_asked_once_and_kept_as_written():
    start, employers = BY_ID["school_start"], BY_ID["past_employers"]
    assert common_questions.normalize(start, "aug 2024") == "August 2024"
    with pytest.raises(Unreadable, match="year"):
        common_questions.normalize(start, "August")
    assert common_questions.normalize(employers, " Globex Robotics,  Initech ") == (
        "Globex Robotics, Initech"
    )
    assert common_questions.normalize(employers, "none") == "none"
    # Answered by the profile's enrollment month; asked only of a profile with one school.
    assert start.id not in {e.id for e in common_questions.open_questions(FULL_PROFILE, NOBODY)}
    two = {"education": {"schools": [{"school": "A"}, {"school": "B"}]}}
    assert not common_questions.applies(start, two)
    assert employers.id in {e.id for e in common_questions.open_questions(FULL_PROFILE, NOBODY)}
