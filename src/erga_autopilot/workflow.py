"""Durable application queue and Discord flight recorder.

Only trusted local/owner intake or the configured feed policy may enqueue work.
Preparing is distinct from submission; external text cannot authorize either.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime

import httpx

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


STATES = {
    "QUEUED",
    "PREPARING",
    "NEEDS_USER",
    "READY_FOR_REVIEW",
    "SUBMITTING",
    "APPLIED",
    "UNKNOWN_SUBMISSION",
    "MANUAL_TAKEOVER",
    "DEFERRED",
}
# Lifecycle tag is the quick status; overlays mark work the owner must do.
STATE_TAGS = {
    "QUEUED": ["Preparing"],
    "PREPARING": ["Preparing"],
    "DEFERRED": ["Preparing"],
    "NEEDS_USER": ["Preparing", "Needs Action"],
    "READY_FOR_REVIEW": ["Preparing", "Needs Action"],
    "MANUAL_TAKEOVER": ["Preparing", "Needs Action"],
    "SUBMITTING": ["Preparing"],
    "UNKNOWN_SUBMISSION": ["Preparing", "Needs Action"],
    "APPLIED": ["Applied"],
}


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
    if status not in STATES:
        raise ValueError("Unknown application state")
    with db() as conn:
        conn.execute(
            "UPDATE application_queue SET status=?,updated_at=?"
            + "".join("," + key + "=?" for key in values)
            + " WHERE id=?",
            [status, now(), *values.values(), application_id],
        )


def apply_tags(application_id: str, names: list[str]):
    """Best-effort forum tag update; the timeline entry, not the tag, is the record."""
    settings = config()
    tags = settings.get("tags", {})
    thread = get(application_id)["thread_id"]
    wanted = [tags[name] for name in names if name in tags]
    if not settings.get("enabled") or not thread or not wanted:
        return
    try:
        discord("PATCH", f"/channels/{thread}", {"applied_tags": wanted})
    except Exception as error:  # noqa: BLE001 -- a tag is cosmetic; the event stays durable
        record(application_id, "discord_tag_failed", {"tags": names, "error": type(error).__name__})


def transition(application_id: str, status: str, trigger: str, detail: str = "", **values):
    """A lifecycle change always leaves a timeline entry explaining why."""
    previous = get(application_id)["status"]
    set_state(application_id, status, **values)
    record(
        application_id,
        "lifecycle",
        {"from": previous, "to": status, "trigger": trigger, "detail": detail[:1200]},
    )
    flush_events(application_id)
    apply_tags(application_id, STATE_TAGS.get(status, ["Preparing"]))


def owner_override(application_id: str, kind: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM owner_commands WHERE application_id=? AND kind=? AND status='applied'",
            (application_id, kind),
        ).fetchone()
    return bool(row)


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


COLORS = {
    "preparing": 0x5865F2,
    "needs": 0xF2A93B,
    "applied": 0x57F287,
    "problem": 0xED4245,
    "qwen": 0x9B59B6,
    "info": 0x99AAB5,
}
STATE_COLORS = {
    "APPLIED": "applied",
    "UNKNOWN_SUBMISSION": "problem",
    "MANUAL_TAKEOVER": "problem",
    "NEEDS_USER": "needs",
    "READY_FOR_REVIEW": "needs",
}


def clip(text, limit: int) -> str:
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def embed(
    title: str, description: str = "", *, color: str = "info", fields=(), url=None, footer=None
):
    """One Discord embed within the platform's size limits."""
    item = {"title": clip(title, 256) or "Update", "color": COLORS[color]}
    if description:
        item["description"] = clip(description, 2000)
    if url:
        item["url"] = url
    if fields:
        item["fields"] = [
            {
                "name": clip(name, 256) or "\u200b",
                "value": clip(value, 1024) or "—",
                "inline": inline,
            }
            for name, value, inline in list(fields)[:25]
        ]
    if footer:
        item["footer"] = {"text": clip(footer, 2048)}
    return item


def short_hash(value) -> str:
    return str(value or "")[:12]


def display_title(item: dict) -> str:
    title = (item.get("title") or "").strip()
    if title.lower().startswith("job application for "):
        title = title[len("job application for ") :]
    return title or "Application"


def command_block(commands) -> str:
    return "```\n" + "\n".join(commands) + "\n```"


def question_lines(questions, limit: int = 10) -> str:
    lines = []
    for question in list(questions)[:limit]:
        label = clip(question.get("label") or question.get("name") or "Question", 110)
        line = f"• {label} · `{question.get('key', '')}`"
        options = question.get("options") or []
        if options:
            line += "\n  options: " + clip(", ".join(str(o) for o in options[:8]), 160)
        lines.append(line)
    if len(questions) > limit:
        lines.append(f"• …and {len(questions) - limit} more in the forum")
    return "\n".join(lines)


def event_embeds(application_id: str, kind: str, data: dict) -> list[dict]:
    """Glanceable forum entries: one card per event, values in fields, commands in code."""
    if kind == "forum_creation_attempt":
        return []
    if kind == "fields_prepared":
        cards = []
        filled = data.get("filled", [])
        for start in range(0, len(filled), 24):
            fields = []
            for field in filled[start : start + 24]:
                if "sha256" in field:
                    value = f"resume PDF · `{short_hash(field['sha256'])}`\n_{field['source']}_"
                else:
                    value = f"{clip(field.get('value', ''), 300)}\n_{clip(field.get('source', ''), 120)}_"
                fields.append((field.get("label") or "Field", value, True))
            cards.append(
                embed(
                    "Form filled" if start == 0 else "Form filled (continued)",
                    f"{len(filled)} fields filled from approved facts" if start == 0 else "",
                    color="preparing",
                    fields=fields,
                )
            )
        pending = data.get("pending", [])
        if pending:
            cards.append(
                embed(
                    f"Waiting on you · {len(pending)} question{'s' if len(pending) != 1 else ''}",
                    question_lines(pending),
                    color="needs",
                    footer="Qwen drafts what it can; unknown facts come to you",
                )
            )
        return cards
    if kind == "qwen_job_review":
        decision = str(data.get("decision", "needs_review"))
        label = {"fit": "Fit", "not_fit": "Not a fit", "needs_review": "Your call"}.get(
            decision, decision
        )
        color = {"fit": "applied", "not_fit": "problem"}.get(decision, "needs")
        groups = {"satisfied": [], "unknown": [], "conflict": []}
        for item in data.get("requirements", []):
            line = clip(item.get("requirement", ""), 90)
            if item.get("checked_by") == "code":
                line += " ✓code"
            groups.setdefault(item.get("status", "unknown"), []).append("• " + line)
        fields = []
        if groups["satisfied"]:
            fields.append(("Satisfied", "\n".join(groups["satisfied"][:8]), False))
        if groups["unknown"]:
            fields.append(("Unknown", "\n".join(groups["unknown"][:6]), False))
        if groups["conflict"]:
            fields.append(("Conflict", "\n".join(groups["conflict"][:6]), False))
        footer = "Qwen extracted the requirements · code checked dates and approved facts"
        if data.get("qwen_decision") and data["qwen_decision"] != decision:
            footer += f" · Qwen said {data['qwen_decision']}"
        if data.get("note"):
            footer += " · " + data["note"]
        return [
            embed(
                f"Job fit · {label}",
                clip(data.get("rationale", ""), 700),
                color=color,
                fields=fields,
                footer=footer,
            )
        ]
    if kind == "qwen_answer_proposal":
        fields = [
            ("Sources", ", ".join(data.get("sources", [])) or "—", False),
            ("Why", clip(data.get("explanation", ""), 400), False),
        ]
        if data.get("unslop"):
            fields.append(("Unslop", clip(data["unslop"], 300), False))
        if data.get("approve_command"):
            fields.append(
                ("Approve exactly this text", command_block([data["approve_command"]]), False)
            )
        return [
            embed(
                "Draft · " + clip(data.get("label") or data.get("key", ""), 200),
                data.get("value", ""),
                color="qwen",
                fields=fields,
                footer="Qwen through Hermes · unapproved until you reply",
            )
        ]
    if kind == "qwen_question":
        return [
            embed(
                "Needs you · " + clip(data.get("label") or data.get("key", ""), 200),
                clip(data.get("explanation", ""), 600),
                color="needs",
                fields=[
                    (
                        "Reply with",
                        command_block(
                            [f"answer {application_id} {data.get('key', '')} = your answer"]
                        ),
                        False,
                    )
                ],
            )
        ]
    if kind == "lifecycle":
        to = str(data.get("to", ""))
        return [
            embed(
                f"{data.get('from', '?')} → {to}",
                clip(data.get("trigger", ""), 300)
                + (("\n" + clip(data.get("detail", ""), 600)) if data.get("detail") else ""),
                color=STATE_COLORS.get(to, "preparing"),
            )
        ]
    if kind == "submission_confirmed":
        checks = data.get("checks", {})
        fields = [("Package", f"`{short_hash(data.get('package_hash'))}`", True)]
        if data.get("confirmation_url"):
            fields.append(("Confirmation page", data["confirmation_url"], False))
        if checks:
            fields.append(
                (
                    "Checks",
                    "\n".join(
                        f"{'✅' if v else '❌'} {k}" for k, v in checks.items() if k != "confirmed"
                    ),
                    False,
                )
            )
        if data.get("erga"):
            fields.append(("Erga", clip(json.dumps(data["erga"]), 200), False))
        return [
            embed("Applied ✅", clip(data.get("reason", ""), 300), color="applied", fields=fields)
        ]
    if kind == "submission_unknown":
        checks = data.get("checks", {})
        fields = [("Package", f"`{short_hash(data.get('package_hash'))}`", True)]
        if checks:
            fields.append(
                (
                    "Checks",
                    "\n".join(
                        f"{'✅' if v else '❌'} {k}" for k, v in checks.items() if k != "confirmed"
                    ),
                    False,
                )
            )
        fields.append(
            (
                "Reply with",
                command_block(
                    [
                        f"reconcile {application_id} applied",
                        f"reconcile {application_id} not-submitted",
                    ]
                ),
                False,
            )
        )
        return [
            embed(
                "Submission unclear · do not click again",
                clip(data.get("reason", ""), 600),
                color="problem",
                fields=fields,
            )
        ]
    if kind == "submit_attempt":
        return [
            embed(
                "Submitting once",
                f"Package `{short_hash(data.get('package_hash'))}` · adapter {data.get('adapter', '')}",
                color="preparing",
            )
        ]
    if kind == "needs_action":
        fields = []
        if data.get("questions"):
            fields.append(("Questions", question_lines(data["questions"]), False))
        if data.get("commands"):
            fields.append(("Reply with", command_block(data["commands"]), False))
        return [
            embed(
                data.get("headline") or "Needs you",
                clip(data.get("reason", ""), 1200),
                color="needs",
                fields=fields,
            )
        ]
    if kind == "shortlisted":
        fields = []
        if data.get("items"):
            fields.append(("Why", "\n".join("• " + clip(i, 160) for i in data["items"][:8]), False))
        if data.get("commands"):
            fields.append(("Reply with", command_block(data["commands"]), False))
        return [embed("Your call", clip(data.get("reason", ""), 800), color="needs", fields=fields)]
    if kind == "opened":
        return [
            embed("Opened", clip(data.get("title", ""), 200), color="info", url=data.get("url"))
        ]
    if kind == "application_link":
        return [
            embed(
                f"Clicked · {clip(data.get('clicked', ''), 80)}", data.get("url", ""), color="info"
            )
        ]
    if kind == "resume_prepared":
        fields = [
            ("Tailored", "yes" if data.get("tailored") else "no, approved base PDF", True),
            ("SHA-256", f"`{short_hash(data.get('sha256'))}`", True),
        ]
        if data.get("warning"):
            fields.append(("Warning", clip(data["warning"], 300), False))
        return [
            embed(
                "Resume ready", clip(data.get("review", ""), 200), color="preparing", fields=fields
            )
        ]
    if kind == "resume_preparation_started":
        return [embed("Preparing resume", "Erga job intake from approved evidence", color="info")]
    if kind == "qwen_failure":
        return [
            embed(
                "Qwen run failed · " + str(data.get("phase", "")).replace("_", " "),
                clip(data.get("reason", ""), 500),
                color="problem",
            )
        ]
    if kind == "owner_answer":
        return [
            embed(
                "You answered",
                f"`{data.get('field_key', '')}` = {clip(data.get('value', ''), 900)}",
                color="applied",
            )
        ]
    if kind.endswith("_requested"):
        extra = {k: v for k, v in data.items() if k not in {"kind"}}
        return [
            embed(
                "You asked · " + kind[: -len("_requested")],
                clip(", ".join(f"{k}: {v}" for k, v in extra.items()), 300),
                color="info",
            )
        ]
    if kind == "browser_access_blocked":
        return [
            embed(
                "Employer blocked the recruiting browser",
                clip(data.get("reason", data.get("url", "")), 600),
                color="problem",
            )
        ]
    fields = [(str(k), clip(v, 400), False) for k, v in list(data.items())[:10]]
    return [embed(kind.replace("_", " ").capitalize(), "", color="info", fields=fields)]


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
    title = display_title(item)
    thread = discord(
        "POST",
        f"/channels/{settings['forum_channel_id']}/threads",
        {
            "name": clip(title, 80),
            "auto_archive_duration": 10080,
            "applied_tags": [settings["tags"]["Preparing"]]
            if settings.get("tags", {}).get("Preparing")
            else [],
            "message": {
                "embeds": [
                    embed(
                        title,
                        "Preparing in the recruiting browser. Nothing is submitted without your `submit` reply.",
                        color="preparing",
                        url=item["url"],
                        fields=[
                            ("Application", f"`{application_id}`", True),
                            ("Source", item["source"].replace("_", " "), True),
                            ("Posting", clip(item["source_url"], 300), False),
                        ],
                    )
                ],
                "allowed_mentions": {"parse": []},
            },
        },
    )
    set_state(application_id, item["status"], thread_id=thread["id"])
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO workflow_checkpoints VALUES(?,?)", (thread["id"], thread["id"])
        )
    with db() as conn:
        conn.execute(
            "UPDATE application_events SET delivery='sent' WHERE application_id=? AND kind='forum_creation_attempt'",
            (application_id,),
        )
    return thread["id"]


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
        cards = event_embeds(application_id, row["kind"], json.loads(row["data"]))
        # Persist before sending; each message independently has a stable nonce.
        with db() as conn:
            conn.execute(
                "UPDATE application_events SET delivery='sending' WHERE id=? AND delivery='pending'",
                (row["id"],),
            )
        try:
            for index, start in enumerate(range(0, len(cards), 10)):
                discord(
                    "POST",
                    f"/channels/{thread}/messages",
                    {
                        "embeds": cards[start : start + 10],
                        "allowed_mentions": {"parse": []},
                        "flags": 4,
                        "nonce": f"{row['id']}:{index}",
                        "enforce_nonce": True,
                    },
                )
        except (httpx.HTTPError, OSError):
            # Discord is down or rate-limiting: keep the entry pending and try again on
            # the next flush. The application keeps working; the record stays durable.
            with db() as conn:
                conn.execute(
                    "UPDATE application_events SET delivery='pending' WHERE id=?", (row["id"],)
                )
            return
        with db() as conn:
            conn.execute("UPDATE application_events SET delivery='sent' WHERE id=?", (row["id"],))


def action_needed(
    application_id: str,
    reason: str,
    *,
    questions=None,
    commands=None,
    headline: str | None = None,
):
    """One card in the thread and one in action-needed: what happened, what to reply."""
    settings = config()
    item = get(application_id)
    payload = {
        "reason": reason,
        "questions": list(questions or []),
        "commands": list(commands or []),
        "headline": headline,
    }
    record(application_id, "needs_action", payload)
    flush_events(application_id)
    if settings.get("enabled") and settings.get("action_channel_id"):
        fields = [("Application", f"`{application_id}`", True)]
        link = forum_url(application_id)
        if link:
            fields.append(("Forum", link, True))
        if payload["questions"]:
            fields.append(("Questions", question_lines(payload["questions"]), False))
        if payload["commands"]:
            fields.append(("Reply with", command_block(payload["commands"]), False))
        discord(
            "POST",
            f"/channels/{settings['action_channel_id']}/messages",
            {
                "embeds": [
                    embed(
                        headline or f"{clip(display_title(item), 90)} · needs you",
                        clip(reason, 1200),
                        color="needs",
                        url=link or item["url"],
                        fields=fields,
                    )
                ],
                "allowed_mentions": {"parse": []},
                "flags": 4,
            },
        )
    apply_tags(application_id, STATE_TAGS.get(item["status"], ["Preparing", "Needs Action"]))


def shortlist(application_id: str, reason: str, items=(), commands=()):
    """A borderline job goes to the owner's shortlist with the reasons, once."""
    settings = config()
    item = get(application_id)
    payload = {"reason": reason, "items": list(items), "commands": list(commands)}
    record(application_id, "shortlisted", payload)
    flush_events(application_id)
    if settings.get("enabled") and settings.get("shortlist_channel_id"):
        fields = [("Application", f"`{application_id}`", True)]
        link = forum_url(application_id)
        if link:
            fields.append(("Forum", link, True))
        if payload["items"]:
            fields.append(
                ("Why", "\n".join("• " + clip(i, 160) for i in payload["items"][:8]), False)
            )
        if payload["commands"]:
            fields.append(("Reply with", command_block(payload["commands"]), False))
        discord(
            "POST",
            f"/channels/{settings['shortlist_channel_id']}/messages",
            {
                "embeds": [
                    embed(
                        "Your call · " + clip(display_title(item), 150),
                        clip(reason, 800),
                        color="needs",
                        url=item["url"],
                        fields=fields,
                    )
                ],
                "allowed_mentions": {"parse": []},
                "flags": 4,
            },
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
            "source": (
                "owner setup reply "
                if r["owner_message_id"].startswith("codex-owner-reply:")
                else "owner Discord message "
            )
            + r["owner_message_id"],
        }
        for r in rows
    }


def save_result(application_id: str, result: dict):
    write_private(state_root() / f"applications/{application_id}/workflow-result.json", result)
