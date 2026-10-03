"""Which of the owner's schools an application states, in one place.

The approved profile may list several schools. One of them may be marked `apply_as`:
that is the entry applications state (its school, degree, major and graduation), the
one the fit review and class standing go by, and the first education block of a form.
Without a mark, a profile with a single school applies as that school; a profile with
several unmarked schools states none of them, so nothing is guessed.

Two facts never follow the mark, because they are about now, not about the application:
where the owner is enrolled today (the entry whose dates include today), and the GPA
(it belongs to the school where it was earned).
"""

import re
from datetime import UTC, datetime

# A "rising junior" in summer Y graduates between these months after June of Y.
RISING_MONTHS = {"senior": (5, 15), "junior": (17, 27), "sophomore": (29, 39)}
YEARS_IN_SCHOOL = ("senior", "junior", "sophomore", "freshman")  # by years left


def schools(profile: dict | None) -> list[dict]:
    return list(((profile or {}).get("education") or {}).get("schools") or [])


def primary_index(profile: dict | None) -> int | None:
    """The position of the entry applications state: the marked one, else the only one."""
    entries = schools(profile)
    marked = [i for i, school in enumerate(entries) if school.get("apply_as") is True]
    if len(marked) == 1:
        return marked[0]
    if not marked and len(entries) == 1:
        return 0
    return None


def primary(profile: dict | None) -> dict | None:
    index = primary_index(profile)
    return None if index is None else schools(profile)[index]


def ordered(profile: dict | None) -> list[tuple[int, dict]]:
    """(position in the profile, entry): the primary entry first, the others after it in
    the profile's order. Empty when no entry is primary."""
    index = primary_index(profile)
    if index is None:
        return []
    entries = schools(profile)
    return [(index, entries[index])] + [(i, s) for i, s in enumerate(entries) if i != index]


def this_month(today: datetime | None = None) -> str:
    return (today or datetime.now(UTC)).strftime("%Y-%m")


def current_index(profile: dict | None, today: datetime | None = None) -> int | None:
    """The entry the owner is enrolled in today: its start is not after this month, its
    graduation not before it, and it is not marked as left. None when no entry, or more
    than one, fits: enrollment is never guessed."""
    now = this_month(today)
    fitting = []
    for i, school in enumerate(schools(profile)):
        start, end = school.get("start_month"), school.get("graduation_month")
        enrolled = school.get("currently_enrolled")
        if start is None and end is None and enrolled is not True:
            continue  # nothing says when
        if enrolled is False or (start and start > now) or (end and end < now):
            continue
        fitting.append(i)
    return fitting[0] if len(fitting) == 1 else None


def current(profile: dict | None, today: datetime | None = None) -> dict | None:
    index = current_index(profile, today)
    return None if index is None else schools(profile)[index]


def graduation(profile: dict | None) -> str | None:
    """The graduation month applications state, as YYYY-MM."""
    school = primary(profile)
    return (school or {}).get("graduation_month") or None


def disclosed_gpa(profile: dict | None) -> tuple[int, dict] | None:
    """(position, entry) of the one school whose GPA the owner discloses; None when no
    school or several do, so a form's plain "GPA" never gets the wrong school's."""
    disclosed = [
        (i, s)
        for i, s in enumerate(schools(profile))
        if s.get("disclose_gpa") is True and s.get("gpa") is not None
    ]
    return disclosed[0] if len(disclosed) == 1 else None


def academic_year_end(when: datetime) -> int:
    """The calendar year an academic year ends in: August starts the next one."""
    return when.year + 1 if when.month >= 8 else when.year


def years_left(graduation_month: str, year_ending: int) -> int:
    """Whole academic years between the one ending in `year_ending` and graduation."""
    year, month = (int(x) for x in graduation_month.split("-"))
    return (year + 1 if month >= 8 else year) - year_ending


def year_in_school(profile: dict | None, today: datetime | None = None) -> str | None:
    """Freshman to senior today, from the stated entry's expected graduation."""
    month = graduation(profile)
    if not month:
        return None
    left = years_left(month, academic_year_end(today or datetime.now(UTC)))
    return YEARS_IN_SCHOOL[left] if 0 <= left < len(YEARS_IN_SCHOOL) else None


def rising(graduation_month: str | None, summer: int) -> str | None:
    """ "senior", "junior" or "sophomore" for a student rising into that year in summer
    `summer`, by the months from June of that year to graduation."""
    if not graduation_month:
        return None
    year, month = (int(x) for x in graduation_month.split("-"))
    after_june = (year - summer) * 12 + (month - 6)
    return next((n for n, (lo, hi) in RISING_MONTHS.items() if lo <= after_june <= hi), None)


def class_years(profile: dict | None, summer: int) -> set[str]:
    """The class words a posting for summer `summer` may use for the owner: the year he
    finishes that spring ("sophomores") and the year he rises into ("rising juniors")."""
    month = graduation(profile)
    if not month:
        return set()
    left = years_left(month, summer)
    finished = YEARS_IN_SCHOOL[left] if 0 <= left < len(YEARS_IN_SCHOOL) else None
    found = {finished} & {"freshman", "sophomore"}
    return found | ({rising(month, summer)} & {"junior", "senior"})


def internship_year(text: str, today: datetime | None = None) -> int:
    """The internship's calendar year from the posting, else the next summer."""
    match = re.search(
        r"(?:summer|spring|fall|winter|intern[a-z]*)\D{0,20}(20\d\d)",
        str(text or ""),
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1))
    now = today or datetime.now(UTC)
    return now.year + 1 if now.month >= 8 else now.year


# --- a form's education blocks ---------------------------------------------------------
# Greenhouse numbers its education blocks in the ids: school--0, degree--1, end-year--1.
GREENHOUSE_BLOCK = re.compile(
    r"^(?:school|degree|discipline|start-month|start-year|end-month|end-year|gpa)--(\d+)$"
)


def block_index(field: dict) -> int | None:
    """Which education block of the form a field sits in: Greenhouse's numbered ids, or
    the place of a repeated question (the second "School" is the second block). None
    when the field is not in a numbered or repeated block."""
    if field.get("education_block") is not None:
        return int(field["education_block"])  # numbered with the whole page in view
    for attribute in (field.get("id"), field.get("name")):
        match = GREENHOUSE_BLOCK.match(str(attribute or ""))
        if match:
            return int(match[1])
    if field.get("occurrence"):
        return int(field["occurrence"])
    if field.get("twins"):
        return 0
    return None


def entry_for_block(profile: dict | None, index: int | None) -> tuple[int, dict] | None:
    """(position, entry) a form's education block states: the primary entry in the first
    block, the others in order after it. A form with one block gets the primary entry."""
    blocks = ordered(profile)
    position = index or 0
    return blocks[position] if position < len(blocks) else None
