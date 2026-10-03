"""Deterministic intake: score each feed job, sort it into a tier, run the daily digest.

No model is called here. The approved profile's preferences and one weight table decide
whether a job is queued, offered to the owner in the digest, or dropped. A score is a
discovery priority, never a claim of eligibility, and feed text is data: it can lower or
raise a number, it cannot approve anything.
"""

import contextlib
import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from . import discord_feed, workflow
from .jobs import identity_key, plain, plain_company, public_link
from .matching import contains
from .runtime import state_root

RULES_VERSION = 1
QUEUE_AT = 70  # this score and above is queued and announced
DIGEST_AT = 35  # this score and above is offered in the daily digest; below it is dropped
SCORE_BAND = 10  # within a band of this many points the newest job goes first
OWNER_PICK = "owner_pick"  # the queue source of a job the owner said yes to in the digest
QUEUE_SORTED = "existing_queue_sorted"  # the mark left by the one-time pass over the queue
HOLD_BRAKE = "hold_brake"  # the mark holding the day the hold brake last engaged

# Every point a job can gain or lose. A title that names no wanted role never reaches
# the digest, whatever else it earns.
WEIGHTS = {
    "role_core": 60,
    "role_adjacent": 35,
    "role_keyword": 35,
    "role_off_field": -40,
    "term_wanted": 10,
    "term_unlisted": 5,
    "term_other": -15,
    "place_preferred": 10,
    "place_remote": 10,
    "place_fine": 5,
    "place_remote_unwanted": -10,
    "place_no_relocation": -20,
    "class_fits": 5,
    "class_differs": -10,
    "posted_within_3_days": 10,
    "posted_within_14_days": 5,
    "posted_over_21_days": -5,
    "company_priority": 15,
    "pay_meets_minimum": 5,
    "pay_below_minimum": -20,
}

# (family, class, what the owner reads, pattern over the plain lower-case title)
ROLE_FAMILIES = (
    (
        "software",
        "core",
        "software role",
        (
            r"software|swe|sde|programmer|developer|full stack|front end|back end"
            r"|web (?:develop\w*|engineer\w*|app\w*)|app(?:lication)? develop\w*"
            r"|computer science"
        ),
    ),
    ("mobile", "core", "mobile role", r"mobile|ios|android"),
    (
        "platform",
        "core",
        "infrastructure software role",
        (
            r"devops|site reliability|sre|platform engineer\w*|cloud engineer\w*"
            r"|cloud (?:develop\w*|software)"
        ),
    ),
    ("data-engineering", "core", "data engineering role", r"data engineer\w*|data platform"),
    ("it", "adjacent", "IT role", r"information technology|information systems"),
    ("testing", "adjacent", "testing role", r"qa|quality assurance|test automation|sdet"),
    ("embedded", "adjacent", "embedded or firmware role", r"firmware|embedded"),
    ("computer-engineering", "adjacent", "computer engineering role", r"computer engineer\w*"),
    ("infrastructure", "adjacent", "infrastructure role", r"infrastructure|cloud"),
    ("analytics-engineering", "adjacent", "analytics engineering role", r"analytics engineer\w*"),
    ("technology", "adjacent", "general technology role", r"technology"),
)
# Another engineering discipline in the role itself: even a software title loses points.
OTHER_DISCIPLINES = "mechanical|civil|electrical|chemical|hardware|radio|layout"
# Other lines of work. Next to a software role these name the team ("Software Engineer
# Intern, Finance Platform"); next to a neighbouring family they name the job ("Tax
# Technology Intern"), which is then not a wanted role at all.
OTHER_FIELDS = (
    "tax|audit|accounting|accountant|finance|financial|marketing|sales|legal|paralegal"
    "|industrial|manufacturing|construction|supply chain|human resources|recruiting|business"
    "|forensic|advisory|help desk|product (?:management|manager|marketing|design|analyst)"
)
SENIORITY = r"(?<!rising )senior|sr|staff|principal|director"
GRADUATE = re.compile(
    r"\b(?:MS|M\.S\.?|(?i:phd|ph\.d\.?|mba|doctoral|masters|master['’]s))(?![A-Za-z])"
)
UNDERGRADUATE = re.compile(
    r"\b(?:BS|B\.S\.?|BA|B\.A\.?|(?i:bachelors?|bachelor['’]s|undergrad\w*))(?![A-Za-z])"
)
# The month a term is under way. Late on purpose: a skipped job is never shown, so a
# winter or spring posting is kept until the term has clearly begun.
SEASON_STARTS = {"winter": 12, "spring": 2, "summer": 6, "fall": 9}
SEASON = r"(spring|summer|fall|autumn|winter)"
TERM = re.compile(rf"{SEASON}\W{{0,3}}(20\d\d)|(20\d\d)\W{{0,3}}{SEASON}", re.IGNORECASE)
NON_US = (
    "canada",
    "ontario",
    "quebec",
    "british columbia",
    "alberta",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "united kingdom",
    "england",
    "london",
    "ireland",
    "germany",
    "france",
    "india",
    "mexico",
    "singapore",
    "australia",
    "japan",
    "china",
)
CANADIAN_PROVINCES = {"on", "qc", "bc", "ab", "mb", "sk", "ns", "nb", "nl", "pe"}
US_STATES = frozenset(
    re.findall(
        r"[a-z]{2}",
        "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh "
        "nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc",
    )
)
US_STATE_NAMES = frozenset(
    re.findall(
        r"[a-z]+(?: [a-z]+)*",
        "alabama, alaska, arizona, arkansas, california, colorado, connecticut, delaware, "
        "florida, georgia, hawaii, idaho, illinois, indiana, iowa, kansas, kentucky, louisiana, "
        "maine, maryland, massachusetts, michigan, minnesota, mississippi, missouri, montana, "
        "nebraska, nevada, new hampshire, new jersey, new mexico, new york, north carolina, "
        "north dakota, ohio, oklahoma, oregon, pennsylvania, rhode island, south carolina, "
        "south dakota, tennessee, texas, utah, vermont, virginia, washington, west virginia, "
        "wisconsin, wyoming, district of columbia, united states, usa, us",
    )
)
# Boards that list a job without being the employer's own application page.
AGGREGATORS = ("jobright.ai", "simplify.jobs", "linkedin.com", "indeed.com", "ziprecruiter.com")
# Boards that ask for an account before the form; the owner picks which are worth one.
ACCOUNT_FIRST = ("myworkdayjobs.com", "myworkdaysite.com")
BOARDS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "icims.com": "icims",
    "smartrecruiters.com": "smartrecruiters",
    "eightfold.ai": "eightfold",
    "workable.com": "workable",
    "oraclecloud.com": "oracle",
    "jobvite.com": "jobvite",
    "taleo.net": "taleo",
    "successfactors.com": "successfactors",
    "bamboohr.com": "bamboohr",
    "paylocity.com": "paylocity",
    "applytojob.com": "jazzhr",
}
CLASS_YEARS = {
    "freshman": ("freshman", "freshmen", "first year", "1st year"),
    "sophomore": ("sophomore", "sophomores", "second year", "2nd year"),
    "junior": ("rising junior", "rising juniors"),
    "senior": ("rising senior", "rising seniors"),
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def on_host(url, suffixes) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def title_words(title) -> str:
    """The title in plain lower-case words, with the spellings postings vary made one."""
    text = plain(title)
    for spelled, words in (
        ("fullstack", "full stack"),
        ("frontend", "front end"),
        ("backend", "back end"),
        ("coop", "co op"),
    ):
        text = re.sub(rf"\b{spelled}\b", words, text)
    return text


def segments(title) -> list[str]:
    """A title's parts: the role, and the qualifiers set off by a dash, comma or bracket."""
    parts = re.split(r"\s+[-–—|@]\s+|[,;:()\[\]]", str(title or ""))
    return [part.strip() for part in parts if part.strip()]


def role_family(text) -> tuple | None:
    """(family, class, phrase) for the first role family the text names."""
    words = title_words(text)
    for family, kind, phrase, pattern in ROLE_FAMILIES:
        if re.search(rf"\b(?:{pattern})\b", words):
            return family, kind, phrase
    if re.search(r"\bIT\b", str(text or "")):
        # Upper-case only: the word "it" is not a department.
        return "it", "adjacent", "IT role"
    return None


def role_head(title) -> str:
    """The part of the title that names the role: a core role before a neighbouring one."""
    parts = segments(title)
    named = [(part, role_family(part)) for part in parts]
    for wanted in ("core", "adjacent"):
        for part, family in named:
            if family and family[1] == wanted:
                return part
    return parts[0] if parts else ""


def excluded_kind(title, prefs: dict) -> tuple[str, bool]:
    """(the excluded keyword the title trips, whether it names the role itself).

    "Machine Learning Engineer Intern" is the excluded role. "Software Engineer Intern,
    Backend/Full Stack/ML" only mentions it among other options, so the owner is asked.
    """
    head = role_head(title)
    mention = ""
    for word in prefs.get("excluded_title_keywords") or []:
        if not contains(title, word):
            continue
        if contains(head, word):
            options = head.split("/")
            roles = [option for option in options if role_family(option)]
            listed_apart = (
                len(options) > 1 and roles and not any(contains(role, word) for role in roles)
            )
            if not listed_apart:
                return word, True
        mention = mention or word
    excluded = {plain(word) for word in prefs.get("excluded_title_keywords") or []}
    if not mention and "artificial intelligence" in excluded and re.search(r"\bAI\b", str(title)):
        # A bare "AI" may be the team or the product, not the role: the owner is asked.
        mention = "AI"
    return mention, False


def terms_in(text) -> list[tuple[str, int]]:
    found = []
    for match in TERM.finditer(str(text or "")):
        season = (match[1] or match[4]).lower().replace("autumn", "fall")
        term = (season, int(match[2] or match[3]))
        if term not in found:
            found.append(term)
    return found


def term_words(term) -> str:
    return f"{term[0]} {term[1]}"


def places(location) -> list[str]:
    return [part.strip() for part in re.split(r"[;|\n]", str(location or "")) if part.strip()]


def outside_us(place) -> bool:
    """A place that is clearly abroad. "Ontario, CA" and "London, KY" are American towns."""
    pieces = [plain(piece) for piece in str(place).split(",") if plain(piece)]
    if any(piece in US_STATES or piece in US_STATE_NAMES for piece in pieces[1:]):
        return False
    if pieces and pieces[0] in US_STATE_NAMES:
        return False
    if len(pieces) > 1 and pieces[-1] in CANADIAN_PROVINCES:
        return True
    return any(contains(place, name) for name in NON_US)


def graduation_month(profile: dict) -> str | None:
    schools = (profile.get("education") or {}).get("schools") or []
    return next((s["graduation_month"] for s in schools if s.get("graduation_month")), None)


def studies_at_graduate_level(profile: dict) -> bool:
    schools = (profile.get("education") or {}).get("schools") or []
    return any(
        school.get("currently_enrolled") is not False
        and re.search(
            r"master|\bm\.?s\.?\b|ph\.?d|doctor|\bmba\b",
            str(school.get("degree") or ""),
            re.IGNORECASE,
        )
        for school in schools
    )


def class_year(profile: dict) -> str | None:
    schools = (profile.get("education") or {}).get("schools") or []
    stated = plain(next((s["student_year"] for s in schools if s.get("student_year")), ""))
    for year, spellings in CLASS_YEARS.items():
        if year in stated.split() or any(word in stated for word in spellings):
            return year
    return None


def listed_hourly_pay(title) -> float | None:
    match = re.search(
        r"\$\s?(\d{1,3}(?:\.\d{1,2})?)\s*(?:/|per|an)\s*(?:hr|hour)",
        str(title or ""),
        re.IGNORECASE,
    )
    return float(match[1]) if match else None


def posted_days_ago(posted_at, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(str(posted_at)[:10])).days
    except ValueError:
        return None


def verdict(score: int, notes: list, *, skip: str = "", cap: bool = False, family=None) -> dict:
    """The result every caller reads: a clamped score, its tier, and why in plain phrases."""
    score = max(0, min(100, score))
    if skip:
        score = 0
    elif family is None:
        score = min(score, DIGEST_AT - 1)  # a title naming no wanted role is never offered
    elif cap:
        score = min(score, QUEUE_AT - 1)  # the owner's call, however well it scores
    tier = 1 if score >= QUEUE_AT else 2 if score >= DIGEST_AT else 3
    # The role first, then what holds a borderline job back, then what speaks for it.
    lead = [phrase for kind, _points, phrase in notes if kind == "role"]
    doubtful = [n for n in notes if n[0] in ("cap", "unknown") or (n[0] != "role" and n[1] < 0)]
    doubts = [phrase for _kind, _points, phrase in doubtful]
    for_it = [note[2] for note in notes if note[0] != "role" and note not in doubtful]
    ordered = lead + (doubts + for_it if tier > 1 else for_it + doubts)
    return {
        "score": score,
        "tier": tier,
        "skip": skip,
        "family": family,
        "reasons": ordered,
        "reason": " · ".join(ordered[:3]),
    }


def score_job(job: dict, profile: dict, *, today: date | None = None) -> dict:
    """Score one feed job 0–100 against the approved profile's preferences.

    A hard rule (an excluded kind of role or company, a graduate or senior role, a term
    that already started, a place outside the US or on the owner's excluded list, a track
    the owner did not ask for) skips the job outright. Everything else adds or removes
    points from WEIGHTS, and some facts cap the job at the digest so the owner decides.
    """
    today = today or datetime.now().astimezone().date()
    prefs = profile.get("preferences") or {}
    title, company = str(job.get("title") or ""), str(job.get("company") or "")
    words = title_words(title)
    notes: list[tuple[str, int, str]] = []
    cap = False

    def skip(code: str, phrase: str) -> dict:
        return verdict(0, [("role", 0, phrase)], skip=code)

    def add(kind: str, weight: str, phrase: str = ""):
        notes.append((kind, WEIGHTS[weight], phrase))

    def hold(phrase: str):
        """Cap the job at the digest: a fact the owner should weigh, not a point change."""
        nonlocal cap
        cap = True
        notes.append(("cap", 0, phrase))

    program = job.get("program")
    wanted_programs = prefs.get("programs") or ["internship"]
    if program and program not in wanted_programs:
        return skip("program", f"{program} role, and you asked for {' and '.join(wanted_programs)}")
    for name in prefs.get("excluded_companies") or []:
        if company and contains(company, name):
            return skip("excluded_company", f"{name} is a company you exclude")
    kind_word, names_the_role = excluded_kind(title, prefs)
    if names_the_role:
        return skip("excluded_role", f"a {kind_word} role, which you exclude")
    if re.search(rf"\b(?:{SENIORITY})\b", words):
        return skip("seniority", "a senior role")
    if not studies_at_graduate_level(profile):
        graduate_only = GRADUATE.search(title) and not UNDERGRADUATE.search(title)
        graduate_program = program != "new-grad" and re.search(
            r"(?<!new )(?<!recent )\bgraduate\b", words
        )
        if graduate_only or graduate_program:
            return skip("graduate", "for graduate students")

    family = role_family(role_head(title))
    keyword = next((k for k in prefs.get("title_keywords") or [] if contains(title, k)), None)
    if family:
        add("role", "role_core" if family[1] == "core" else "role_adjacent", family[2])
    elif keyword:
        family = ("keyword:" + plain(keyword), "keyword", f"matches your keyword “{keyword}”")
        add("role", "role_keyword", family[2])
    if family and family[1] == "core":
        off_field = re.search(rf"\b(?:{OTHER_DISCIPLINES})\b", title_words(role_head(title)))
    else:
        off_field = re.search(rf"\b(?:{OTHER_DISCIPLINES}|{OTHER_FIELDS})\b", words)
    if off_field:
        add("field", "role_off_field", f"{off_field[0]} work, outside what you asked for")
    if kind_word:
        hold(f"mentions {kind_word}, which you exclude")

    terms = terms_in(title) or terms_in(job.get("cycle"))
    wanted_terms = [t for cycle in prefs.get("cycles") or [] for t in terms_in(cycle)]
    open_terms = [t for t in terms if date(t[1], SEASON_STARTS[t[0]], 1) > today]
    if terms and not open_terms:
        return skip("term_started", f"{term_words(terms[-1])} has already started")
    if not terms:
        add("unknown", "term_unlisted", "term not listed")
    elif prefs.get("any_cycle") or not wanted_terms:
        add("term", "term_wanted", term_words(open_terms[0]))
    elif any(t in wanted_terms for t in open_terms):
        add("term", "term_wanted", term_words(next(t for t in open_terms if t in wanted_terms)))
    else:
        add("term", "term_other", f"{term_words(open_terms[0])}, not a term you picked")

    spots = places(job.get("location"))
    abroad = all(outside_us(spot) for spot in spots) if spots else outside_us(title)
    if abroad or contains(title, "stagiaire"):
        return skip("location", "outside the United States")
    if spots:
        excluded = prefs.get("excluded_locations") or []
        allowed = [s for s in spots if not any(contains(s, place) for place in excluded)]
        if not allowed:
            return skip("location", f"in {spots[0]}, a place you exclude")
        preferred = next(
            (
                place
                for place in prefs.get("preferred_locations") or []
                if any(contains(spot, place) for spot in allowed)
            ),
            None,
        )
        styles = prefs.get("work_styles") or []
        remote = any(contains(spot, "remote") for spot in allowed)
        if preferred:
            add("place", "place_preferred", f"in {preferred}")
        elif remote and (not styles or "remote" in styles):
            add("place", "place_remote", "remote")
        elif remote and len(allowed) == 1:
            add("place", "place_remote_unwanted", "remote, and you asked for in person")
        elif prefs.get("relocate") is False and prefs.get("preferred_locations"):
            add("place", "place_no_relocation", "not in the places you listed")
        else:
            add("place", "place_fine")

    graduation = graduation_month(profile)
    academic = job.get("academic_eligibility") or {}
    start, end = academic.get("graduation_start"), academic.get("graduation_end")
    if graduation and (start or end):
        if (not start or start <= graduation) and (not end or graduation <= end):
            add("class", "class_fits", "your graduation date fits")
        else:
            add("class", "class_differs", "asks for a different graduation date")
            cap = cap or academic.get("requirement_level") == "required"
    named_years = [
        year
        for year, spellings in CLASS_YEARS.items()
        if any(contains(title, s) for s in spellings)
    ]
    mine = class_year(profile)
    if named_years and mine:
        if mine in named_years:
            add("class", "class_fits", f"meant for {mine} students")
        else:
            add("class", "class_differs", f"meant for {' and '.join(named_years)} students")
            cap = True
    if program == "new-grad" and graduation:
        months_left = (int(graduation[:4]) - today.year) * 12 + int(graduation[5:7]) - today.month
        if months_left > 9:
            hold("full-time role while you are still in school")

    days = posted_days_ago(job.get("posted_at"), today)
    if days is not None and days <= 3:
        when = "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
        add("recency", "posted_within_3_days", f"posted {when}")
    elif days is not None and days <= 14:
        add("recency", "posted_within_14_days")
    elif days is not None and days > 21:
        add("recency", "posted_over_21_days", "posted over three weeks ago")

    if any(contains(company, name) for name in prefs.get("priority_companies") or []):
        add("company", "company_priority", "a company you put first")
    if re.search(r"\b(?:unpaid|volunteer)\b", words):
        if prefs.get("unpaid_roles") is False:
            return skip("pay", "unpaid, which you ruled out")
        if prefs.get("unpaid_roles") is None:
            hold("unpaid")
    hourly, minimum = listed_hourly_pay(title), prefs.get("minimum_hourly_usd")
    if hourly is not None and minimum:
        if hourly >= minimum:
            add("pay", "pay_meets_minimum", f"pays ${hourly:g} an hour")
        else:
            add("pay", "pay_below_minimum", f"pays ${hourly:g} an hour, below your minimum")

    if on_host(job.get("url"), AGGREGATORS):
        hold("the link goes to a job board, not the employer")
    elif on_host(job.get("url"), ACCOUNT_FIRST):
        hold("needs an account before applying")

    total = sum(points for _kind, points, _phrase in notes)
    shown = [(kind, points, phrase) for kind, points, phrase in notes if phrase]
    if not family:
        shown.insert(0, ("role", 0, "not a role you asked for"))
    # "Tax Technology Intern" names a neighbouring family, but the work is another field:
    # it is never offered, however good the company, place and date.
    wanted = family and not (off_field and family[1] != "core")
    return verdict(total, shown, cap=cap, family=family[0] if wanted else None)


def family_key(job: dict) -> str:
    """The same role at the same company for the same term, wherever it is based."""
    title = TERM.sub(" ", str(job.get("title") or ""))
    terms = terms_in(job.get("title")) or terms_in(job.get("cycle"))
    return json.dumps(
        [plain_company(job.get("company")), title_words(title), [list(t) for t in terms]]
    )


# ---------------------------------------------------------------------------
# Decisions: each posting is scored once and remembered by its stable identity
# ---------------------------------------------------------------------------


def ensure_tables(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS intake_decisions(
        identity TEXT PRIMARY KEY, job_id TEXT NOT NULL, family TEXT NOT NULL,
        score INTEGER NOT NULL, tier INTEGER NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL, application_id TEXT,
        decided_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS intake_decisions_family ON intake_decisions(family,tier);
      CREATE INDEX IF NOT EXISTS intake_decisions_status ON intake_decisions(status,score);
      CREATE TABLE IF NOT EXISTS intake_digests(
        day TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'pending', message_id TEXT,
        shown INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS queue_scores(
        application_id TEXT PRIMARY KEY, score INTEGER NOT NULL, reason TEXT NOT NULL,
        basis TEXT NOT NULL, family TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS intake_marks(name TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def mark(name: str) -> str | None:
    """A checkpoint left by a one-time or once-a-day step, or None when it never ran."""
    with db() as conn:
        row = conn.execute("SELECT value FROM intake_marks WHERE name=?", (name,)).fetchone()
    return row["value"] if row else None


def set_mark(name: str, value: str = ""):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO intake_marks VALUES(?,?)", (name, value or workflow.now())
        )


def db():
    conn = workflow.db()
    ensure_tables(conn)
    return conn


# A posting in one of these states has been put in front of the owner or the queue, so
# the same role in another city is a sibling, not news.
HANDLED = ("queued", "digest", "offered", "picked", "declined")
# States the owner never saw: a manual seed may score these again under the current rules.
UNSEEN = ("capped", "dropped", "lapsed")


def compact(job: dict) -> dict:
    keys = ("id", "company", "title", "location", "url", "cycle", "program", "posted_at")
    return {key: job.get(key) for key in keys} | (
        {"source_revision": job["source_revision"]} if job.get("source_revision") else {}
    )


def decide(
    conn, jobs: list[dict], profile: dict, *, today: date | None = None, known_ids=(), revive=()
) -> dict:
    """Score feed jobs that have no decision yet and sort them into tiers.

    Returns the tier-one jobs to announce and queue, plus counts for the system log.
    A job the feed renumbered or rewrote keeps its identity and is counted as a repeat.
    """
    counts: Counter = Counter()
    fresh, seen = [], set()
    for job in jobs:
        identity = identity_key(job)
        if identity in seen:
            counts["repeats"] += 1
            continue
        seen.add(identity)
        row = conn.execute(
            "SELECT status,job_id FROM intake_decisions WHERE identity=?", (identity,)
        ).fetchone()
        if (row and row["status"] not in revive) or (not row and job.get("id") in known_ids):
            counts["repeats"] += 1
            if row and job.get("id") and row["job_id"] != job["id"]:
                # The feed renumbered the posting: follow it, so a later close is noticed.
                conn.execute(
                    "UPDATE intake_decisions SET job_id=? WHERE identity=?", (job["id"], identity)
                )
            continue
        fresh.append((job, identity, score_job(job, profile, today=today)))
    fresh.sort(key=lambda item: (item[2]["score"], item[0].get("posted_at") or ""), reverse=True)
    stamp = workflow.now()
    leaders: dict[str, dict] = {}
    rows, queue = [], []
    for job, identity, result in fresh:
        tier, reason, family = result["tier"], result["reason"], family_key(job)
        status = {1: "queued", 2: "digest", 3: "dropped"}[tier]
        payload = compact(job)
        if tier == 2 and not public_link(job.get("url")):
            tier, status, reason = 3, "dropped", "no link to apply through"
        if tier < 3:
            if family in leaders:
                place = str(job.get("location") or "").strip()
                if place and place not in leaders[family]["also"]:
                    leaders[family]["also"].append(place)
                status = "sibling"
            else:
                best = conn.execute(
                    "SELECT MIN(tier) FROM intake_decisions WHERE family=? AND status IN "
                    f"({','.join('?' * len(HANDLED))})",
                    (family, *HANDLED),
                ).fetchone()[0]
                if best is not None and best <= tier:
                    status = "sibling"
                else:
                    if best is not None:
                        # The better listing of the role is queued; the one waiting for an
                        # answer steps aside, and leaves the live digest if it is on it.
                        retire_lines(conn, family)
                        conn.execute(
                            "UPDATE intake_decisions SET status='sibling',updated_at=? "
                            "WHERE family=? AND status IN ('digest','offered')",
                            (stamp, family),
                        )
                    payload["also"] = []
                    leaders[family] = payload
        counts[
            {"queued": "queued", "digest": "digest", "sibling": "siblings"}.get(status, "dropped")
        ] += 1
        rows.append(
            (identity, job.get("id") or "", family, result["score"], tier, reason, status, payload)
        )
        if status == "queued":
            queue.append((identity, result["score"], reason, payload))
    for identity, job_id, family, score, tier, reason, status, payload in rows:
        conn.execute(
            "INSERT OR REPLACE INTO intake_decisions(identity,job_id,family,score,tier,reason,"
            "status,payload,decided_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                job_id,
                family,
                score,
                tier,
                reason,
                status,
                json.dumps(payload),
                stamp,
                stamp,
            ),
        )
    return {
        "queue": [
            {**payload, "identity": identity, "score": score, "reason": reason}
            for identity, score, reason, payload in queue
        ],
        "counts": dict(counts),
    }


def retire_lines(conn, family: str):
    """Close the open digest lines of a role that has just been queued from another listing."""
    offered = {
        row["identity"]
        for row in conn.execute(
            "SELECT identity FROM intake_decisions WHERE family=? AND status='offered'", (family,)
        )
    }
    if not offered:
        return
    live = conn.execute(
        "SELECT day,data FROM intake_digests WHERE delivery IN ('pending','sent')"
    ).fetchall()
    for row in live:
        data = json.loads(row["data"])
        moved = [
            line for line in data["lines"] if line["identity"] in offered and not line.get("answer")
        ]
        for line in moved:
            line["answer"] = "moved"
        if moved:
            conn.execute(
                "UPDATE intake_digests SET data=?,shown=0 WHERE day=?",
                (json.dumps(data), row["day"]),
            )


def unseen_best(conn, profile: dict, known_ids=(), limit: int = 25) -> list[dict]:
    """The best-scoring open jobs that were never decided or announced, for a manual seed."""
    found = []
    for row in conn.execute("SELECT metadata,revision FROM jobs WHERE active=1").fetchall():
        job = json.loads(row["metadata"])
        if job.get("id") in known_ids:
            continue
        decision = conn.execute(
            "SELECT status FROM intake_decisions WHERE identity=?", (identity_key(job),)
        ).fetchone()
        if decision and decision["status"] not in UNSEEN:
            continue
        result = score_job(job, profile)
        if result["tier"] == 1:
            job["source_revision"] = row["revision"]
            found.append((result["score"], job.get("posted_at") or "", len(found), job))
    found.sort(reverse=True, key=lambda item: item[:2])
    return [item[3] for item in found[:limit]]


def log_counts(counts: dict, capped: int = 0):
    """One system-log line per feed run that decided anything; the owner's channels stay quiet."""
    parts = [
        f"{counts[key]} {label}"
        for key, label in (
            ("queued", "queued"),
            ("digest", "for the digest"),
            ("dropped", "dropped below the bar"),
            ("siblings", "same role elsewhere"),
            ("repeats", "already seen"),
        )
        if counts.get(key)
    ]
    if capped:
        parts.append(f"{capped} left out by the backlog cap")
    if parts:
        workflow.system_line("intake", "feed · " + " · ".join(parts))


def basis(profile_hash: str, today: date | None = None) -> str:
    """What a stored queue score was computed from; a new profile, rule set or day rescores."""
    day = (today or datetime.now().astimezone().date()).isoformat()
    return f"{str(profile_hash or '')[:12]}:{RULES_VERSION}:{day}"


def record_queue_score(
    conn, application_id: str, score: int, reason: str, stamp: str, family: str = ""
):
    conn.execute(
        "INSERT OR REPLACE INTO queue_scores VALUES(?,?,?,?,?)",
        (application_id, int(score), reason, stamp, family),
    )


def rescore_queue(rows, approved: dict) -> list[tuple[str, str]]:
    """Score queued feed jobs for the queue order; return (id, why) for those to park.

    A job is parked when a hard rule now skips it, when its feed listing scores below the
    digest bar, or when the same role at the same company is queued for another place
    with a score at least as good. A queued job with no listing is judged on its title's
    hard rules only.
    """
    profile = approved.get("profile") or {}
    stamp = basis(approved.get("profile_hash", ""))
    parked = []
    with db() as conn:
        for row in rows:
            known = conn.execute(
                "SELECT basis FROM queue_scores WHERE application_id=?", (row["id"],)
            ).fetchone()
            if known and known["basis"] == stamp:
                continue
            listing = conn.execute(
                "SELECT metadata FROM jobs WHERE url IN (?,?) ORDER BY active DESC LIMIT 1",
                (row["url"], row["source_url"]),
            ).fetchone()
            family = ""
            if listing:
                listed = json.loads(listing["metadata"])
                result = score_job(listed, profile)
                park = bool(result["skip"]) or result["score"] < DIGEST_AT
                family = family_key(listed)
            else:
                company, _, title = row["title"].partition(" — ")
                if not title:
                    company, title = "", row["title"]
                result = score_job({"company": company, "title": title}, profile)
                park = bool(result["skip"])
            if park:
                parked.append((row["id"], "excluded by your rules: " + result["reason"]))
            else:
                record_queue_score(
                    conn, row["id"], result["score"], result["reason"], stamp, family
                )
        gone = {application_id for application_id, _ in parked}
        waiting = {row["id"] for row in rows} - gone
        ranked = conn.execute(
            "SELECT s.application_id,s.family,q.status FROM queue_scores s JOIN "
            "application_queue q ON q.id=s.application_id WHERE s.family!='' "
            "ORDER BY s.score DESC,q.created_at"
        ).fetchall()
        # A role already being prepared, waiting on the owner, or sent counts as taken.
        kept = {
            row["family"]
            for row in ranked
            if row["status"] not in ("QUEUED", "DEFERRED") and row["application_id"] not in gone
        }
        for row in ranked:
            if row["application_id"] not in waiting:
                continue
            if row["family"] in kept:
                parked.append(
                    (row["application_id"], "the same role is already queued for another place")
                )
            kept.add(row["family"])
    return parked


def is_feed(source) -> bool:
    name = str(source or "")
    return workflow.SOURCE_ALIASES.get(name, name) == "feed"


def move_borderline(rows, gone: set) -> list[tuple[str, str]]:
    """The one-time sort of a queue filled under the old rules: queued feed jobs whose
    listing now scores in the digest tier leave the queue and wait for the owner's yes.

    Returns (id, why) for the applications to park. Only feed jobs still queued, with a
    listing to score and no decision yet, are moved; `rescore_queue` has just scored them.
    """
    moved = []
    stamp = workflow.now()
    with db() as conn:
        for row in rows:
            if row["id"] in gone or not is_feed(row["source"]):
                continue
            scored = conn.execute(
                "SELECT score,reason,family FROM queue_scores WHERE application_id=?",
                (row["id"],),
            ).fetchone()
            if not scored or not scored["family"]:
                continue  # no feed listing behind it: nothing reliable to sort it by
            if not DIGEST_AT <= scored["score"] < QUEUE_AT:
                continue
            listing = conn.execute(
                "SELECT metadata FROM jobs WHERE url IN (?,?) ORDER BY active DESC LIMIT 1",
                (row["url"], row["source_url"]),
            ).fetchone()
            if not listing:
                continue
            listed = json.loads(listing["metadata"])
            identity = identity_key(listed)
            if conn.execute(
                "SELECT 1 FROM intake_decisions WHERE identity=?", (identity,)
            ).fetchone():
                continue
            # The link as it was queued, so a later yes finds this same application.
            payload = {**compact(listed), "url": row["source_url"], "also": []}
            conn.execute(
                "INSERT INTO intake_decisions(identity,job_id,family,score,tier,reason,status,"
                "payload,application_id,decided_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    listed.get("id") or "",
                    scored["family"],
                    scored["score"],
                    2,
                    scored["reason"],
                    "digest",
                    json.dumps(payload),
                    row["id"],
                    stamp,
                    stamp,
                ),
            )
            moved.append((row["id"], "waiting for your yes on the daily list"))
    return moved


def fit_may_hold(source: str, fit: dict) -> bool:
    """Whether a job-fit review may stop this application for the owner.

    A link the owner pasted is never held. A job the owner picked from the digest is held
    only on a conflict code verified against an approved fact. Everything the owner did
    not choose (feed jobs, links the agent queued) is held as before.
    """
    if not workflow.source_policy(source)["owner_decided"]:
        return True
    if source == OWNER_PICK:
        return fit.get("decision") == "not_fit"
    return False


# ---------------------------------------------------------------------------
# Pacing: the daily cap and the per-platform gap for unattended sending
# ---------------------------------------------------------------------------


def number(settings: dict, key: str, default: int) -> int:
    """A numeric setting; missing, null or unreadable values fall back to the default."""
    value = settings.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def platform_of(url) -> str:
    """The applicant-tracking platform behind a link, or the employer's own host."""
    from . import boards
    from .live_browser import job_scope

    safe = public_link(str(url or ""))
    if not safe:
        return ""
    board = boards.board_for(safe)
    if board:
        return board.NAME  # Paylocity, Workable, JazzHR, BambooHR: each its own gap
    host = (urlsplit(safe).hostname or "").lower()
    if job_scope(safe)[0] == "greenhouse":
        return "greenhouse"
    for suffix, name in BOARDS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host.removeprefix("www.")


def attempt_platform(application_id: str, queue_url) -> str:
    """Where a submission was sent: the prepared form's own page when it is on record."""
    path = state_root() / f"applications/{application_id}/workflow-result.json"
    try:
        page_url = (json.loads(path.read_text()).get("page") or {}).get("url")
    except (OSError, ValueError, AttributeError):
        page_url = None
    return platform_of(page_url) or platform_of(queue_url)


def prepare_pacing(conn):
    """Add the platform column to submission attempts once, and fill it where missing."""
    ensure_tables(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_submission_attempts)")}
    if "platform" not in columns:
        # Another local service may add the column in the same moment; either one will do.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE live_submission_attempts ADD COLUMN platform TEXT")
    rows = conn.execute(
        "SELECT a.application_id,q.url FROM live_submission_attempts a LEFT JOIN "
        "application_queue q ON q.id=a.application_id WHERE a.platform IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE live_submission_attempts SET platform=? WHERE application_id=?",
            (attempt_platform(row["application_id"], row["url"]), row["application_id"]),
        )


def daily_cap_reached(conn, settings: dict, now: datetime) -> bool:
    cap = number(settings, "max_submissions_per_day", 30)
    sent = conn.execute(
        "SELECT COUNT(*) FROM live_submission_attempts WHERE created_at LIKE ?",
        (now.strftime("%Y-%m-%d") + "%",),
    ).fetchone()[0]
    return sent >= cap


def inside_global_gap(conn, settings: dict, now: datetime) -> bool:
    minutes = number(settings, "min_minutes_between_submissions", 0)
    last = conn.execute("SELECT MAX(created_at) FROM live_submission_attempts").fetchone()[0]
    return bool(
        minutes and last and now - datetime.fromisoformat(last) < timedelta(minutes=minutes)
    )


def hold_brake(conn, settings: dict, now: datetime) -> bool:
    """Whether feed jobs wait until tomorrow: enough of them stopped for the owner today.

    Unattended runs no longer stop at the first holds, so this keeps one day from
    filling action-needed. Only holds on feed jobs count, and only feed jobs wait. The
    first time it engages on a day, the system log gets one line.
    """
    limit = number(settings, "max_new_holds_per_day", 8)
    today = now.strftime("%Y-%m-%d")
    names = [name for name in (*workflow.SOURCES, *workflow.SOURCE_ALIASES) if is_feed(name)]
    held = conn.execute(
        "SELECT COUNT(DISTINCT e.application_id) FROM application_events e JOIN "
        "application_queue q ON q.id=e.application_id WHERE e.kind='needs_action' "
        f"AND e.created_at LIKE ? AND q.source IN ({','.join('?' * len(names))})",
        (today + "%", *names),
    ).fetchone()[0]
    if held < limit:
        return False
    said = conn.execute("SELECT value FROM intake_marks WHERE name=?", (HOLD_BRAKE,)).fetchone()
    if not said or said["value"] != today:
        conn.execute("INSERT OR REPLACE INTO intake_marks VALUES(?,?)", (HOLD_BRAKE, today))
        conn.commit()  # nothing stays locked while the line is posted
        workflow.system_line(
            "intake",
            f"hold brake · {held} feed jobs stopped for you today, so feed jobs wait until "
            "tomorrow · your own links and picks still go",
        )
    return True


def resting_platforms(conn, settings: dict, now: datetime) -> set[str]:
    """Platforms that took a submission less than the per-platform gap ago."""
    seconds = number(settings, "min_seconds_between_submissions_per_platform", 90)
    if not seconds:
        return set()
    rows = conn.execute(
        "SELECT platform,MAX(created_at) AS last FROM live_submission_attempts "
        "WHERE platform IS NOT NULL AND platform!='' GROUP BY platform"
    ).fetchall()
    return {
        row["platform"]
        for row in rows
        if now - datetime.fromisoformat(row["last"]) < timedelta(seconds=seconds)
    }


# ---------------------------------------------------------------------------
# The daily digest: one live card of borderline jobs, answered with a number and a word
# ---------------------------------------------------------------------------

DIGEST_REPLIES = ["3 yes", "5 no", "all yes", "none"]


def digest_channel() -> str | None:
    settings = workflow.config()
    return settings.get("shortlist_channel_id") if settings.get("enabled") else None


def digest_day(now: datetime, hour: int) -> str:
    """The digest's day runs from its posting hour to the same hour the next morning."""
    return (now - timedelta(hours=hour)).date().isoformat()


def line_text(line: dict, linked: bool = True) -> str:
    """One numbered line: company, role, place, and the reason in a few words."""
    place = workflow.clip(line.get("location") or "place not listed", 40)
    if line.get("also"):
        place += f" +{len(line['also'])} more"
    company = workflow.clip(line.get("company") or "Company", 40)
    role = workflow.clip(line.get("title") or "Role", 70)
    if line.get("answer"):
        outcome = {"yes": "queued", "moved": "queued from another listing"}.get(
            line["answer"], "skipped"
        )
        return f"~~{line['number']}. {company} · {role}~~ {outcome}"
    if linked and line.get("url"):
        role = f"[{role.replace('[', '(').replace(']', ')')}]({line['url']})"
    text = f"{line['number']}. **{company}** · {role} · {place}"
    return text + (f" · _{workflow.clip(line['reason'], 70)}_" if line.get("reason") else "")


def digest_card(day: str, data: dict) -> dict:
    """The digest as one card: numbered lines, the replies, and what waits for tomorrow."""
    when = date.fromisoformat(day)
    lines = [line_text(line) for line in data["lines"]]
    if len("\n".join(lines)) > 3800:
        # Long links would push lines off the card; the roles stay, the links go.
        lines = [line_text(line, linked=False) for line in data["lines"]]
    card = workflow.embed(
        f"Worth a look · {when.day} {when.strftime('%b')}",
        color="needs",
        fields=[("Reply", workflow.command_block(DIGEST_REPLIES), False)],
        footer=(
            f"{data['waiting']} more on tomorrow's list"
            if data.get("waiting")
            else "Close matches I did not queue on my own"
        ),
    )
    card["description"] = "\n".join(lines)
    return card


def send(method: str, route: str, payload: dict | None = None):
    return discord_feed.discord(method, route, payload)


def flush_digests():
    """Post a recorded digest, or redraw one whose lines changed; retried on later ticks."""
    channel = digest_channel()
    if not channel:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_digests WHERE delivery='pending' OR (delivery='sent' AND shown=0)"
        ).fetchall()
    for row in rows:
        body = {
            "embeds": [digest_card(row["day"], json.loads(row["data"]))],
            "allowed_mentions": {"parse": []},
        }
        try:
            if row["delivery"] == "pending":
                sent = send(
                    "POST",
                    f"/channels/{channel}/messages",
                    {**body, "nonce": "digest:" + row["day"], "enforce_nonce": True},
                )
                message_id = str(sent.get("id", ""))
            else:
                send("PATCH", f"/channels/{channel}/messages/{row['message_id']}", body)
                message_id = row["message_id"]
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("digest", row["day"], error)
            return
        with db() as conn:
            conn.execute(
                "UPDATE intake_digests SET delivery='sent',shown=1,message_id=? WHERE day=?",
                (message_id, row["day"]),
            )


def withdraw_digest(row, channel: str | None):
    """Take a digest out of the channel; its decisions stay on record."""
    if channel and row["delivery"] == "sent" and row["message_id"]:
        try:
            send("DELETE", f"/channels/{channel}/messages/{row['message_id']}")
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("digest-withdraw", row["day"], error)
    with db() as conn:
        conn.execute("UPDATE intake_digests SET delivery='withdrawn' WHERE day=?", (row["day"],))


def run_digest(settings: dict, now: datetime | None = None) -> dict:
    """Once per feed run: retire yesterday's digest and post today's when its hour has come.

    Lines nobody answered go back to waiting and return on the next digest, until the
    posting closes or `digest_keep_days` pass.
    """
    channel = digest_channel()
    if not channel:
        return {"digest_day": None}
    now = now or datetime.now().astimezone()
    today = digest_day(now, number(settings, "digest_hour", 9) % 24)
    size = max(number(settings, "digest_size", 15), 1)
    keep_days = max(number(settings, "digest_keep_days", 30), 1)
    with db() as conn:
        old = conn.execute(
            "SELECT * FROM intake_digests WHERE day<? AND delivery IN ('pending','sent')", (today,)
        ).fetchall()
    for row in old:
        with db() as conn:
            for line in json.loads(row["data"])["lines"]:
                if not line.get("answer"):
                    conn.execute(
                        "UPDATE intake_decisions SET status='digest',updated_at=? "
                        "WHERE identity=? AND status='offered'",
                        (workflow.now(), line["identity"]),
                    )
        withdraw_digest(row, channel)
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM intake_digests WHERE day=?", (today,)).fetchone()
        if not exists:
            cutoff = (now.astimezone(UTC) - timedelta(days=keep_days)).isoformat()
            # A closed posting or one that waited too long leaves without being asked about.
            conn.execute(
                "UPDATE intake_decisions SET status='lapsed',updated_at=? WHERE status='digest' "
                "AND (decided_at<? OR job_id IN (SELECT id FROM jobs WHERE active=0))",
                (workflow.now(), cutoff),
            )
            waiting = conn.execute(
                "SELECT * FROM intake_decisions WHERE status='digest' "
                "ORDER BY score DESC,decided_at DESC,identity"
            ).fetchall()
            if waiting:
                lines = []
                for number_, row in enumerate(waiting[:size], start=1):
                    payload = json.loads(row["payload"])
                    lines.append(
                        {
                            "number": number_,
                            "identity": row["identity"],
                            "company": payload.get("company"),
                            "title": payload.get("title"),
                            "location": payload.get("location"),
                            "also": payload.get("also") or [],
                            "url": payload.get("url"),
                            "reason": row["reason"],
                            "score": row["score"],
                            "answer": None,
                        }
                    )
                    conn.execute(
                        "UPDATE intake_decisions SET status='offered',updated_at=? WHERE identity=?",
                        (workflow.now(), row["identity"]),
                    )
                conn.execute(
                    "INSERT INTO intake_digests(day,data,created_at) VALUES(?,?,?)",
                    (
                        today,
                        json.dumps({"lines": lines, "waiting": max(len(waiting) - size, 0)}),
                        workflow.now(),
                    ),
                )
    with db() as conn:
        live = conn.execute("SELECT * FROM intake_digests WHERE delivery='sent'").fetchall()
    for row in live:
        if all(line.get("answer") for line in json.loads(row["data"])["lines"]):
            withdraw_digest(row, channel)  # nothing left on it to answer
    flush_digests()
    return {"digest_day": today}


ANSWER_WORDS = {"yes": "yes", "y": "yes", "no": "no", "n": "no"}
NUMBERED = re.compile(
    r"\s*((?:\d{1,2}(?:\s*-\s*\d{1,2})?(?:\s*,\s*|\s+and\s+|\s+))+)(yes|y|no|n)\b[\s,;.]*"
)


def read_reply(text: str) -> dict | str | None:
    """`3 yes`, `1 4 no`, `2-5 yes, 7 no` as {number: answer}; `all yes`, `all no` and
    `none` as the answer for every open line; anything else is not a digest reply."""
    words = " ".join(str(text or "").strip().strip("`").lower().rstrip(".!").split())
    if words in {"all yes", "yes to all", "yes all"}:
        return "yes"
    if words in {"none", "all no", "no to all", "no all"}:
        return "no"
    answers: dict[int, str] = {}
    position = 0
    while position < len(words):
        match = NUMBERED.match(words, position)
        if not match:
            return None
        for item in re.findall(r"\d{1,2}(?:\s*-\s*\d{1,2})?", match[1]):
            first, _, last = (part.strip() for part in item.partition("-"))
            for value in range(int(first), int(last or first) + 1):
                answers[value] = ANSWER_WORDS[match[2]]
        position = match.end()
    return answers or None


def digest_reply(message: dict, owner: str, channel: str) -> bool:
    """Apply the owner's reply to the live digest. True when the message was one.

    Only the owner's own message in the shortlist channel counts. `yes` queues the job as
    the owner's pick; `no` drops it. A reply that cannot apply raises one plain line.
    """
    author = message.get("author") or {}
    if (
        not channel
        or channel != workflow.config().get("shortlist_channel_id")
        or author.get("id") != owner
        or author.get("bot")
    ):
        return False
    reply = read_reply(message.get("content", ""))
    if reply is None:
        return False
    referenced = (message.get("message_reference") or {}).get("message_id")
    with db() as conn:
        live = conn.execute(
            "SELECT * FROM intake_digests WHERE delivery='sent' ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if referenced:
            target = conn.execute(
                "SELECT * FROM intake_digests WHERE message_id=?", (referenced,)
            ).fetchone()
            if not target:
                return False  # a reply to some other card; the usual reader handles it
            if target["delivery"] != "sent":
                raise ValueError("That list has closed. The next one will have new numbers.")
            live = target
        if not live:
            cards = conn.execute(
                "SELECT 1 FROM owner_notices WHERE channel='shortlist' AND delivery='sent'"
            ).fetchone()
            if cards:
                return False
            raise ValueError("No list is open right now. The next one will have new numbers.")
    data = json.loads(live["data"])
    lines = {line["number"]: line for line in data["lines"]}
    if isinstance(reply, str):
        reply = {n: reply for n, line in lines.items() if not line.get("answer")}
    unknown = sorted(n for n in reply if n not in lines)
    if unknown:
        raise ValueError(
            f"There is no number {unknown[0]} on today's list; it goes up to {len(lines)}."
        )
    queued, tracked, skipped = [], [], 0
    stamp = basis(_profile_hash())
    for number_, answer in sorted(reply.items()):
        line = lines[number_]
        if line.get("answer"):
            continue  # answered already; a repeated reply changes nothing
        application_id = None
        if answer == "yes":
            title = f"{line.get('company') or ''} — {line.get('title') or ''}".strip(" —")
            # The source is spelled out here: this reply is the one place it is given.
            result = workflow.enqueue(line["url"], source="owner_pick", title=title)
            name = str(line.get("company") or line.get("title") or "one")
            if result["already_exists"] and result["status"] == "DEFERRED":
                # Parked earlier (the one-time sort of the old queue does this): the yes
                # brings the same application back as the owner's pick.
                application_id = result["application_id"]
                workflow.set_state(application_id, "QUEUED", error=None)
                queued.append(name)
            elif result["already_exists"]:
                tracked.append(name)  # the same link is an application already; nothing new
            else:
                application_id = result["application_id"]
                queued.append(name)
        else:
            skipped += 1
        line["answer"] = answer
        with db() as conn:
            conn.execute(
                "UPDATE intake_decisions SET status=?,application_id=?,updated_at=? WHERE identity=?",
                (
                    "picked" if answer == "yes" else "declined",
                    application_id,
                    workflow.now(),
                    line["identity"],
                ),
            )
            if application_id:
                record_queue_score(
                    conn, application_id, line.get("score") or 0, line.get("reason") or "", stamp
                )
            conn.execute(
                "UPDATE intake_digests SET data=?,shown=0 WHERE day=?",
                (json.dumps(data), live["day"]),
            )
    if all(line.get("answer") for line in data["lines"]):
        withdraw_digest(live, channel)
    else:
        flush_digests()
    parts = []
    if queued:
        parts.append("queued " + workflow.clip(", ".join(queued), 300))
    if tracked:
        parts.append("already on your list: " + workflow.clip(", ".join(tracked), 300))
    if skipped:
        parts.append(f"skipped {skipped}")
    if parts:
        parts[0] = parts[0][0].upper() + parts[0][1:]
        try:
            send(
                "POST",
                f"/channels/{channel}/messages",
                {"content": " · ".join(parts) + ".", "allowed_mentions": {"parse": []}},
            )
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("digest-reply", live["day"], error)
    return True


def _profile_hash() -> str:
    try:
        return workflow.read_approved().get("profile_hash", "")
    except ValueError:
        return ""
