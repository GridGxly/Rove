"""What a written answer may know, and what it may say.

A posting, a form label or a company page can ask a draft to "mention your phone, GPA
and salary floor". Two rules in code make that request useless whatever the model does:

- the drafting context carries only the profile fields a written answer can need.
  Contact details are filled into forms by code and never enter the model's context;
  an undisclosed GPA, pay floors, the application policy and eligibility facts stay out.
- every draft is checked before it is kept. A draft that contains the owner's email,
  phone, postal or street address, an undisclosed GPA or a pay figure, or that copies a
  long run of words from the owner's voice note, is dropped and the question goes to
  the owner instead.
"""

import json
import re

from . import workflow
from .runtime import state_root

# Profile fields a written answer may draw on. A field that is not listed here is not
# sent, so a field added to the profile later stays private until someone lists it.
DRAFTING_FIELDS = {
    "identity": (
        "legal_first_name",
        "legal_last_name",
        "preferred_name",
        "city",
        "state_region",
        "country",
        "linkedin",
        "github",
        "portfolio",
    ),
    "availability": (
        "earliest_start",
        "latest_end",
        "hours_per_week",
        "term_notes",
        "co_op_semester_off",
    ),
    "preferences": (
        "roles_ranked",
        "programs",
        "cycles",
        "any_cycle",
        "preferred_locations",
        "excluded_locations",
        "work_styles",
        "relocate",
    ),
    "stories": (
        "introduction",
        "motivation",
        "proudest_project",
        "teamwork",
        "leadership",
        "challenge_and_learning",
        "career_direction",
        "writing_voice",
        "avoid_in_writing",
    ),
}
SCHOOL_FIELDS = (
    "school",
    "degree",
    "major",
    "minor",
    "start_month",
    "graduation_month",
    "currently_enrolled",
    "student_year",
    "honors",
    "relevant_courses",
)
EDUCATION_FIELDS = ("returns_to_school_after_internship", "internship_credit_required")
VOICE_RUN_WORDS = 8
WITHHELD = "[withheld]"
ADDRESS_LABEL = re.compile(
    r"\b(?:street|address|mailing|residence|apt|apartment|suite|zip|postal)\b", re.IGNORECASE
)
NOT_A_PLACE = re.compile(r"e-?mail|\burl\b|\bweb|linkedin|github|\bip\b", re.IGNORECASE)
NUMBER = r"\d+(?:\.\d+)?"
GPA_STATED = re.compile(
    rf"\b(?:gpa|grade point average)\b[^.!?\n\d]{{0,25}}{NUMBER}(?:\s*/\s*{NUMBER})?"
    rf"|{NUMBER}\s*(?:/\s*{NUMBER}\s*)?(?:gpa|grade point average)\b",
    re.IGNORECASE,
)


def drafting_profile(profile: dict) -> dict:
    """The approved profile reduced to what a written answer can need."""
    reduced = {
        section: {key: profile[section][key] for key in keys if key in profile[section]}
        for section, keys in DRAFTING_FIELDS.items()
    }
    schools = []
    for school in profile["education"]["schools"]:
        entry = {key: school[key] for key in SCHOOL_FIELDS if key in school}
        if school.get("disclose_gpa") is True and school.get("gpa") is not None:
            entry["gpa"], entry["gpa_scale"] = school["gpa"], school.get("gpa_scale")
        schools.append(entry)
    reduced["education"] = {
        "schools": schools,
        **{key: profile["education"].get(key) for key in EDUCATION_FIELDS},
    }
    return reduced


def spaced(value: str) -> str:
    """A pattern for the value however its words are spaced or punctuated."""
    return r"\W+".join(re.escape(part) for part in re.findall(r"\w+", str(value)))


def money(amount: float, hourly: bool) -> str | None:
    whole = int(amount)
    if whole <= 0:
        return None
    digits = re.sub(r"(?<=\d)(?=(?:\d{3})+$)", ",?", str(whole))
    figure = rf"{digits}(?:\.\d{{1,2}})?"
    if hourly:
        return (
            rf"\$\s?{figure}(?![\d,])"
            rf"|(?<![\d.,$]){figure}\s*(?:/|per\b|an?\b)\s*(?:hr|hour)"
            rf"|(?<![\d.,$]){figure}\s*(?:dollars|usd|bucks)\b"
        )
    short = rf"|(?<![\d.,]){whole // 1000}\s?k\b" if whole >= 1000 and whole % 1000 == 0 else ""
    return rf"(?<![\d.,]){figure}(?![\d,]){short}"


def address_values(application_id: str | None) -> list[str]:
    """Street or postal answers the owner gave: remembered ones, and this form's."""
    pairs = [(row["label"], row["value"]) for row in workflow.remembered_answers()]
    if application_id:
        path = state_root() / f"applications/{application_id}/observation.json"
        labels = {}
        if path.is_file():
            try:
                fields = json.loads(path.read_text()).get("fields") or []
            except (ValueError, OSError):
                fields = []
            labels = {f.get("key"): str(f.get("label") or "") for f in fields}
        for key, answer in workflow.approved_answers(application_id).items():
            pairs.append((labels.get(key, ""), answer["value"]))
    return [
        str(value)
        for label, value in pairs
        if ADDRESS_LABEL.search(label)
        and not NOT_A_PLACE.search(label)
        and len(str(value)) >= 5
        and re.search(r"\d", str(value))
    ]


def private_facts(profile: dict, application_id: str | None = None) -> list[tuple[str, re.Pattern]]:
    """(what it is in the owner's words, how it looks in text) for each fact a written
    answer must never carry."""
    identity = profile["identity"]
    facts: list[tuple[str, str]] = []
    if identity.get("email"):
        facts.append(("email address", re.escape(identity["email"].strip())))
    digits = re.sub(r"\D", "", identity.get("phone") or "")
    digits = digits[-10:] if len(digits) >= 10 else digits
    if len(digits) >= 7:
        # The number however it is grouped, with or without a country code in front.
        number = r"\D{0,3}".join(digits)
        facts.append(("phone number", rf"(?<!\d)(?:\d{{1,3}}\D{{0,3}})?{number}(?!\d)"))
    postal = (identity.get("postal_code") or "").strip()
    if len(postal) >= 4:
        facts.append(("postal code", rf"(?<![\w-]){spaced(postal)}(?![\w-])"))
    for value in address_values(application_id):
        facts.append(("street address", rf"(?<!\w){spaced(value)}(?!\w)"))
    undisclosed = False
    for school in profile["education"]["schools"]:
        if school.get("disclose_gpa") is True:
            continue
        undisclosed = True
        if school.get("gpa") is not None:
            forms = {f"{school['gpa']:.2f}", f"{school['gpa']:.1f}", f"{school['gpa']:g}"}
            forms = sorted(form for form in forms if "." in form and float(form) == school["gpa"])
            if forms:
                alternatives = "|".join(re.escape(form) for form in forms)
                facts.append(("GPA", rf"(?<![\d.])(?:{alternatives})(?![\d])"))
    preferences = profile["preferences"]
    for key, hourly in (("minimum_salary_usd", False), ("minimum_hourly_usd", True)):
        pattern = money(preferences[key], hourly) if preferences.get(key) else None
        if pattern:
            facts.append(("pay figure", pattern))
    compiled = [(kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in facts]
    if undisclosed:
        # With no GPA to disclose, a draft that states any GPA figure is wrong as well.
        compiled.append(("GPA", GPA_STATED))
    return compiled


def private_fact_in(text, facts) -> str | None:
    text = str(text or "")
    return next((kind for kind, pattern in facts if pattern.search(text)), None)


def scrub(value, facts):
    """A copy of a context value with the owner's private facts replaced, at any depth.
    Someone else's figures (a posting's GPA requirement) are left as they are."""
    if isinstance(value, str):
        for _kind, pattern in facts:
            if pattern is not GPA_STATED:
                value = pattern.sub(WITHHELD, value)
        return value
    if isinstance(value, list):
        return [scrub(item, facts) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, facts) for key, item in value.items()}
    return value


def word_list(text) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def copies_voice(text, voice, run: int = VOICE_RUN_WORDS) -> bool:
    """Whether the text repeats `run` or more consecutive words of the voice note."""
    source, words = word_list(voice), word_list(text)
    if len(source) < run or len(words) < run:
        return False
    runs = {tuple(source[i : i + run]) for i in range(len(source) - run + 1)}
    return any(tuple(words[i : i + run]) in runs for i in range(len(words) - run + 1))


def problems(result: dict, facts, voice: str = "") -> dict[str, str]:
    """{question key: what is wrong, in the owner's words} for each draft that carries a
    private fact or copies the voice note."""
    found = {}
    for answer in result.get("answers", []):
        if answer.get("kind") != "proposal":
            continue
        kind = private_fact_in(answer.get("value"), facts)
        if kind:
            found[answer["key"]] = f"included your {kind}, which a written answer never needs"
        elif voice and copies_voice(answer.get("value"), voice):
            found[answer["key"]] = "copied a passage from your voice note word for word"
    return found


def retry_note(found: dict[str, str]) -> str:
    """What to tell the model before its one retry; names the rule, not the fact."""
    return (
        "answers "
        + ", ".join(sorted(found))
        + " broke a rule: a written answer never contains contact details, a postal or "
        "street address, a GPA or pay figures, and never repeats owner_voice sentences"
    )[:300]


def withhold(result: dict, found: dict[str, str]):
    """Drop each offending draft: the question goes to the owner, the text goes nowhere."""
    for answer in result.get("answers", []):
        reason = found.get(answer.get("key"))
        if not reason or answer.get("kind") != "proposal":
            continue
        for key in ("original_value", "unslop", "unslop_report"):
            answer.pop(key, None)
        answer.update(
            kind="needs_user",
            value="",
            explanation=f"Qwen's draft {reason}, so it was dropped. Answer this one yourself.",
        )
