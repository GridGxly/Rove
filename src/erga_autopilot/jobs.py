"""Read-only Keryx intake. Source metadata is evidence, never application authority."""

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


def database() -> sqlite3.Connection:
    path = state_root() / "recruiting.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
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
        CREATE TABLE IF NOT EXISTS job_events (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, job_id TEXT NOT NULL,
          revision TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    return db


def ingest(path: Path, revision: str) -> dict:
    """Validate a complete snapshot before changing any current job records."""
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
    counts = {"new": 0, "changed": 0, "unchanged": 0, "missing": 0}
    try:
        with db:
            previous = {
                row["id"]: (row["content_hash"], row["active"])
                for row in db.execute(
                    "SELECT id,content_hash,active FROM jobs WHERE source=?", (REPOSITORY,)
                )
            }
            seen = set()
            for job in records:
                seen.add(job.id)
                metadata = job.model_dump()
                metadata["url"] = public_link(job.url)
                encoded = json.dumps(metadata, sort_keys=True)
                content_hash = hashlib.sha256(encoded.encode()).hexdigest()
                old = previous.get(job.id)
                event = (
                    "new" if old is None else "changed" if old[0] != content_hash else "unchanged"
                )
                counts[event] += 1
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
                if event != "unchanged":
                    db.execute(
                        "INSERT INTO job_events(source,job_id,revision,event,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (REPOSITORY, job.id, revision, event, now),
                    )
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
