"""Durable application queue and Discord flight recorder.

Only trusted local/owner intake or the configured feed policy may enqueue work.
Preparing is distinct from submission; external text cannot authorize either.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from .discord_feed import discord
from .jobs import database, public_link
from .onboarding import read_approved
from .runtime import state_root, write_private


def config() -> dict:
    path = state_root() / "config/workflow.json"
    return json.loads(path.read_text()) if path.exists() else {"enabled": False}


def db():
    conn = database()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS application_queue(
        id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, source_url TEXT NOT NULL,
        source TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
        profile_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        run_id TEXT, thread_id TEXT, package_hash TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS application_events(
        id INTEGER PRIMARY KEY, application_id TEXT NOT NULL,
        kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
      CREATE TABLE IF NOT EXISTS job_link_aliases(
        source_url TEXT PRIMARY KEY, employer_url TEXT NOT NULL, evidence TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS owner_commands(
        message_id TEXT PRIMARY KEY, application_id TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_checkpoints(channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS application_answers(
        application_id TEXT NOT NULL, field_key TEXT NOT NULL, value TEXT NOT NULL,
        owner_message_id TEXT NOT NULL, PRIMARY KEY(application_id,field_key));
      CREATE TABLE IF NOT EXISTS live_submission_attempts(
        application_id TEXT PRIMARY KEY, package_hash TEXT NOT NULL,
        owner_message_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    return conn


def now():
    return datetime.now(UTC).isoformat()


def resolve_alias(url: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT employer_url FROM job_link_aliases WHERE source_url=?", (url,)
        ).fetchone()
    return row[0] if row else url


def get(application_id: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM application_queue WHERE id=?", (application_id,)
        ).fetchone()
    if not row:
        raise ValueError("Unknown application")
    return dict(row)


def set_state(application_id: str, status: str, **values):
    allowed = {"run_id", "thread_id", "package_hash", "error", "title"}
    if values.keys() - allowed:
        raise ValueError("Invalid state fields")
    with db() as conn:
        conn.execute(
            "UPDATE application_queue SET status=?,updated_at=?"
            + "".join("," + key + "=?" for key in values)
            + " WHERE id=?",
            [status, now(), *values.values(), application_id],
        )


def enqueue(url: str, *, source: str = "owner_link", title: str = "") -> dict:
    safe = public_link(url.strip().strip("<>\"'"))
    if not safe:
        raise ValueError("Use a complete public HTTPS job link")
    target = resolve_alias(safe)
    profile = read_approved()
    application_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO application_queue "
            "(id,url,source_url,source,title,status,profile_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                application_id,
                target,
                safe,
                source,
                title[:300],
                "QUEUED",
                profile["profile_hash"],
                now(),
                now(),
            ),
        )
        row = dict(
            conn.execute("SELECT * FROM application_queue WHERE url=?", (target,)).fetchone()
        )
    return {
        "application_id": row["id"],
        "status": row["status"],
        "url": row["url"],
        "already_exists": row["id"] != application_id,
        "submission_enabled": False,
        "next_action": "The local worker opens the visible browser and records preparation in the application forum.",
    }


def record(application_id: str, kind: str, data: dict):
    # Runtime callers supply bounded application events, never credentials or raw page text.
    with db() as conn:
        conn.execute(
            "INSERT INTO application_events(application_id,kind,data,created_at) VALUES(?,?,?,?)",
            (application_id, kind, json.dumps(data), now()),
        )


def forum_url(application_id: str) -> str | None:
    item = get(application_id)
    return (
        f"https://discord.com/channels/{config()['guild_id']}/{item['thread_id']}"
        if item["thread_id"]
        else None
    )


def ensure_forum(application_id: str) -> str | None:
    item, settings = get(application_id), config()
    if item["thread_id"]:
        return item["thread_id"]
    if not settings.get("enabled") or not settings.get("forum_channel_id"):
        return None
    # Do not retry an uncertain forum creation: investigate the existing forum first.
    with db() as conn:
        prior = conn.execute(
            "SELECT 1 FROM application_events WHERE application_id=? AND kind='forum_creation_attempt'",
            (application_id,),
        ).fetchone()
    if prior:
        raise RuntimeError("Forum creation result is uncertain; reconcile before retrying")
    record(application_id, "forum_creation_attempt", {})
    thread = discord(
        "POST",
        f"/channels/{settings['forum_channel_id']}/threads",
        {
            "name": (item["title"] or "Application")[:80] + " · " + application_id,
            "auto_archive_duration": 10080,
            "applied_tags": [settings["tags"]["Preparing"]]
            if settings.get("tags", {}).get("Preparing")
            else [],
            "message": {
                "content": f"**Preparing**\n{item['source_url']}\nOfficial target: {item['url']}\nApplication `{application_id}` · visible browser\nSubmission requires review of the exact prepared package.",
                "allowed_mentions": {"parse": []},
            },
        },
    )
    set_state(application_id, item["status"], thread_id=thread["id"])
    with db() as conn:
        conn.execute(
            "UPDATE application_events SET delivery='sent' WHERE application_id=? AND kind='forum_creation_attempt'",
            (application_id,),
        )
    return thread["id"]


def event_text(kind: str, data: dict) -> str:
    if kind == "fields_prepared":
        parts = ["**Form preparation**"]
        for field in data.get("filled", []):
            value = field.get("value", "resume PDF · SHA-256 " + field.get("sha256", ""))
            parts.append(f"Asked: {field['label']}\nFilled: {value}\nSource: {field['source']}")
        for field in data.get("pending", []):
            parts.append(
                f"Needs answer: {field['label']} · `{field.get('key', '')}`\n{field.get('reason', '')}"
            )
        return "\n\n".join(parts)
    return (
        "**"
        + kind.replace("_", " ").capitalize()
        + "**\n"
        + "\n".join(f"{key}: {value}" for key, value in data.items())
    )


def flush_events(application_id: str):
    thread = ensure_forum(application_id)
    if not thread:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM application_events WHERE application_id=? AND delivery='pending' ORDER BY id",
            (application_id,),
        ).fetchall()
    for row in rows:
        content = event_text(row["kind"], json.loads(row["data"]))
        # Persist chunks before sending; each independently has a stable nonce.
        with db() as conn:
            conn.execute(
                "UPDATE application_events SET delivery='sending' WHERE id=? AND delivery='pending'",
                (row["id"],),
            )
        for index, start in enumerate(range(0, len(content), 1800)):
            discord(
                "POST",
                f"/channels/{thread}/messages",
                {
                    "content": content[start : start + 1800],
                    "allowed_mentions": {"parse": []},
                    "flags": 4,
                    "nonce": f"{row['id']}:{index}",
                    "enforce_nonce": True,
                },
            )
        with db() as conn:
            conn.execute("UPDATE application_events SET delivery='sent' WHERE id=?", (row["id"],))


def action_needed(application_id: str, reason: str):
    settings = config()
    record(application_id, "needs_action", {"reason": reason})
    flush_events(application_id)
    if settings.get("enabled") and settings.get("action_channel_id"):
        discord(
            "POST",
            f"/channels/{settings['action_channel_id']}/messages",
            {
                "content": f"**Application {application_id} needs you**\n{reason[:1300]}\n{forum_url(application_id) or ''}",
                "allowed_mentions": {"parse": []},
                "flags": 4,
            },
        )
        tags = settings.get("tags", {})
        thread = get(application_id)["thread_id"]
        if thread and tags.get("Needs Action"):
            discord(
                "PATCH",
                f"/channels/{thread}",
                {"applied_tags": [tags[t] for t in ("Preparing", "Needs Action") if t in tags]},
            )


def status() -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT id,title,status,url,thread_id,error FROM application_queue ORDER BY created_at DESC LIMIT 25"
        ).fetchall()
    return {"enabled": config().get("enabled", False), "applications": [dict(r) for r in rows]}


def field_key(field: dict) -> str:
    identity = {k: field.get(k) for k in ("label", "name", "id", "kind", "options")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def approved_answers(application_id: str) -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT field_key,value,owner_message_id FROM application_answers WHERE application_id=?",
            (application_id,),
        ).fetchall()
    return {
        r["field_key"]: {
            "value": r["value"],
            "source": "owner Discord message " + r["owner_message_id"],
        }
        for r in rows
    }


def save_result(application_id: str, result: dict):
    write_private(state_root() / f"applications/{application_id}/workflow-result.json", result)
