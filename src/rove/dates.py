"""Dates written the way each control takes them.

A value (an approved graduation month, an approved start day, or an answer the owner
typed) is read once into year, month and, when it names one, day. It is then written for
the control in front of it:

- `input[type=date]`: ISO, `2027-06-01`
- `input[type=month]`: `2027-12`
- a text box with a date mask in its placeholder, pattern or label (`MM/DD/YYYY`,
  `MM/YYYY`, `YYYY-MM-DD`, `DD/MM/YYYY`): the digits in the mask's order, and the same
  with the mask's separators for a box that does not insert them itself
- a month, day or year dropdown: its one option for that month, day or year

A value that names no day is never given one: a control that needs a day then stays a
question. Nothing here decides what the date is; it only writes it.
"""

import re
from datetime import date

MONTHS = (
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
MONTH_WORDS = {name: i for i, name in enumerate(MONTHS, start=1)} | {
    name[:3]: i for i, name in enumerate(MONTHS, start=1)
}
MONTH_WORDS["sept"] = 9

# A date mask: two or three parts of MM, DD, YYYY (or YY, M, D) with one separator kind.
MASK = re.compile(
    r"\b((?:mm|dd|yyyy|yy|m|d)(?:\s*([/.\-])\s*(?:mm|dd|yyyy|yy|m|d)){1,2})\b", re.IGNORECASE
)
DATE_KINDS = {"date", "month"}


def _valid(year: int, month: int, day: int | None):
    if not 1 <= month <= 12 or not 1900 <= year <= 2100:
        return None
    if day is not None:
        try:
            date(year, month, day)
        except ValueError:
            return None
    return year, month, day


def parse(value) -> tuple[int, int, int | None] | None:
    """(year, month, day or None) from an ISO, numeric or written date; None otherwise."""
    text = " ".join(str(value or "").replace(",", " ").split()).strip().lower()
    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text)
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", text)
    if m:
        return _valid(int(m[1]), int(m[2]), int(m[3]) if m[3] else None)
    m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})", text)
    if m:
        first, second, year = int(m[1]), int(m[2]), int(m[3])
        # Month first, the way the owner writes dates; day first only when it cannot be a month.
        if first > 12 >= second:
            return _valid(year, second, first)
        return _valid(year, first, second)
    m = re.fullmatch(r"(\d{1,2})\s*[/.\-]\s*(\d{4})", text)
    if m:
        return _valid(int(m[2]), int(m[1]), None)
    words = re.findall(r"[a-z]+|\d+", text)
    months = [MONTH_WORDS[w] for w in words if w in MONTH_WORDS]
    numbers = [int(w) for w in words if w.isdigit()]
    years = [n for n in numbers if n >= 1000]
    days = [n for n in numbers if n < 100]
    others = [w for w in words if not w.isdigit() and w not in MONTH_WORDS and w != "of"]
    if len(months) != 1 or len(years) != 1 or len(days) > 1 or others:
        return None
    return _valid(years[0], months[0], days[0] if days else None)


def mask_of(field: dict) -> str | None:
    """The date mask a text box shows in its placeholder, pattern or label, as written."""
    for text in (field.get("placeholder"), field.get("pattern"), field.get("label")):
        m = MASK.search(str(text or ""))
        if m and re.search(r"y", m[1], re.IGNORECASE):
            return m[1]
    return None


def masked(parts: tuple, mask: str) -> tuple[str, str] | None:
    """(digits only, with separators) for the mask, or None when it needs a missing day."""
    year, month, day = parts
    pieces = re.findall(r"[a-z]+", mask.lower())
    separator = re.search(r"[/.\-]", mask)
    written = []
    for piece in pieces:
        if piece.startswith("d"):
            if day is None:
                return None
            written.append(f"{day:02d}" if piece == "dd" else str(day))
        elif piece.startswith("m"):
            written.append(f"{month:02d}" if piece == "mm" else str(month))
        else:
            written.append(f"{year:04d}" if piece == "yyyy" else f"{year % 100:02d}")
    return "".join(written), (separator[0] if separator else "").join(written)


def for_input(value, field: dict) -> str | None:
    """The value for a date or month input, or a masked text box; None when it cannot be.

    A field that is neither is not this module's: its value comes back unchanged.
    """
    kind = field.get("kind")
    mask = mask_of(field) if kind in {"text", "tel"} else None
    if kind not in DATE_KINDS and not mask:
        return value
    parts = parse(value)
    if parts is None:
        # A masked box keeps what the owner wrote; a date control takes nothing else.
        return value if mask else None
    year, month, day = parts
    if kind == "month":
        return f"{year:04d}-{month:02d}"
    if kind == "date":
        return None if day is None else f"{year:04d}-{month:02d}-{day:02d}"
    written = masked(parts, mask)
    return written[1] if written else None


def is_date_box(field: dict) -> bool:
    return field.get("kind") in DATE_KINDS or (
        field.get("kind") in {"text", "tel"} and mask_of(field) is not None
    )


def same_date(expected: str, kept: str) -> bool:
    """A masked box kept the date when its digits are the expected digits."""
    return bool(kept) and re.sub(r"\D", "", expected) == re.sub(r"\D", "", kept)


def _words(text) -> list[str]:
    return re.findall(r"[a-z]+|\d+", str(text or "").lower())


def part_of(options: list[dict]) -> str | None:
    """ "month", "day" or "year" for a dropdown that lists only those; None otherwise.

    A prompt option ("Select", "--", empty) is ignored. Months may be names, short names
    or numbers 1 to 12; days are 1 to 31; years are four digits.
    """
    labels = [o.get("label") or "" for o in options]
    real = [w for w in (_words(label) for label in labels) if w and not _prompt(w)]
    if len(real) < 2:
        return None
    if all(len(w) == 1 and w[0].isdigit() and len(w[0]) == 4 for w in real):
        return "year"
    named = [_month_of(w) for w in real]
    if len(real) == 12 and sorted(n for n in named if n) == list(range(1, 13)):
        return "month"
    if all(len(w) == 1 and w[0].isdigit() and 1 <= int(w[0]) <= 31 for w in real):
        return "day" if len(real) >= 28 else None
    return None


def _prompt(words: list[str]) -> bool:
    return words[0] in {"select", "choose", "pick", "please", "month", "day", "year"}


def _month_of(words: list[str]) -> int | None:
    """The month an option names: a name, a short name, a number, or a number and a name."""
    found = set()
    for w in words:
        if w in MONTH_WORDS:
            found.add(MONTH_WORDS[w])
        elif w.isdigit() and 1 <= int(w) <= 12:
            found.add(int(w))
        else:
            return None
    return found.pop() if len(found) == 1 else None


def option_for(options: list[dict], value) -> dict | None:
    """The one option of a month, day or year dropdown for the date the value names."""
    part, parts = part_of(options), parse(value)
    if part is None or parts is None:
        return None
    year, month, day = parts
    if part == "day" and day is None:
        return None
    wanted = {"year": year, "month": month, "day": day}[part]
    matches = []
    for option in options:
        words = _words(option.get("label"))
        if not words or _prompt(words):
            continue
        if part == "month":
            hit = _month_of(words) == wanted
        else:
            hit = len(words) == 1 and words[0].isdigit() and int(words[0]) == wanted
        if hit:
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def question_label(label: str) -> str | None:
    """The date question a month, day or year dropdown is part of, named as a date.

    "Graduation month" and "Expected graduation date — Year" both read as the graduation
    date question, so the approved date can be looked up for it. None for a bare "Month".
    """
    words = [w for w in re.findall(r"[a-z]+", str(label or "").lower())]
    kept = [w for w in words if w not in {"month", "day", "year", "mm", "dd", "yy", "yyyy"}]
    if not kept or kept == words:
        return None
    if "date" not in kept:
        kept.append("date")
    return " ".join(kept)
