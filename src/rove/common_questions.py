"""The common application questions, in code, for the one-time warm-up in `#memory`.

Each entry names a question by the canonical id questions.py gives it, words it the way
the owner reads it, says what kind of answer it takes, whether it is a legal or personal
question, and which approved-profile field answers it when one does. The warm-up asks
only the entries nothing answers yet: not the profile, not an earlier answer, not a
policy default. An answer goes through `workflow.remember_answer` under the entry's
label, so the live resolver finds it under the same canonical id a form's own wording
gets. Questions that differ per employer, that a policy already answers in a better
way, or that need a sentence are left out on purpose.

The parsing here is exact: a date is a date the owner wrote, a number is a number, a
choice is one of the options. Nothing is guessed.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import questions

SHORT_TEXT, NUMBER, DATE, MONTH, YES_NO, CHOICE = (
    "short text",
    "number",
    "date",
    "month",
    "yes/no",
    "choice",
)
SKIP = "skip"
TEXT_LIMIT = 300


@dataclass(frozen=True)
class CommonQuestion:
    id: str  # the canonical id questions.classify gives the label
    label: str  # the question in the owner's words; an answer is kept under it
    kind: str  # SHORT_TEXT | NUMBER | DATE | MONTH | YES_NO | CHOICE
    options: tuple[str, ...] = ()
    sensitive: bool = False  # legal or personal: kept with the sensitive marker
    profile: str = ""  # the approved-profile field that answers it, when one exists
    hint: str = ""  # how to answer, shown after the question
    only_when: str = ""  # a profile condition the question depends on
    # (pattern on the normalized answer, another label the same answer is kept under)
    aliases: tuple[tuple[str, str], ...] = ()
    low: float | None = None  # a number's allowed range
    high: float | None = None


CORPUS = (
    CommonQuestion(
        "start_availability",
        "When can you start?",
        DATE,
        profile="availability.earliest_start",
        hint="a date, like Jan 15 2027",
    ),
    CommonQuestion(
        "availability_end",
        "Until when are you available?",
        DATE,
        profile="availability.latest_end",
        hint="a date, like Aug 20 2027",
    ),
    CommonQuestion(
        "hours_per_week",
        "How many hours per week can you work?",
        NUMBER,
        profile="availability.hours_per_week",
        hint="a number, like 40",
        low=1,
        high=80,
    ),
    CommonQuestion(
        "graduation_date",
        "Expected graduation date",
        MONTH,
        profile="education.schools.0.graduation_month",
        hint="a month and year, like May 2027",
    ),
    CommonQuestion(
        "gpa",
        "Current GPA",
        NUMBER,
        profile="education.schools.0.gpa",
        hint="a number, like 3.8",
        only_when="gpa_disclosed",
        low=0,
        high=100,
    ),
    CommonQuestion(
        "salary_expectation",
        "Desired pay",
        SHORT_TEXT,
        sensitive=True,
        hint="say per hour or per year, like $30/hour or $70k/year",
        aliases=(
            (r"\bhour|\bhr\b|hourly", "Expected hourly rate"),
            (r"\byear|annual|salary|\bk\b", "Desired annual salary"),
        ),
    ),
    CommonQuestion(
        "relocate_willing",
        "Are you willing to relocate?",
        YES_NO,
        profile="preferences.relocate",
    ),
    CommonQuestion(
        "preferred_locations",
        "Preferred locations",
        SHORT_TEXT,
        profile="preferences.preferred_locations",
        hint="cities or regions, like Austin, TX; Chicago, IL",
    ),
    CommonQuestion(
        "work_style_preference",
        "Preferred work arrangement",
        CHOICE,
        options=("Remote", "Hybrid", "On-site"),
        profile="preferences.work_styles",
    ),
    CommonQuestion(
        "work_authorization_us",
        "Are you legally authorized to work in the United States?",
        YES_NO,
        sensitive=True,
        profile="eligibility.us_work_authorized",
    ),
    CommonQuestion(
        "sponsorship_us_now_or_future",
        "Will you now or in the future require sponsorship to work in the US?",
        YES_NO,
        sensitive=True,
        profile="eligibility.sponsorship_now+sponsorship_future",
    ),
    CommonQuestion(
        "security_clearance",
        "Do you have an active security clearance?",
        YES_NO,
        sensitive=True,
    ),
    CommonQuestion("references_available", "Can you provide references?", YES_NO),
    CommonQuestion("linkedin_url", "LinkedIn profile URL", SHORT_TEXT, profile="identity.linkedin"),
    CommonQuestion("github_url", "GitHub URL", SHORT_TEXT, profile="identity.github"),
    CommonQuestion("portfolio_url", "Portfolio", SHORT_TEXT, profile="identity.portfolio"),
    CommonQuestion(
        "languages_spoken",
        "Which languages do you speak?",
        SHORT_TEXT,
        hint="like English, Spanish",
    ),
    CommonQuestion(
        "drivers_license",
        "Do you have a valid driver's licence?",
        YES_NO,
        sensitive=True,
    ),
    CommonQuestion(
        "at_least_18",
        "Are you at least 18 years old?",
        YES_NO,
        sensitive=True,
        profile="eligibility.at_least_18",
    ),
    CommonQuestion(
        "essential_functions",
        "Are you able to perform the essential functions of the job with or without "
        "reasonable accommodation?",
        YES_NO,
        sensitive=True,
    ),
    CommonQuestion(
        "background_check_consent",
        "Do you consent to a background check?",
        YES_NO,
        sensitive=True,
    ),
    CommonQuestion(
        "contact_consent",
        "Do you agree to be contacted by text message about your application?",
        YES_NO,
        # Answered by the owner's standing rule (yes), so the warm-up never asks it.
    ),
    CommonQuestion("pronouns", "Pronouns", SHORT_TEXT, sensitive=True, hint="like he/him"),
    CommonQuestion(
        "preferred_name", "Preferred name", SHORT_TEXT, profile="identity.preferred_name"
    ),
    CommonQuestion(
        "street_address",
        "Street address",
        SHORT_TEXT,
        hint="the first line; city, state and postal code come from your profile",
    ),
    CommonQuestion("country", "Country of residence", SHORT_TEXT, profile="identity.country"),
)
BY_ID = {entry.id: entry for entry in CORPUS}


def applies(entry: CommonQuestion, profile: dict | None) -> bool:
    """Whether the owner should be asked this at all, given the approved profile."""
    if entry.only_when == "gpa_disclosed":
        schools = ((profile or {}).get("education") or {}).get("schools") or []
        return len(schools) == 1 and schools[0].get("disclose_gpa") is True
    return True


def field(entry: CommonQuestion) -> dict:
    """The entry as the form field the live resolver would see."""
    return {
        "label": entry.label,
        "kind": "select-one" if entry.options or entry.kind == YES_NO else "text",
        "options": [{"label": o} for o in choices(entry)],
    }


def choices(entry: CommonQuestion) -> tuple[str, ...]:
    return ("Yes", "No") if entry.kind == YES_NO else entry.options


def detail(entry: CommonQuestion) -> str:
    """What goes in brackets after the question: the options, or how to answer."""
    if choices(entry):
        return " / ".join(choices(entry))
    return entry.hint or entry.kind


def open_questions(profile: dict | None, recall) -> list[CommonQuestion]:
    """The corpus entries nothing answers yet, in corpus order.

    `recall` is `workflow.recall_answer`. An entry is answered when the live resolver,
    given the profile and the owner's earlier answers, returns a value for it.
    """
    pending = []
    for entry in CORPUS:
        if not applies(entry, profile):
            continue
        value, _source = questions.resolve(field(entry), profile or {}, recall=recall)
        if value is None:
            pending.append(entry)
    return pending


# --- answers as the owner types them -------------------------------------------------
YES_WORDS = frozenset({"yes", "y", "yeah", "yep", "yup", "true", "sure", "correct", "ok"})
NO_WORDS = frozenset({"no", "n", "nope", "false", "never"})
MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
NUMERIC_DATE = re.compile(
    r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?|(\d{1,2})/(\d{1,2})/(\d{2,4})|(\d{1,2})/(\d{4})"
)
# Words around a date that say nothing about it: "mid January 2027", "the 15th of May".
DATE_FILLER = frozenset(
    {"of", "the", "on", "from", "starting", "by", "mid", "early", "late", "st", "nd", "rd", "th"}
)


class Unreadable(ValueError):
    """The answer is not of the kind the question takes; the message says what would be."""


def month_number(word: str) -> int | None:
    """January, Jan or Janu is month 1; "mart" is nothing."""
    if len(word) < 3:
        return None
    return next((n for n, name in enumerate(MONTH_NAMES, start=1) if name.startswith(word)), None)


def parse_date(text: str, *, need_day: bool) -> tuple[int, int, int | None]:
    """(year, month, day or None) from a date as a person writes it, or Unreadable.

    Numbers go month/day/year or year-month-day; words go in any order with the month
    spelled out. A year is always required, written in full: "Jan 27" says nothing.
    """
    raw = " ".join(str(text or "").replace(",", " ").split()).lower()
    match = NUMERIC_DATE.fullmatch(raw)
    if match:
        if match.group(1):
            year, month, day = int(match.group(1)), int(match.group(2)), match.group(3)
            day = int(day) if day else None
        elif match.group(4):
            month, day, year = int(match.group(4)), int(match.group(5)), int(match.group(6))
        else:
            month, day, year = int(match.group(7)), None, int(match.group(8))
        return _checked(year, month, day, need_day)
    words = re.findall(r"[a-z]+|\d+", raw)
    months = [month_number(w) for w in words if w.isalpha() and month_number(w)]
    others = [w for w in words if w.isalpha() and not month_number(w) and w not in DATE_FILLER]
    numbers = [w for w in words if w.isdigit()]
    years = [w for w in numbers if len(w) == 4]
    days = [w for w in numbers if len(w) <= 2]
    if len(months) != 1 or others or len(numbers) != len(years) + len(days) or len(days) > 1:
        raise Unreadable("a date like Jan 15 2027 or 2027-01-15")
    if len(years) != 1:
        raise Unreadable("a date with its year written out, like Jan 15 2027")
    return _checked(int(years[0]), months[0], int(days[0]) if days else None, need_day)


def _checked(year: int, month: int, day: int | None, need_day: bool) -> tuple[int, int, int | None]:
    if year < 100:
        year += 2000
    try:
        datetime(year, month, day or 1, tzinfo=UTC)
    except ValueError as error:
        raise Unreadable("a real date, like Jan 15 2027") from error
    if not 2000 <= year <= 2100:
        raise Unreadable("a year like 2027")
    if need_day and day is None:
        raise Unreadable("a day as well, like Jan 15 2027")
    return year, month, day


def date_words(year: int, month: int, day: int | None) -> str:
    """The way the profile's own dates read on a form: January 15, 2027, or January 2027."""
    when = datetime(year, month, day or 1, tzinfo=UTC)
    return f"{when:%B} {day}, {year}" if day else f"{when:%B} {year}"


def parse_number(text: str, entry: CommonQuestion) -> str:
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(text or ""))
    if not match:
        raise Unreadable(entry.hint or "a number")
    digits = match.group(0).replace(",", ".")
    number = float(digits)
    if (entry.low is not None and number < entry.low) or (
        entry.high is not None and number > entry.high
    ):
        raise Unreadable(f"a number between {entry.low:g} and {entry.high:g}")
    return digits.rstrip("0").rstrip(".") if "." in digits else digits


def parse_choice(text: str, options: tuple[str, ...]) -> str:
    """The option the answer names: its text, its text without spaces ("onsite"), or the
    start of it when only one option starts that way."""
    wanted = questions.normalized(text)
    chosen = questions.match_option(options, text, loose=True)
    if chosen is None and wanted:
        squeezed = [o for o in options if questions.normalized(o).replace(" ", "") == wanted]
        starts = [o for o in options if questions.normalized(o).startswith(wanted)]
        chosen = squeezed[0] if len(squeezed) == 1 else starts[0] if len(starts) == 1 else None
    if chosen is None:
        raise Unreadable("one of " + " / ".join(options))
    return chosen


def normalize(entry: CommonQuestion, text: str) -> str:
    """The owner's answer as it is kept, checked for the entry's kind, or Unreadable."""
    answer = " ".join(str(text or "").split())
    if not answer:
        raise Unreadable("an answer")
    if entry.kind == YES_NO:
        word = questions.normalized(answer)
        if word in YES_WORDS:
            return "Yes"
        if word in NO_WORDS:
            return "No"
        raise Unreadable("yes or no")
    if entry.kind == CHOICE:
        return parse_choice(answer, entry.options)
    if entry.kind == NUMBER:
        return parse_number(answer, entry)
    if entry.kind in {DATE, MONTH}:
        if entry.id == "start_availability" and questions.normalized(answer) in {
            "immediately",
            "asap",
            "now",
            "right away",
            "as soon as possible",
        }:
            return "Immediately"
        year, month, day = parse_date(answer, need_day=False)
        return date_words(year, month, day if entry.kind == DATE else None)
    if len(answer) > TEXT_LIMIT:
        raise Unreadable(f"at most {TEXT_LIMIT} characters")
    return answer


def alias_labels(entry: CommonQuestion, answer: str) -> list[str]:
    """Other labels the same answer is kept under, by what the answer says."""
    text = questions.normalized(answer) + " " + str(answer).lower()
    return [label for pattern, label in entry.aliases if re.search(pattern, text)]
