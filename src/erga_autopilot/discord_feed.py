"""Deterministic Keryx notifications; never run Qwen or authorize an application."""

import hashlib
import json
import time
from datetime import UTC, datetime

import httpx

from .jobs import database, sync_keryx
from .matching import contains, review_matches
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
    db = database()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS feed_cursor(id INTEGER PRIMARY KEY CHECK(id=1), event_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feed_outbox(
          key TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
    """)
    return db


def candidate_match(job: dict, prefs: dict) -> bool:
    return (
        job["program"] == "internship"
        and any(contains(job["title"], x) for x in prefs["title_keywords"])
        and not any(contains(job["title"], x) for x in prefs["excluded_title_keywords"])
        and not any(contains(job["company"], x) for x in prefs["excluded_companies"])
    )


def queue_jobs(db, records):
    for job in records:
        key = hashlib.sha256(
            (
                job["id"] + job.get("source_revision", "") + job["title"] + str(job.get("url"))
            ).encode()
        ).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO feed_outbox(key,job_id,payload) VALUES(?,?,?)",
            (key, job["id"], json.dumps(job)),
        )


def tick(seed: bool = False) -> dict:
    config = json.loads((state_root() / "config/feed.json").read_text())
    if not config.get("enabled"):
        return {"enabled": False}
    result = sync_keryx()
    prefs = read_approved()["profile"]["preferences"]
    db = feed_db()
    sent = 0
    initial = review_matches(25)["jobs"] if seed else []
    try:
        with db:
            row = db.execute("SELECT event_id FROM feed_cursor WHERE id=1").fetchone()
            maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM job_events").fetchone()[0]
            if row:
                events = db.execute(
                    """SELECT j.metadata,j.revision FROM job_events e JOIN jobs j
                    ON e.job_id=j.id AND e.source=j.source WHERE e.id>? AND j.active=1
                    AND e.event IN ('new','changed') ORDER BY e.id""",
                    (row[0],),
                ).fetchall()
                jobs = []
                for event in events:
                    job = json.loads(event["metadata"])
                    if candidate_match(job, prefs):
                        job["source_revision"] = event["revision"]
                        jobs.append(job)
                queue_jobs(db, jobs)
            if seed:
                queue_jobs(db, initial)
            db.execute("INSERT OR REPLACE INTO feed_cursor VALUES(1,?)", (maximum,))
        pending = db.execute("""SELECT o.* FROM feed_outbox o JOIN jobs j ON j.id=o.job_id
            WHERE o.status='pending' AND j.active=1 LIMIT 25""").fetchall()
        for offset in range(0, len(pending), 3):
            batch = pending[offset : offset + 3]
            parts = ["**Internship matches · queued for visible preparation**"]
            for row in batch:
                job = json.loads(row["payload"])
                from .workflow import enqueue

                queued = (
                    enqueue(job["url"], source="keryx", title=job["company"] + " — " + job["title"])
                    if job.get("url")
                    else None
                )
                title = (job["company"] + " — " + job["title"]).replace("\n", " ")[:180]
                parts.append(
                    f"**{title}**\n{job.get('cycle') or 'Term not listed'} · {job.get('location', '')[:120]}\n{job.get('url') or 'Employer link needs review'}\n"
                    + (
                        f"Application `{queued['application_id']}` · requirements checked before filling."
                        if queued
                        else "Needs employer link."
                    )
                )
            text = "\n\n".join(parts)
            if len(text) > 1950:
                raise ValueError("Feed batch exceeds Discord message limit")
            with db:
                for row in batch:
                    db.execute("UPDATE feed_outbox SET status='sending' WHERE key=?", (row["key"],))
            message = discord(
                "POST",
                f"/channels/{config['channel_id']}/messages",
                {
                    "content": text,
                    "allowed_mentions": {"parse": []},
                    "nonce": str(int(batch[0]["key"][:15], 16)),
                    "enforce_nonce": True,
                    "flags": 4,
                },
            )
            with db:
                for row in batch:
                    db.execute(
                        "UPDATE feed_outbox SET status='sent',message_id=? WHERE key=?",
                        (message["id"], row["key"]),
                    )
            sent += len(batch)
        result.update(
            sent=sent,
            pending=db.execute(
                "SELECT COUNT(*) FROM feed_outbox WHERE status='pending'"
            ).fetchone()[0],
        )
    finally:
        db.close()
    result["finished_at"] = datetime.now(UTC).isoformat()
    write_private(state_root() / "jobs/feed-service.json", result)
    return result
