"""Deterministic Keryx notifications; never run Qwen or authorize an application."""

import contextlib
import json
import sqlite3
import time
from datetime import UTC, datetime

import httpx

from . import timing
from .jobs import database, identity_key, sync_keryx
from .onboarding import read_approved
from .runtime import state_root, write_private


def private_env() -> dict:
    from pathlib import Path

    values = {}
    for path in (state_root() / "config/setup.env", Path.home() / ".hermes/.env"):
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
    return values


@timing.call("discord")
def discord_upload(channel: str, path, payload: dict) -> dict:
    """One message with one private file attached (a screenshot or the resume as sent)."""
    import mimetypes
    from pathlib import Path

    token = private_env().get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("Discord bot credential is missing")
    file_path = Path(path)
    kind = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    with (
        httpx.Client(base_url="https://discord.com/api/v10", timeout=60, trust_env=False) as c,
        file_path.open("rb") as handle,
    ):
        response = c.post(
            f"/channels/{channel}/messages",
            headers={"Authorization": "Bot " + token},
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (file_path.name, handle, kind)},
        )
    response.raise_for_status()
    return response.json()


@timing.call("discord")
def discord(method: str, path: str, payload: dict | None = None):
    token = private_env().get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("Discord bot credential is missing")
    with httpx.Client(base_url="https://discord.com/api/v10", timeout=30, trust_env=False) as c:
        response = c.request(method, path, headers={"Authorization": "Bot " + token}, json=payload)
        for _ in range(3):
            if response.status_code != 429:
                break
            # Only a definite rate-limit rejection is retryable. Timeouts are ambiguous.
            delay = float(response.json().get("retry_after", 1))
            if not 0 <= delay <= 30:
                break
            time.sleep(delay + 0.05)
            response = c.request(
                method, path, headers={"Authorization": "Bot " + token}, json=payload
            )
        response.raise_for_status()
        return response.json() if response.content else {}


def feed_db():
    from . import intake

    db = database()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS feed_cursor(id INTEGER PRIMARY KEY CHECK(id=1), event_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feed_outbox(
          key TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
    """)
    if "score" not in {row[1] for row in db.execute("PRAGMA table_info(feed_outbox)")}:
        # Another local service may add the column in the same moment; either one will do.
        with contextlib.suppress(sqlite3.OperationalError):
            db.execute("ALTER TABLE feed_outbox ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
    intake.ensure_tables(db)
    return db


def queue_jobs(db, records):
    """One outbox row per posting, keyed by its stable identity, carrying its score.

    A posting already in the outbox stays as it is, except one the backlog cap left out:
    queueing that again makes it pending again.
    """
    for job in records:
        db.execute(
            "INSERT INTO feed_outbox(key,job_id,payload,score) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status='pending',payload=excluded.payload,"
            "score=excluded.score WHERE feed_outbox.status='expired'",
            (
                job.get("identity") or identity_key(job),
                job["id"],
                json.dumps(job),
                int(job.get("score") or 0),
            ),
        )


def cap_backlog(db, keep: int) -> int:
    """A backlog beyond `keep` jobs keeps the best-scoring ones (the newest among equals)
    and expires the rest instead of flooding the channel."""
    with db:
        cursor = db.execute(
            """UPDATE feed_outbox SET status='expired' WHERE status='pending' AND rowid NOT IN
            (SELECT rowid FROM feed_outbox WHERE status='pending'
             ORDER BY score DESC,rowid DESC LIMIT ?)""",
            (max(keep, 1),),
        )
        db.execute(
            """UPDATE intake_decisions SET status='capped' WHERE status='queued' AND identity IN
            (SELECT key FROM feed_outbox WHERE status='expired')"""
        )
    return cursor.rowcount


def job_card(job: dict, queued: dict | None) -> dict:
    """One glanceable card per job: the role, the company and place, why it matched."""
    from .workflow import clip, embed

    place = clip(job.get("location") or "Location not listed", 120)
    if job.get("also"):
        place += f" · also in {len(job['also'])} other place{'s' if len(job['also']) != 1 else ''}"
    description = f"**{clip(job['company'], 100)}** · {place}"
    if job.get("reason"):
        description += "\n" + clip(job["reason"], 160)
    return embed(
        clip(job["title"].replace("\n", " "), 200),
        description,
        color="preparing",
        url=job.get("url"),
        fields=[
            ("Cycle", job.get("cycle") or "Not listed", True),
            ("Track", str(job.get("program", "")).replace("-", " ").title() or "—", True),
        ],
        footer=(
            "Already tracked"
            if queued and queued.get("already_exists")
            else "Queued"
            if queued
            else "Not queued · the posting has no employer link"
        ),
    )


def close_withdrawn_postings(revision: str) -> int:
    """A queued application whose Keryx posting closed is parked, not prepared."""
    from . import workflow

    db = database()
    try:
        closed = [
            r[0]
            for r in db.execute(
                "SELECT url FROM jobs WHERE active=0 AND revision=? AND url IS NOT NULL",
                (revision,),
            )
        ]
    finally:
        db.close()
    parked = 0
    for url in closed:
        with workflow.db() as conn:
            rows = conn.execute(
                "SELECT id,status FROM application_queue WHERE url=? AND status IN "
                "('QUEUED','NEEDS_USER','READY_FOR_REVIEW')",
                (url,),
            ).fetchall()
        for row in rows:
            if row["status"] == "QUEUED":
                workflow.transition(row["id"], "DEFERRED", "posting closed in the Keryx feed")
                parked += 1
            else:
                workflow.record(row["id"], "posting_closed", {"source": "Keryx", "url": url})
                workflow.flush_events(row["id"])
    return parked


def tick(seed: bool = False) -> dict:
    from . import intake

    config = json.loads((state_root() / "config/feed.json").read_text())
    if not config.get("enabled"):
        return {"enabled": False}
    result = sync_keryx()
    if result.get("changed_source") and result.get("revision"):
        close_withdrawn_postings(result["revision"])
    approved = read_approved()
    profile = approved["profile"]
    db = feed_db()
    sent = 0
    counts: dict = {}
    try:
        with db:
            row = db.execute("SELECT event_id FROM feed_cursor WHERE id=1").fetchone()
            maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM job_events").fetchone()[0]
            # A job is announced once. A posting the feed rewrote or renumbered keeps its
            # identity, and an id announced before identities existed stays announced.
            announced = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT job_id FROM feed_outbox WHERE status IN ('sent','sending','pending')"
                )
            }
            jobs = []
            if row:
                events = db.execute(
                    """SELECT j.metadata,j.revision FROM job_events e JOIN jobs j
                    ON e.job_id=j.id AND e.source=j.source WHERE e.id>? AND j.active=1
                    AND e.event IN ('new','changed') ORDER BY e.id""",
                    (row[0],),
                ).fetchall()
                for event in events:
                    job = json.loads(event["metadata"])
                    job["source_revision"] = event["revision"]
                    jobs.append(job)
            # Every new job is scored once, in code: the best are queued and announced, the
            # borderline wait for the daily digest, the rest are dropped and only counted.
            decided = intake.decide(db, jobs, profile, known_ids=announced)
            counts = decided["counts"]
            queue_jobs(db, decided["queue"])
            if seed:
                # A manual seed pulls in the best open jobs that were never announced.
                best = intake.unseen_best(db, profile, announced)
                seeded = intake.decide(db, best, profile, known_ids=announced, revive=intake.UNSEEN)
                queue_jobs(db, seeded["queue"])
                for key, value in seeded["counts"].items():
                    counts[key] = counts.get(key, 0) + value
            db.execute("INSERT OR REPLACE INTO feed_cursor VALUES(1,?)", (maximum,))
            db.execute(
                """UPDATE feed_outbox SET status='superseded' WHERE status='pending'
                AND job_id IN (SELECT job_id FROM feed_outbox WHERE status='sent')"""
            )
        expired = cap_backlog(db, intake.number(config, "max_pending", 40))
        intake.log_counts(counts, expired)
        pending = db.execute(
            """SELECT o.* FROM feed_outbox o JOIN jobs j ON j.id=o.job_id
            WHERE o.status='pending' AND j.active=1 ORDER BY o.score DESC,o.rowid DESC LIMIT ?""",
            (intake.number(config, "batch_size", 10),),
        ).fetchall()
        from .workflow import enqueue

        stamp = intake.basis(approved.get("profile_hash", ""))
        for row in pending:
            job = json.loads(row["payload"])
            queued = (
                enqueue(job["url"], source="keryx", title=job["company"] + " — " + job["title"])
                if job.get("url")
                else None
            )
            card = job_card(job, queued)
            with db:
                if queued and not queued.get("already_exists") and "score" in job:
                    intake.record_queue_score(
                        db,
                        queued["application_id"],
                        job["score"],
                        job.get("reason", ""),
                        stamp,
                        intake.family_key(job),
                    )
                    db.execute(
                        "UPDATE intake_decisions SET application_id=? WHERE identity=?",
                        (queued["application_id"], row["key"]),
                    )
                db.execute("UPDATE feed_outbox SET status='sending' WHERE key=?", (row["key"],))
            message = discord(
                "POST",
                f"/channels/{config['channel_id']}/messages",
                {
                    "embeds": [card],
                    "allowed_mentions": {"parse": []},
                    "nonce": str(int(row["key"][:15], 16)),
                    "enforce_nonce": True,
                },
            )
            with db:
                db.execute(
                    "UPDATE feed_outbox SET status='sent',message_id=? WHERE key=?",
                    (message["id"], row["key"]),
                )
            sent += 1
        result.update(
            sent=sent,
            expired=expired,
            pending=db.execute(
                "SELECT COUNT(*) FROM feed_outbox WHERE status='pending'"
            ).fetchone()[0],
            **{key: counts.get(key, 0) for key in ("queued", "digest", "dropped")},
        )
    finally:
        db.close()
    result.update(intake.run_digest(config))
    result["finished_at"] = datetime.now(UTC).isoformat()
    write_private(state_root() / "jobs/feed-service.json", result)
    return result
