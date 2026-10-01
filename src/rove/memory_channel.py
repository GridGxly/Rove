"""`#memory`: the owner's plain-language window onto what Rove remembers.

The channel is an interface, not the store. Remembered answers live in SQLite
(`answer_memory`) and are mirrored to `Answers.md` in the vault. Approved profile facts
are read-only here: this module never writes the profile. Every owner message gets one
short reply in words, without ids, keys or hashes. Parsing is keyword and number
matching in code; no model reads this channel and nothing typed here reaches one.

The list opens with what the approved profile holds, unnumbered, then the remembered
answers, numbered because only they can be changed here.

Replies and the "Saved: ..." lines for answers learned in application threads go
through a small outbox, so a Discord outage neither loses a line nor posts it twice.

`backfill_from_applications` is a one-time helper, never called by the tick: it saves
the answers the owner typed for applications prepared before answers were remembered.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime

import httpx

from . import form_reading, workflow
from .discord_feed import discord
from .live_browser import normalized, resolve_known
from .onboarding import read_approved
from .runtime import state_root

HELP_LINE = (
    "Ask `what do you know`, name a question (`relocation`), or say `forget 3`, "
    "`change 3 to No`, or `remember: question = answer`."
)
EMPTY_ANSWERS = "No answers saved yet. I save each answer you give in an application thread."
PRIVATE_HINT = "The ones marked saved are private; name one to see it."
PAGE_ITEMS = 12
PAGE_CHARS = 1700
MATCH_LIMIT = 6

# Answers that are shown as "saved" in a list and spelled out only when named.
SENSITIVE = re.compile(
    r"authori[sz]|sponsor|\bvisa\b|citizen|immigration|clearance|criminal|convict|felon"
    r"|misdemeanor|arrest|background check|gender|\bsex\b|\brace\b|ethnic|hispanic|latino"
    r"|veteran|disabilit|sexual orientation|pronoun|self.?identif|religio|\bage\b"
    r"|date of birth|at least 18|18 years|export control",
    re.IGNORECASE,
)
# Identity and credential steps stay manual; an answer to one is never kept.
MANUAL_ONLY = re.compile(
    r"social security|\bssn\b|passport|bank account|routing number|verification code"
    r"|driver.?s licen[sc]e|password|passcode|security code|one.?time code|card number",
    re.IGNORECASE,
)
GROUPS = (
    (
        "Eligibility",
        re.compile(
            r"authori[sz]|sponsor|\bvisa\b|citizen|clearance|export control|legally|at least 18",
            re.IGNORECASE,
        ),
    ),
    (
        "Background",
        re.compile(
            r"criminal|convict|felon|background check|gender|\brace\b|ethnic|hispanic|veteran"
            r"|disabilit|orientation|pronoun|self.?identif",
            re.IGNORECASE,
        ),
    ),
    (
        "Location",
        re.compile(
            r"relocat|commut|on.?site|in.?person|hybrid|remote|located|\bbased\b|office",
            re.IGNORECASE,
        ),
    ),
    (
        "Availability",
        re.compile(
            r"availab|start date|when can you start|\bmonths?\b|hours|full.?time|part.?time"
            r"|graduat|semester|\bterm\b",
            re.IGNORECASE,
        ),
    ),
    ("Pay", re.compile(r"salary|compensation|\bpay\b|hourly|wage", re.IGNORECASE)),
)
# Words that carry no topic in a question the owner types.
STOP = frozenset(
    re.findall(
        r"[a-z]+",
        "a about all am an and answer answered answers any anything are as ask asked asks at be "
        "by can could did do does for form forms from has have how i if in is it know me mine my "
        "of on or our please question questions remember remembered said save saved say should "
        "show tell that the there they this to us was we what whats when which will with would "
        "you your yours",
    )
)
LIST_WORDS = re.compile(
    r"list(?: all| everything)?|answers|everything|all"
    r"|show(?: me)?(?: all| everything| the list)?"
    r"|what (?:do|did) you (?:know|remember|have)(?: about me| so far)?"
    r"|what have you (?:saved|learned|got)(?: so far)?"
)
ABOUT = re.compile(
    r"what (?:do|did) you (?:know|remember|have)(?: saved)? about (.+)", re.IGNORECASE
)


class Reply(Exception):
    """A request that cannot apply; the message is the one plain line the owner gets."""


def db():
    conn = workflow.db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS memory_outbox(
        id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, content TEXT NOT NULL,
        created_at TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
      CREATE TABLE IF NOT EXISTS memory_listing(
        id INTEGER PRIMARY KEY CHECK(id=1), questions TEXT NOT NULL, shown INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS memory_announced(
        question TEXT PRIMARY KEY, value_hash TEXT NOT NULL);
    """)
    return conn


# ---------------------------------------------------------------------------
# The answer store, reached only through these helpers. `workflow.remember_answer`,
# `recall_answer`, `forget_answer` and `remembered_answers` are the interface. An answer is
# kept under what its question means (see questions.py), so two wordings of a question
# code knows are one answer.
# ---------------------------------------------------------------------------


def stored() -> list[dict]:
    """Remembered answers, one per question, the newest answer to each."""
    rows, seen = [], set()
    answers = sorted(
        workflow.remembered_answers(), key=lambda r: str(r.get("created_at", "")), reverse=True
    )
    for row in answers:
        # One line per question as the owner names it: an answer kept for one employer
        # and his general answer to the same question are the same line, newest first.
        question = workflow.question_fingerprint(row["label"]) or row["key"]
        if question in seen:
            continue
        seen.add(question)
        options = row.get("options") or []
        if isinstance(options, str):
            options = json.loads(options)
        rows.append(
            {
                "question": question,
                "label": str(row["label"]),
                "options": [str(o) for o in options],
                "value": str(row["value"]),
                "created_at": str(row.get("created_at", "")),
                # Code's own class for the question, or a private word in its label.
                "sensitive": bool(row.get("sensitive")) or bool(SENSITIVE.search(row["label"])),
            }
        )
    return rows


def store(label: str, options: list, value: str, message_id: str):
    """Keep an answer the owner gave here: his general answer, whatever the employer."""
    if not workflow.remember_answer(label, options, value, f"memory:{message_id}"):
        raise Reply(
            "That is one I ask you every time a form brings it up, so I do not keep an "
            "answer to it."
        )
    mark_announced(workflow.question_fingerprint(label), workflow.recall_answer(label) or value)


def erase(question: str, keep=()):
    """Remove every stored answer to one question, then refresh the vault mirror."""
    for row in workflow.remembered_answers():
        same = (workflow.question_fingerprint(row["label"]) or row["key"]) == question
        if same and row["key"] not in keep:
            workflow.forget_answer(key=row["key"])
    try:
        from . import vault

        vault.sync_answers()
    except Exception as error:  # noqa: BLE001 -- the readable copy never blocks the change
        workflow.delivery_failed("vault", "answers", error)


def use_counts() -> Counter:
    """How many prepared applications each remembered answer filled, read from their packages."""
    counts: Counter = Counter()
    for path in (state_root() / "applications").glob("*/package.json"):
        try:
            filled = json.loads(path.read_text()).get("filled", [])
        except (OSError, ValueError, AttributeError):
            continue
        for field in filled if isinstance(filled, list) else []:
            if isinstance(field, dict) and field.get("source") == "your earlier answer":
                counts[workflow.question_fingerprint(field.get("label"))] += 1
    return counts


# ---------------------------------------------------------------------------
# Approved profile facts: readable here, never written here.
# ---------------------------------------------------------------------------


def yes_no(value) -> str | None:
    return None if value is None else ("Yes" if value else "No")


def profile_name(profile: dict) -> str | None:
    identity = profile["identity"]
    parts = [identity[k] for k in ("legal_first_name", "legal_middle_name", "legal_last_name")]
    return " ".join(p for p in parts if p) or None


def school_fact(key: str):
    def read(profile: dict):
        schools = profile["education"]["schools"]
        value = schools[0].get(key) if len(schools) == 1 else None
        if key == "graduation_month" and value:
            return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC).strftime("%B %Y")
        return value

    return read


def sponsorship(profile: dict) -> str | None:
    now, future = (profile["eligibility"][k] for k in ("sponsorship_now", "sponsorship_future"))
    if now is None and future is None:
        return None
    if now is False and future is False:
        return "No, not now and not in the future"
    return f"now: {yes_no(now) or 'not set'}, in the future: {yes_no(future) or 'not set'}"


def location(profile: dict) -> str | None:
    identity = profile["identity"]
    place = ", ".join(p for p in (identity["city"], identity["state_region"]) if p)
    return place or identity["country"] or None


def link_words(url) -> str:
    """A link as words Discord will not unfurl: no scheme, no `www.`, no trailing slash."""
    text = re.sub(r"^https?://(?:www\.)?", "", str(url or "").strip(), flags=re.IGNORECASE)
    return text.rstrip("/")


def link_fact(key: str):
    return lambda profile: link_words(profile["identity"][key]) or None


def work_authorization(profile: dict) -> str | None:
    return yes_no(profile["eligibility"]["us_work_authorized"])


def citizenship(profile: dict) -> str | None:
    return yes_no(profile["eligibility"]["us_citizen"])


# (name the owner reads, what in a message names it, how to read it)
PROFILE_FACTS = (
    ("Name", r"\bname\b", profile_name),
    ("Email", r"\be-?mail\b", lambda p: p["identity"]["email"]),
    ("Phone", r"\bphone\b|\bmobile\b|\bcell\b", lambda p: p["identity"]["phone"]),
    ("School", r"\bschool\b|\buniversity\b|\bcollege\b", school_fact("school")),
    ("Major", r"\bmajor\b|field of study", school_fact("major")),
    ("Degree", r"\bdegree\b", school_fact("degree")),
    ("Graduation", r"\bgraduat", school_fact("graduation_month")),
    ("Location", r"\blocation\b|\bcity\b|where (?:do )?i live|where i am based", location),
    ("LinkedIn", r"linked ?in", link_fact("linkedin")),
    ("GitHub", r"git ?hub", link_fact("github")),
    ("Portfolio", r"portfolio|\bwebsite\b", link_fact("portfolio")),
    (
        "Work authorization",
        r"authori[sz]|(?:eligible|allowed|permitted|right) to work",
        work_authorization,
    ),
    ("Visa sponsorship", r"sponsor|\bvisa\b", sponsorship),
    ("Citizenship", r"citizen", citizenship),
)
# Shown as "saved" in the list; spelled out only when the owner names one.
PRIVATE_PROFILE_FACTS = (
    ("work authorization", work_authorization),
    ("visa sponsorship", sponsorship),
    ("citizenship", citizenship),
)


def approved_profile() -> dict | None:
    try:
        return read_approved()["profile"]
    except (ValueError, OSError, KeyError):
        return None


def profile_facts(text: str) -> list[dict]:
    """Approved profile facts a message names, with their values."""
    profile = approved_profile()
    if not profile:
        return []
    facts = []
    for name, pattern, read in PROFILE_FACTS:
        if re.search(pattern, text, re.IGNORECASE):
            value = read(profile)
            if value not in (None, ""):
                facts.append({"name": name, "value": str(value)})
    return facts


def profile_answers(question: str) -> dict | None:
    """The profile fact that already answers this question on a form, if there is one."""
    if len(keywords(question)) <= 2:
        named = profile_facts(question)
        if named:
            return named[0]
    profile = approved_profile()
    if not profile:
        return None
    try:
        value, source = resolve_known(question, profile)
    except (KeyError, TypeError, ValueError):
        return None
    if value in (None, "") or str(source or "").startswith("default."):
        return None
    return {"name": f"“{words(question)}”", "value": str(value)}


def read_only(fact: dict) -> str:
    return (
        f"{fact['name']} comes from your approved profile, so I do not change it here. "
        "Changing it goes through the profile flow."
    )


def school_line(school: dict) -> str:
    study = " in ".join(shown(x, 50) for x in (school.get("degree"), school.get("major")) if x)
    parts = [shown(school.get("school"), 60), study]
    month = school.get("graduation_month")
    if month:
        when = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        today = datetime.now(UTC)
        ahead = (when.year, when.month) >= (today.year, today.month)
        parts.append(f"{'graduating' if ahead else 'graduated'} {when:%B %Y}")
    return "School: " + " · ".join(part for part in parts if part)


def profile_section() -> tuple[list[str], bool]:
    """The top of the list: what the approved profile holds, one short line per kind.

    Read-only and unnumbered. Eligibility facts are named and shown as "saved"; the
    second value says whether any were, so the list can explain how to see them.
    """
    profile = approved_profile()
    if not profile:
        return ["**From your profile**", "I could not read your approved profile just now."], False
    identity = profile["identity"]
    lines = ["**From your profile** (read-only here)"]
    if profile_name(profile):
        lines.append(f"Name: {shown(profile_name(profile), 80)}")
    lines += [school_line(school) for school in profile["education"]["schools"][:2]]
    if location(profile):
        lines.append(f"Location: {shown(location(profile), 80)}")
    links = [link_words(identity[key]) for key in ("linkedin", "github", "portfolio")]
    if any(links):
        lines.append("Links: " + " · ".join(shown(link, 50) for link in links if link))
    private = [name for name, read in PRIVATE_PROFILE_FACTS if read(profile) is not None]
    if private:
        lines.append(f"{', '.join(private).capitalize()}: saved")
    if len(lines) == 1:
        lines.append("Nothing is approved there yet.")
    return lines, bool(private)


# ---------------------------------------------------------------------------
# Words in, words out.
# ---------------------------------------------------------------------------


def plain(text) -> str:
    """Text from a form, on one line, that Discord will not render as markup or a link."""
    text = re.sub(r"[*`]", " ", str(text or "")).replace("](", "] (")
    return " ".join(text.split())


def words(label, limit: int = 90) -> str:
    """A form's question as the owner reads it: no required marks, no markup."""
    text = re.sub(r"\((?:optional|required)\)", " ", str(label or ""), flags=re.IGNORECASE)
    return workflow.clip(plain(text), limit) or "that question"


def shown(value, limit: int = 160) -> str:
    return workflow.clip(plain(value), limit) or "—"


def told(created_at: str) -> str:
    """When the owner gave the answer, as a date he would say."""
    try:
        when = datetime.fromisoformat(created_at).astimezone()
    except ValueError:
        return "you told me"
    year = "" if when.year == datetime.now(UTC).astimezone().year else f", {when.year}"
    return f"you told me on {when:%b} {when.day}{year}"


def keywords(text: str) -> list[str]:
    return [w for w in normalized(str(text)).split() if len(w) > 1 and w not in STOP]


def near(a: str, b: str) -> bool:
    """Two words name the same thing: equal, or one stem (`relocation`, `relocate`)."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter < 4:
        return False
    stem = max(4, shorter - 3)
    return a[:stem] == b[:stem]


def find(text: str, rows: list[dict], strict: bool = False) -> list[dict]:
    """Remembered answers whose question carries the words of the text.

    All words must be there. When no question has them all and the search is not
    `strict` (a change or a removal always is), the questions with the most of them are
    returned, as long as that is at least half of the words.
    """
    exact = workflow.question_fingerprint(text)
    for row in rows:
        if row["question"] == exact:
            return [row]
    wanted = keywords(text)
    if not wanted:
        return []
    scored = []
    for row in rows:
        have = normalized(row["label"]).split()
        scored.append((sum(any(near(w, h) for h in have) for w in wanted), row))
    full = [row for score, row in scored if score == len(wanted)]
    if full or strict:
        return full
    best = max((score for score, _ in scored), default=0)
    if not best or best * 2 < len(wanted):
        return []
    return [row for score, row in scored if score == best]


def group_of(row: dict) -> str:
    return next((name for name, pattern in GROUPS if pattern.search(row["label"])), "Other")


def line(number: int | None, row: dict, reveal: bool, origin: bool = False) -> str:
    value = shown(row["value"]) if reveal or not row["sensitive"] else "saved"
    text = f"{words(row['label'])} → {value}"
    if origin:
        text += " · " + told(row["created_at"])
    return f"{number}. {text}" if number else text


def fit(value: str, options: list[str]) -> str:
    """A new value for a question with options must be one of them, as the form words it."""
    choices = [o for o in options if o.strip() and not o.strip().startswith("-")]
    if not choices:
        return value
    match = next((o for o in choices if normalized(o) == normalized(value)), None)
    if match is None:
        raise Reply(
            "That is not one of the options this question has: "
            + " / ".join(shown(o, 40) for o in choices[:8])
        )
    return match


# ---------------------------------------------------------------------------
# The numbered list the owner last saw; `forget 3` and `more` refer to it.
# ---------------------------------------------------------------------------


def save_listing(questions: list[str], shown_count: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_listing VALUES(1,?,?)",
            (json.dumps(questions), shown_count),
        )


def load_listing() -> tuple[list[str], int]:
    with db() as conn:
        row = conn.execute("SELECT questions,shown FROM memory_listing WHERE id=1").fetchone()
    return (json.loads(row["questions"]), int(row["shown"])) if row else ([], 0)


def headings(rows: list[dict]) -> list[str]:
    """The heading each row sits under in one message: its topic when another row
    shares it, "Other" when it would stand alone."""
    counts = Counter(group_of(row) for row in rows)
    return [group_of(row) if counts[group_of(row)] > 1 else "Other" for row in rows]


def ordered(rows: list[dict]) -> list[dict]:
    """Most used first, newest first among equals. Each message's worth is then kept
    together by what it is about, so the most used answers are always on the first one."""
    uses = use_counts()
    ranked = sorted(rows, key=lambda r: r["created_at"], reverse=True)
    ranked.sort(key=lambda r: -uses[r["question"]])
    result: list[dict] = []
    for start in range(0, len(ranked), PAGE_ITEMS):
        chunk = ranked[start : start + PAGE_ITEMS]
        groups: dict[str, list[dict]] = {}
        for row, heading in zip(chunk, headings(chunk), strict=True):
            groups.setdefault(heading, []).append(row)
        result += [row for members in groups.values() for row in members]
    return result


def page(order: list[str], start: int, rows: list[dict], lead=(), private: bool = False) -> str:
    """One message of the answers, numbered by place in the whole list, from `start` on.

    `lead` is the profile section above the first message; only the answers carry
    numbers, because only they can be changed here.
    """
    by_question = {row["question"]: row for row in rows}
    lines: list[str] = list(lead)
    if start == 0:
        lines.append(f"**Answers you gave** ({len(order)}, most used first)")
    taken: list[tuple[str, dict]] = []
    size, index = sum(len(x) + 1 for x in lines), start
    while index < len(order) and len(taken) < PAGE_ITEMS:
        row = by_question.get(order[index])
        if row is not None:  # one forgotten since the list was shown keeps its number retired
            entry = line(index + 1, row, reveal=False)
            size += len(entry) + 20  # room for a heading above it
            if taken and size > PAGE_CHARS:
                break
            taken.append((entry, row))
        index += 1
    names = headings([row for _, row in taken])
    current = None
    for (entry, _), name in zip(taken, names, strict=True):
        if len(set(names)) > 1 and name != current:
            lines.append(f"_{name}_")
        lines.append(entry)
        current = name
    left = sum(1 for question in order[index:] if question in by_question)
    save_listing(order, index if left else len(order))
    if private or any(row["sensitive"] for _, row in taken):
        lines.append(PRIVATE_HINT)
    if left:
        lines.append(f"Say `more` for the other {left}.")
    return "\n".join(lines)


def list_all() -> str:
    """What Rove knows, in one message: the profile's facts, then the answers given."""
    lead, private = profile_section()
    rows = ordered(stored())
    if not rows:
        save_listing([], 0)
        return "\n".join([*lead, EMPTY_ANSWERS, *([PRIVATE_HINT] if private else [])])
    return page([row["question"] for row in rows], 0, rows, lead, private)


def more() -> str:
    order, shown_count = load_listing()
    if not order:
        return list_all()
    rows = stored()
    left = {row["question"] for row in rows} & set(order[shown_count:])
    if not left:
        return "That is everything I remember."
    return page(order, shown_count, rows)


def by_number(number: int, rows: list[dict]) -> dict:
    if not rows:
        raise Reply("No answers are saved, so there is nothing to change.")
    order, _ = load_listing()
    if not order:
        raise Reply("I have not shown you a list yet. Say `list` first.")
    if not 1 <= number <= len(order):
        raise Reply(f"There is no {number} in the last list; it goes up to {len(order)}.")
    row = next((r for r in rows if r["question"] == order[number - 1]), None)
    if row is None:
        raise Reply(f"Number {number} is no longer saved. Say `list` for the current list.")
    return row


def pick(text: str, rows: list[dict]) -> dict:
    """The one remembered answer a change or removal names, or a plain line saying why not."""
    text = text.strip().rstrip(".!?").strip()
    number = re.fullmatch(r"#?(\d{1,3})", text)
    if number:
        return by_number(int(number[1]), rows)
    matches = find(text, rows, strict=True)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        matches = matches[:MATCH_LIMIT]
        save_listing([row["question"] for row in matches], len(matches))
        raise Reply(
            "That fits more than one. Say it again with the number:\n"
            + "\n".join(line(n, row, reveal=False) for n, row in enumerate(matches, start=1))
        )
    facts = profile_facts(text)
    if facts:
        raise Reply(read_only(facts[0]))
    if not keywords(text):
        raise Reply("Tell me which one, like `forget 3` or `forget relocation`.")
    raise Reply("I have nothing saved about that. Say `list` to see what I remember.")


# ---------------------------------------------------------------------------
# What each owner message does.
# ---------------------------------------------------------------------------


def ask(text: str) -> str | None:
    """What is saved about the words of a message, or None when nothing is."""
    rows = stored()
    # A private answer is spelled out only when every word of the message names it.
    named = bool(find(text, rows, strict=True))
    matches = find(text, rows)
    facts = profile_facts(text)
    if not matches and not facts:
        return None
    lines: list[str] = []
    if len(matches) == 1:
        lines.append(line(None, matches[0], reveal=named, origin=True))
    elif matches:
        extra = len(matches) - MATCH_LIMIT
        matches = matches[:MATCH_LIMIT]
        save_listing([row["question"] for row in matches], len(matches))
        lines += [line(n, row, reveal=named, origin=True) for n, row in enumerate(matches, start=1)]
        if extra > 0:
            lines.append(f"…and {extra} more; say `list` to see everything.")
    for fact in facts:
        lines.append(
            f"{fact['name']} → {shown(fact['value'])} · from your approved profile "
            "(it changes through the profile flow, not here)"
        )
    return "\n".join(lines)


def about(topic: str) -> str:
    """`what do you know about <topic>`: the plain topic query, with its own empty line."""
    return ask(topic) or (
        f"I have nothing about “{shown(topic, 60)}” yet, in your profile or in the "
        "answers you gave."
    )


def forget(target: str) -> str:
    row = pick(target, stored())
    erase(row["question"])
    with db() as conn:
        conn.execute("DELETE FROM memory_announced WHERE question=?", (row["question"],))
    return f"Forgot “{words(row['label'])}”. I will ask you the next time a form needs it."


def apply_change(row: dict, value: str, message_id: str) -> str:
    value = value.strip()
    if not value or value.lower() == "skip":
        raise Reply("Give me the new answer, like `change 3 to No`. To drop it, say `forget 3`.")
    value = fit(value, row["options"])
    store(row["label"], row["options"], value, message_id)
    # Other wordings and option sets of the same question would keep the old answer.
    erase(
        row["question"],
        keep={workflow.question_fingerprint(row["label"], row["options"]), row["question"]},
    )
    return f"Updated: when a form asks “{words(row['label'])}”, I now answer {shown(value)}."


def change(target: str, value: str, message_id: str) -> str:
    return apply_change(pick(target, stored()), value, message_id)


def change_phrase(body: str, message_id: str) -> str:
    """`change <which> to <value>`. Either side may contain " to ": the split is the first
    one that names a single saved answer and leaves a value its question accepts."""
    splits = list(re.finditer(r"\s+to\s+", body, re.IGNORECASE))
    if not splits:
        raise Reply("Say it like `change 3 to No` or `relocation: No`.")
    rows = stored()
    chosen = None
    for split in splits:
        head, value = body[: split.start()].strip(), body[split.end() :].strip()
        number = re.fullmatch(r"#?(\d{1,3})", head)
        try:
            found = [by_number(int(number[1]), rows)] if number else find(head, rows, strict=True)
        except Reply:
            found = []
        if len(found) != 1:
            continue
        chosen = chosen or split
        try:
            fit(value, found[0]["options"])
        except Reply:
            continue
        chosen = split
        break
    chosen = chosen or splits[0]
    return change(body[: chosen.start()], body[chosen.end() :], message_id)


def remember(question: str, answer: str, message_id: str) -> str:
    question, answer = " ".join(question.split()), answer.strip()
    if MANUAL_ONLY.search(question):
        return "I do not keep that kind of answer. Identity and security steps stay with you."
    if not question or not answer or answer.lower() == "skip":
        raise Reply("Say it like `remember: question = answer`.")
    if len(question) > 300 or len(answer) > 2000:
        raise Reply("That is too long to keep as one answer.")
    rows = stored()
    known = find(question, rows, strict=True)
    exact = workflow.question_fingerprint(question)
    if any(row["question"] == exact for row in known) or (
        len(known) == 1 and len(keywords(question)) <= 2
    ):
        return apply_change(known[0], answer, message_id)
    fact = profile_answers(question)
    if fact:
        return read_only(fact)
    store(question, [], answer, message_id)
    return (
        f"Saved: when a form asks “{words(question)}”, I answer {shown(answer)}. "
        "I use it on forms that word the question the same way."
    )


def respond(text: str, message_id: str) -> str:
    """One reply for one owner message. A `Reply` carries the plain line to send."""
    raw = " ".join(str(text).replace("`", " ").split())
    raw = re.sub(r"^(?:<@[!&]?\d+>\s*)+", "", raw)  # a leading mention of the bot is not a word
    low = raw.lower().rstrip(".!?").strip()
    if not low or low in {"help", "commands"}:
        return HELP_LINE
    if LIST_WORDS.fullmatch(low):
        return list_all()
    if low in {"more", "next", "show more"}:
        return more()
    match = ABOUT.fullmatch(raw.rstrip(".!?").strip())
    if match:
        return about(match[1])
    match = re.fullmatch(r"remember\b(?: that)?\s*[:,]?\s*(.+?)\s*=\s*(.*)", raw, re.IGNORECASE)
    if match:
        return remember(match[1], match[2], message_id)
    if re.match(r"remember\b", low):
        raise Reply("Say it like `remember: question = answer`.")
    match = re.fullmatch(r"(?:forget|remove|delete|drop)\s+(?:about\s+)?(.+)", raw, re.IGNORECASE)
    if match:
        return forget(match[1])
    match = re.fullmatch(r"(?:change|update|set|correct)\s+(.+)", raw, re.IGNORECASE)
    if match:
        return change_phrase(match[1], message_id)
    match = re.fullmatch(r"(.+?)\s*[:=]\s*(.+)", raw)
    if match and not raw.endswith("?"):
        return change(match[1], match[2], message_id)
    return ask(raw) or HELP_LINE


# ---------------------------------------------------------------------------
# One-time backfill: answers the owner typed for applications prepared before answers
# were remembered. Never part of the tick; someone runs it once, by hand.
# ---------------------------------------------------------------------------

# Longer than this is writing for one employer, not a fact to reuse on another form.
BACKFILL_VALUE_CHARS = 200
# A label that is itself an answer is an option's text, not a question.
ANSWER_WORDS = frozenset(
    {"yes", "no", "true", "false", "n a", "na", "none", "other", "select", "on", "off"}
    | {"agree", "i agree", "disagree", "decline", "ok", "maybe", "not sure"}
)


def past_question(application_id: str, field_key: str) -> dict | None:
    """The question a past answer was given to: from the application's last form
    observation, else from the thread's own record of the answer."""
    path = state_root() / f"applications/{application_id}/observation.json"
    try:
        fields = json.loads(path.read_text()).get("fields", [])
    except (OSError, ValueError, AttributeError):
        fields = []
    for field in fields if isinstance(fields, list) else []:
        if isinstance(field, dict) and field.get("key") == field_key:
            return {
                "label": str(field.get("label") or ""),
                "options": [
                    str(o.get("label", "")) if isinstance(o, dict) else str(o)
                    for o in field.get("options") or []
                ],
                "kind": field.get("kind"),
                "label_missing": bool(field.get("label_missing")),
            }
    with workflow.db() as conn:
        events = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? "
            "AND kind='owner_answer' ORDER BY id DESC",
            (application_id,),
        ).fetchall()
    for event in events:
        data = json.loads(event["data"])
        if data.get("field_key") == field_key and data.get("label"):
            return {"label": str(data["label"]), "options": [], "kind": None}
    return None


def backfill_problem(question: dict | None, value: str) -> str | None:
    """Why a past answer is not kept, in a few words; None when it should be."""
    if not value or value.lower() == "skip":
        return "blank or skipped"
    if question is None or form_reading.unreadable(question):
        return "question could not be read"
    label = question["label"]
    name = normalized(label)
    choices = {normalized(option) for option in question["options"]}
    if (
        name in ANSWER_WORDS
        or name == normalized(value)
        or name in choices
        or len(re.findall(r"[a-z]", name)) < 3
    ):
        return "label is an answer, not a question"
    if (
        question.get("kind") in {"password", "file", "hidden"}
        or SENSITIVE.search(label)
        or MANUAL_ONLY.search(label)
    ):
        return "private or manual"
    if len(value) > BACKFILL_VALUE_CHARS:
        return "long written answer"
    if profile_answers(label):
        return "answered by your profile"
    return None


def backfill_from_applications(include_setup_replies: bool = False) -> dict:
    """Save the answers the owner typed for past applications as remembered answers.

    Only a value the owner typed himself counts: a row whose message is recorded as his
    `answer` reply. An approved or auto-used Qwen draft, and a blank left on an optional
    field, are never taken. Rows entered as setup replies have no such record and are
    left out unless asked for. Private questions, questions that could not be read,
    long written answers and anything the profile already answers are skipped; an answer
    already remembered is never overwritten; the latest answer to a question wins.

    Saving goes through `workflow.remember_answer`, so the vault mirror follows. Nothing
    is posted to `#memory`: the answers show up in the list. Returns the count, the
    questions saved in plain words, and how many rows were skipped for each reason.
    """
    with workflow.db() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT a.application_id,a.field_key,a.value,a.owner_message_id "
                "FROM application_answers a JOIN owner_commands c "
                "ON c.message_id=a.owner_message_id AND c.application_id=a.application_id "
                "WHERE c.kind='answer' ORDER BY c.created_at,c.message_id"
            )
        ]
        if include_setup_replies:
            setup = conn.execute(
                "SELECT application_id,field_key,value,owner_message_id "
                "FROM application_answers WHERE owner_message_id LIKE 'codex-owner-reply:%'"
            )
            rows = [*(dict(row) for row in setup), *rows]
    skipped: Counter = Counter()
    latest: dict[str, tuple[dict, str, str]] = {}
    for row in rows:
        question = past_question(row["application_id"], row["field_key"])
        value = str(row["value"]).strip()
        problem = backfill_problem(question, value)
        if problem:
            skipped[problem] += 1
            continue
        fingerprint = workflow.question_fingerprint(question["label"])
        if fingerprint in latest:
            skipped["replaced by a later answer"] += 1
        latest[fingerprint] = (question, value, row["owner_message_id"])
    labels = []
    for fingerprint, (question, value, message_id) in latest.items():
        if workflow.recall_answer(question["label"]) is not None:
            skipped["already saved"] += 1
            continue
        workflow.remember_answer(question["label"], question["options"], value, message_id)
        mark_announced(fingerprint, value)  # old answers are not news for the channel
        labels.append(words(question["label"]))
    return {"saved": len(labels), "labels": labels, "skipped": dict(skipped)}


# ---------------------------------------------------------------------------
# Delivery: an outbox, the channel cursor, and the lines for answers learned elsewhere.
# ---------------------------------------------------------------------------


def value_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def mark_announced(question: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_announced VALUES(?,?)", (question, value_hash(value))
        )


def enqueue(key: str, content: str):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO memory_outbox(key,content,created_at) VALUES(?,?,?)",
            (key, content, workflow.now()),
        )


def learned_line(row: dict, changed: bool) -> str:
    if row["sensitive"]:
        return f"Saved your answer to “{words(row['label'])}”. Name it here to see it."
    if changed:
        return f"Updated: when a form asks “{words(row['label'])}”, I now answer {shown(row['value'])}."
    return f"Saved: when a form asks “{words(row['label'])}”, I answer {shown(row['value'])}."


def announce_learned(quiet: bool = False):
    """Queue one line for each answer learned or changed since the last pass.

    The line and the note that it was said are written together, so a crash repeats
    nothing. `quiet` records what is already known without a line: the first pass over a
    channel must not replay everything Rove ever learned.
    """
    rows = sorted(stored(), key=lambda r: r["created_at"])
    with db() as conn:
        said = {
            r["question"]: r["value_hash"]
            for r in conn.execute("SELECT question,value_hash FROM memory_announced")
        }
        for row in rows:
            current = value_hash(row["value"])
            if said.get(row["question"]) == current:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO memory_announced VALUES(?,?)", (row["question"], current)
            )
            if not quiet:
                # The note above, written in the same transaction, is what makes this
                # line happen once; the key only has to be unique.
                moment = workflow.now()
                conn.execute(
                    "INSERT OR IGNORE INTO memory_outbox(key,content,created_at) VALUES(?,?,?)",
                    (
                        f"learned:{row['question']}:{moment}",
                        learned_line(row, changed=row["question"] in said),
                        moment,
                    ),
                )
        for question in said.keys() - {row["question"] for row in rows}:
            # Forgotten: learning it again later is news again.
            conn.execute("DELETE FROM memory_announced WHERE question=?", (question,))


def flush(channel: str):
    """Deliver waiting lines in order; a failure keeps the rest for the next pass."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id,content FROM memory_outbox WHERE delivery='pending' ORDER BY id"
        ).fetchall()
    for row in rows:
        try:
            sent = discord(
                "POST",
                f"/channels/{channel}/messages",
                {
                    "content": workflow.clip(row["content"], 1900),
                    "allowed_mentions": {"parse": []},
                    "nonce": f"memory:{row['id']}",
                    "enforce_nonce": True,
                },
            )
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("memory", row["id"], error)
            return
        with db() as conn:
            # The words were delivered; the outbox keeps the fact, not a copy of them.
            conn.execute(
                "UPDATE memory_outbox SET delivery='sent', content='', message_id=? WHERE id=?",
                (str((sent or {}).get("id", "")), row["id"]),
            )


def handle(message_id: str, text: str):
    """Act on one owner message exactly once and queue its one reply."""
    key = f"reply:{message_id}"
    with db() as conn:
        if conn.execute("SELECT 1 FROM memory_outbox WHERE key=?", (key,)).fetchone():
            return
    try:
        reply = respond(text, str(message_id))
    except Reply as error:
        reply = str(error)
    except Exception as error:  # noqa: BLE001 -- one bad message must not stall the channel
        workflow.system_line("memory", f"memory command failed · {type(error).__name__}")
        reply = "That did not work and nothing was changed. The detail is in the system log."
    enqueue(key, reply)


def cursor(channel: str):
    with db() as conn:
        return conn.execute(
            "SELECT message_id FROM workflow_checkpoints WHERE channel_id=?", (channel,)
        ).fetchone()


def read(channel: str, owner: str):
    """Handle the owner's new messages and move the channel's cursor past them."""
    checkpoint = cursor(channel)
    # The first pass only establishes a cursor. Old messages are never replayed.
    route = f"/channels/{channel}/messages?limit=100"
    if checkpoint:
        route += "&after=" + checkpoint[0]
    messages = discord("GET", route) or []
    for message in sorted(messages, key=lambda m: int(m["id"])):
        author = message.get("author", {})
        text = message.get("content", "").strip()
        if not checkpoint or author.get("id") != owner or author.get("bot") or not text:
            continue
        handle(str(message["id"]), text)
    if messages or not checkpoint:
        empty_cursor = str((int(datetime.now(UTC).timestamp() * 1000) - 1420070400000) << 22)
        newest = max(messages, key=lambda m: int(m["id"]))["id"] if messages else empty_cursor
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workflow_checkpoints VALUES(?,?)", (channel, str(newest))
            )


def poll(owner: str):
    """One pass over `#memory`. It never raises: memory talk must not stop an application."""
    try:
        if not workflow.config().get("enabled"):
            return
        channel = workflow.ensure_memory_channel()
        if not channel:
            return
        first = cursor(channel) is None
        try:
            read(channel, owner)
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("memory", "read", error)
        announce_learned(quiet=first)
        flush(channel)
    except Exception as error:  # noqa: BLE001 -- reported in the system log, retried next tick
        workflow.system_line("memory", f"memory channel pass failed · {type(error).__name__}")
