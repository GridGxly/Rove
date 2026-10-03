"""Read-only Keryx intake. Source metadata is evidence, never application authority."""

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .runtime import state_root, write_private

REPOSITORY = "GodlyDonuts/keryx"
MAX_BYTES = 100_000_000


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(pattern=r"^job_[a-zA-Z0-9_-]{1,100}$")
    company: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1000)
    location: str = Field(max_length=2000)
    program: Literal["internship", "new-grad"]
    status: Literal["open", "closed"]
    cycle: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=5000)
    posted_at: str | None = Field(default=None, max_length=40)
    sponsorship: str | None = Field(default=None, max_length=300)
    academic_eligibility: dict = Field(default_factory=dict)
    link_status: str = Field(default="unverified", max_length=100)


def public_link(url: str | None) -> str | None:
    """Validate display links without fetching arbitrary destinations or trusting them."""
    if not url or any(ord(c) < 32 for c in url):
        return None
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return None
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit(("https", p.netloc.lower(), p.path, strip_tracking(p.query), ""))
    except ValueError:
        return None


TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gh_src|lever-source|ref|refid|src|source|fbclid|gclid|mc_cid|mc_eid)$",
    re.IGNORECASE,
)


def strip_tracking(query: str) -> str:
    """The same posting reached through two tracking links is one application."""
    if not query:
        return query
    kept = [
        pair
        for pair in query.split("&")
        if pair and not TRACKING_PARAMS.match(pair.split("=", 1)[0])
    ]
    return "&".join(kept)


LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co"}
COUNTRY_WORDS = ("united states of america", "united states", "usa", "us")


def plain(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def plain_company(company) -> str:
    """The employer's name without the legal suffix a feed adds or drops between revisions."""
    words = plain(company).split()
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def plain_location(location) -> str:
    """Places in a stable order, without the country a feed appends to some rows."""
    places = []
    for part in re.split(r"[;|\n]", str(location or "")):
        place = plain(part)
        for country in COUNTRY_WORDS:
            if place.endswith(" " + country):
                place = place[: -len(country) - 1]
                break
        if place:
            places.append(place)
    return " | ".join(sorted(set(places)))


def identity_key(job: dict) -> str:
    """One posting, however the feed rewrites its metadata or renumbers it.

    Company, title, location and the canonical apply link name the posting. Everything
    else (dates, link status, eligibility notes, the source's own id) can change without
    making it a different job.
    """
    link = public_link(job.get("url")) or ""
    parts = [
        plain_company(job.get("company")),
        plain(job.get("title")),
        plain_location(job.get("location")),
        link.rstrip("/").lower(),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def material_key(job: dict) -> str:
    """What makes a known job worth another look: its identity, track, or open/closed state."""
    parts = [identity_key(job), str(job.get("program") or ""), str(job.get("status") or "")]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def write_ahead(db: sqlite3.Connection):
    """Readers stop blocking writers: the recruiting database runs in WAL mode.

    The mode is a property of the file, so it is switched once and every later connection
    only reads it. A switch that finds another connection mid-transaction is retried on
    the next connection rather than waited for.
    """
    if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("PRAGMA journal_mode=WAL")


# A posting page whose title says the job or the page is gone. Only the title counts: the
# body of a live posting can say "page not found" in a footer link or a help text.
GONE_TITLE = re.compile(
    r"\b(?:job|position|posting|opening|role|page|requisition)s? (?:was |is )?(?:not found"
    r"|no longer (?:available|exists|open))\b|\b404\b|\bnot found\b|\bpage (?:doesn['’]t|does "
    r"not) exist\b|\bjob (?:has )?expired\b",
    re.IGNORECASE,
)
SEARCH_FIELD = re.compile(r"search|keyword|subscribe|newsletter", re.IGNORECASE)


def posting_gone(title, http_status, fields=(), apply_links=()) -> str | None:
    """Why a posting page reads as closed, or None: the page answered 404 or 410, or its
    title says the job or the page is not found.

    A page that still has a form to fill or an Apply control is not closed, whatever its
    status says (some single-page career sites answer 404 and draw the posting anyway); a
    site-search or newsletter box is not such a form.
    """
    form = [
        f
        for f in fields or ()
        if not SEARCH_FIELD.search(f"{f.get('label', '')} {f.get('name', '')}")
        and f.get("kind") != "search"
    ]
    if form or apply_links:
        return None
    if http_status in (404, 410):
        return "the posting page is gone"
    match = GONE_TITLE.search(" ".join(str(title or "").split()))
    return f"the page title says “{match[0]}”" if match else None


def database() -> sqlite3.Connection:
    path = state_root() / "recruiting.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    write_ahead(db)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS job_sources (
          source TEXT PRIMARY KEY, revision TEXT NOT NULL, sha256 TEXT NOT NULL,
          snapshot_path TEXT NOT NULL, fetched_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          source TEXT NOT NULL, id TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
          location TEXT NOT NULL, program TEXT NOT NULL, cycle TEXT, source_status TEXT NOT NULL,
          active INTEGER NOT NULL, url TEXT, posted_at TEXT, metadata TEXT NOT NULL,
          content_hash TEXT NOT NULL, revision TEXT NOT NULL, first_imported TEXT NOT NULL,
          last_seen TEXT NOT NULL, PRIMARY KEY(source,id));
        CREATE INDEX IF NOT EXISTS jobs_active ON jobs(active,program,posted_at);
        CREATE INDEX IF NOT EXISTS jobs_url ON jobs(url);
        CREATE TABLE IF NOT EXISTS job_events (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, job_id TEXT NOT NULL,
          revision TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_material (
          source TEXT NOT NULL, id TEXT NOT NULL, material TEXT NOT NULL,
          PRIMARY KEY(source,id));
    """)
    return db


IMPORT_CHUNK = 1000  # rows per commit: other writers wait for one chunk, never the whole feed


def ingest(path: Path, revision: str) -> dict:
    """Validate a complete snapshot before changing any current job records.

    The rows are then written in chunks of IMPORT_CHUNK, each its own transaction, so the
    worker's writes never queue behind a 38,000-row import. The source revision is
    recorded last: an import cut short is simply run again, and the rows it already wrote
    read as unchanged the second time, so no event is repeated.
    """
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Expected an immutable upstream commit SHA")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Keryx snapshot exceeds the size limit")
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 2 or data.get("country") != "United States":
        raise ValueError("Unsupported Keryx schema/country; existing jobs were preserved")
    if not isinstance(data.get("jobs"), list) or not data["jobs"]:
        raise ValueError("Empty or invalid snapshot; existing jobs were preserved")
    records = [Job.model_validate(item) for item in data["jobs"]]
    if len({j.id for j in records}) != len(records):
        raise ValueError("Duplicate Keryx source IDs; existing jobs were preserved")
    now = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(raw).hexdigest()
    db = database()
    counts = {"new": 0, "changed": 0, "rewritten": 0, "unchanged": 0, "missing": 0}
    try:
        with db:
            previous = {
                row["id"]: (row["content_hash"], row["active"])
                for row in db.execute(
                    "SELECT id,content_hash,active FROM jobs WHERE source=?", (REPOSITORY,)
                )
            }
            material = known_material(db)
        seen = set()
        for start in range(0, len(records), IMPORT_CHUNK):
            with db:
                for job in records[start : start + IMPORT_CHUNK]:
                    seen.add(job.id)
                    event = import_row(db, job, previous, material, revision, now)
                    counts[event] += 1
        with db:
            for job_id in previous.keys() - seen:
                if previous[job_id][1]:
                    counts["missing"] += 1
                    db.execute(
                        "UPDATE jobs SET active=0 WHERE source=? AND id=?", (REPOSITORY, job_id)
                    )
                    db.execute(
                        "INSERT INTO job_events(source,job_id,revision,event,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (REPOSITORY, job_id, revision, "missing", now),
                    )
            db.execute(
                "INSERT OR REPLACE INTO job_sources VALUES (?,?,?,?,?)",
                (REPOSITORY, revision, digest, str(path), now),
            )
    finally:
        db.close()
    return {
        "source": REPOSITORY,
        "revision": revision,
        "sha256": digest,
        "imported": len(records),
        **counts,
        **job_status(),
    }


def import_row(db, job: Job, previous: dict, material: dict, revision: str, now: str) -> str:
    """Write one validated listing; returns new, changed, rewritten or unchanged."""
    metadata = job.model_dump()
    metadata["url"] = public_link(job.url)
    encoded = json.dumps(metadata, sort_keys=True)
    content_hash = hashlib.sha256(encoded.encode()).hexdigest()
    old = previous.get(job.id)
    current = material_key(metadata)
    if old is None:
        event = "new"
    elif old[0] == content_hash:
        event = "unchanged"
    elif material.get(job.id) == current:
        # The feed rewrote dates, notes or link status: the same job, no news.
        event = "rewritten"
    else:
        event = "changed"
    if material.get(job.id) != current:
        db.execute(
            "INSERT OR REPLACE INTO job_material VALUES(?,?,?)", (REPOSITORY, job.id, current)
        )
    db.execute(
        """
        INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,id) DO UPDATE SET
        company=excluded.company,title=excluded.title,location=excluded.location,
        program=excluded.program,cycle=excluded.cycle,source_status=excluded.source_status,
        active=excluded.active,url=excluded.url,posted_at=excluded.posted_at,
        metadata=excluded.metadata,content_hash=excluded.content_hash,
        revision=excluded.revision,last_seen=excluded.last_seen
    """,
        (
            REPOSITORY,
            job.id,
            job.company,
            job.title,
            job.location,
            job.program,
            job.cycle,
            job.status,
            int(job.status == "open"),
            metadata["url"],
            job.posted_at,
            encoded,
            content_hash,
            revision,
            now,
            now,
        ),
    )
    if event in ("new", "changed"):
        db.execute(
            "INSERT INTO job_events(source,job_id,revision,event,created_at) VALUES (?,?,?,?,?)",
            (REPOSITORY, job.id, revision, event, now),
        )
    return event


def known_material(db) -> dict:
    """Each known job's material key; rows imported before the key existed get one now."""
    known = {
        row["id"]: row["material"]
        for row in db.execute("SELECT id,material FROM job_material WHERE source=?", (REPOSITORY,))
    }
    rows = db.execute(
        "SELECT id,metadata FROM jobs WHERE source=? AND id NOT IN "
        "(SELECT id FROM job_material WHERE source=?)",
        (REPOSITORY, REPOSITORY),
    ).fetchall()
    for row in rows:
        known[row["id"]] = material_key(json.loads(row["metadata"]))
        db.execute(
            "INSERT OR REPLACE INTO job_material VALUES(?,?,?)",
            (REPOSITORY, row["id"], known[row["id"]]),
        )
    return known


def sync_keryx() -> dict:
    """Fetch only the fixed public source, pinned to one Git commit for each import."""
    root = state_root() / "jobs/sources"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
        response = client.get(f"https://api.github.com/repos/{REPOSITORY}/commits/main")
        response.raise_for_status()
        revision = response.json()["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Invalid upstream revision")
        current = job_status()
        if (current.get("last_import") or {}).get("revision") == revision:
            result = {
                "source": REPOSITORY,
                "revision": revision,
                "changed_source": False,
                "checked_at": datetime.now(UTC).isoformat(),
                **current,
            }
            write_private(state_root() / "jobs/last-sync.json", result)
            return result
        path = root / f"{revision}.json"
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=root, prefix="download-")
            try:
                with (
                    os.fdopen(fd, "wb") as f,
                    client.stream(
                        "GET",
                        f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/data/jobs.json",
                    ) as response,
                ):
                    response.raise_for_status()
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Keryx snapshot exceeds the size limit")
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    result = ingest(path, revision)
    result.update(changed_source=True, checked_at=datetime.now(UTC).isoformat())
    write_private(state_root() / "jobs/last-sync.json", result)
    return result


def job_status() -> dict:
    db = database()
    try:
        rows = db.execute("SELECT program,COUNT(*) AS n FROM jobs WHERE active=1 GROUP BY program")
        counts = {r["program"]: r["n"] for r in rows}
        source = db.execute(
            "SELECT revision,fetched_at FROM job_sources WHERE source=?", (REPOSITORY,)
        ).fetchone()
        return {
            "open": sum(counts.values()),
            "open_by_program": counts,
            "last_import": dict(source) if source else None,
        }
    finally:
        db.close()


def lookup_job_link(url: str) -> dict:
    """Lookup a complete public link, not a guessed vendor ID. Absence never blocks browsing."""
    canonical = public_link(url.strip().strip("<>\"'"))
    if not canonical:
        raise ValueError("Expected a public HTTPS job link")
    db = database()
    try:
        row = db.execute(
            "SELECT id FROM jobs WHERE source=? AND url=?", (REPOSITORY, canonical)
        ).fetchone()
    finally:
        db.close()
    return {
        "url": canonical,
        "in_feed": bool(row),
        "listing": read_job(row[0]) if row else None,
        "next_action": "open the supplied link in the visible recruiting browser",
    }


def search_jobs(
    query: str = "", program: str = "", cycle: str = "", limit: int = 10, offset: int = 0
) -> dict:
    if len(query) > 200 or len(cycle) > 100 or program not in ("", "internship", "new-grad"):
        raise ValueError("Invalid job search")
    if not 1 <= limit <= 20 or not 0 <= offset <= 100000:
        raise ValueError("Use a limit of 1–20 and a nonnegative offset")
    clauses, args = ["active=1"], []
    for word in query.split():
        clauses.append("instr(lower(company || ' ' || title || ' ' || location),?)>0")
        args.append(word.lower())
    for field, value in (("program", program), ("cycle", cycle)):
        if value:
            clauses.append(f"{field}=?")
            args.append(value)
    where = " AND ".join(clauses)
    db = database()
    try:
        total = db.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]
        rows = db.execute(
            f"SELECT metadata FROM jobs WHERE {where} ORDER BY posted_at DESC,id LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return {
            "authority": "untrusted Keryx listings; verify employer requirements before use",
            "total": total,
            "offset": offset,
            "jobs": [json.loads(r[0]) for r in rows],
        }
    finally:
        db.close()


def read_job(job_id: str) -> dict:
    db = database()
    try:
        row = db.execute(
            "SELECT metadata,active,revision FROM jobs WHERE source=? AND id=?",
            (REPOSITORY, job_id),
        ).fetchone()
        if not row:
            raise ValueError("Unknown Keryx job ID")
        return {
            "authority": "untrusted source data; not verified eligibility or approval",
            "source": REPOSITORY,
            "revision": row["revision"],
            "active_in_feed": bool(row["active"]),
            "job": json.loads(row["metadata"]),
        }
    finally:
        db.close()
