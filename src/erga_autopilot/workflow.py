"""Durable application queue and Discord flight recorder.

Only trusted local/owner intake or the configured feed policy may enqueue work.
Preparing is distinct from submission; external text cannot authorize either.
"""

import contextlib
import hashlib
import json
import re
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
      CREATE TABLE IF NOT EXISTS owner_notices(
        id INTEGER PRIMARY KEY, application_id TEXT NOT NULL, channel TEXT NOT NULL,
        data TEXT NOT NULL, created_at TEXT NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
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
    if status not in WAITING:
        # The owner channels are to-do lists: a card leaves when the item stops waiting.
        withdraw_notices(application_id)
        refresh_status(application_id)


# States in which a card sits in action-needed or shortlist until the owner replies.
WAITING = {"NEEDS_USER", "READY_FOR_REVIEW", "MANUAL_TAKEOVER", "UNKNOWN_SUBMISSION"}


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
    sync_note(application_id)


def sync_note(application_id: str):
    """Refresh the application's Obsidian note; a missing vault never blocks the workflow."""
    from .vault import sync_application

    with contextlib.suppress(Exception):  # the vault is a readable mirror, not the record
        sync_application(application_id)


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


def brief(text, limit: int = 240) -> str:
    """Whole leading sentences that fit the limit; a paragraph is not a card."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(kept) + len(sentence) + 1 > limit:
            break
        kept = (kept + " " + sentence).strip()
    return kept or text[: limit - 1] + "…"


def source_words(source) -> str:
    """Where a value came from, in the owner's words; internal keys stay internal."""
    text = str(source or "").strip()
    lowered = text.lower()
    if lowered.startswith(("identity.", "education.", "eligibility.", "preferences.", "profile")):
        return "your profile"
    if "owner" in lowered and ("message" in lowered or "answer" in lowered):
        return "your reply"
    if "resume" in lowered:
        return "your resume"
    if lowered in {"intake_source", "feed", "keryx"}:
        return "how the job was found"
    if lowered.startswith(("story", "narrative")):
        return "your story note"
    if lowered.startswith(("erga", "evidence", "career_evidence", "project")):
        return "your evidence"
    if lowered.startswith(("posting", "job", "company", "research")):
        return "the posting"
    return text.replace("_", " ") or "—"


def sources_line(sources) -> str:
    return ", ".join(dict.fromkeys(source_words(s) for s in list(sources or [])[:5]))


STATE_WORDS = {
    "QUEUED": "Back in the queue",
    "PREPARING": "Preparing",
    "NEEDS_USER": "Needs you",
    "READY_FOR_REVIEW": "Ready for your review",
    "SUBMITTING": "Submitting",
    "APPLIED": "Applied",
    "UNKNOWN_SUBMISSION": "Submission unclear",
    "MANUAL_TAKEOVER": "Needs you in the browser",
    "DEFERRED": "Parked",
}


def display_title(item: dict) -> str:
    title = (item.get("title") or "").strip()
    if title.lower().startswith("job application for "):
        title = title[len("job application for ") :]
    return title or "Application"


def command_block(commands) -> str:
    return "```\n" + "\n".join(commands) + "\n```"


def question_lines(questions, limit: int = 6) -> str:
    lines = []
    for index, question in enumerate(list(questions)[:limit], start=1):
        label = clip(question.get("label") or question.get("name") or "Question", 120)
        options = question.get("options") or []
        line = f"{index}. {label}"
        if options:
            line += "  (" + clip(" / ".join(str(o) for o in options[:6]), 100) + ")"
        lines.append(line)
    if len(questions) > limit:
        lines.append(f"…and {len(questions) - limit} more in the thread")
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
                    value = (
                        f"resume PDF `{short_hash(field['sha256'])}` · "
                        f"_{source_words(field.get('source'))}_"
                    )
                else:
                    value = (
                        f"{clip(field.get('value', ''), 300)} · "
                        f"_{source_words(field.get('source'))}_"
                    )
                fields.append((field.get("label") or "Field", value, True))
            cards.append(
                embed(
                    f"Form filled · {len(filled)} fields"
                    if start == 0
                    else "Form filled (continued)",
                    "All from approved facts." if start == 0 else "",
                    color="preparing",
                    fields=fields,
                )
            )
        pending = data.get("pending", [])
        if pending:
            cards.append(
                f"→ {len(pending)} question{'s' if len(pending) != 1 else ''} left for Qwen or you"
            )
        return cards
    if kind == "qwen_job_review":
        decision = str(data.get("decision", "needs_review"))
        label = {"fit": "Fit", "not_fit": "Not a fit", "needs_review": "Your call"}.get(
            decision, decision
        )
        color = {"fit": "applied", "not_fit": "problem"}.get(decision, "needs")
        conflicts = [
            "• " + clip(r.get("requirement", ""), 110)
            for r in data.get("requirements", [])
            if r.get("status") == "conflict" and r.get("kind") != "skills"
        ]
        fields = []
        if conflicts:
            fields.append(("Conflicts with your approved facts", "\n".join(conflicts[:4]), False))
        if data.get("unverified"):
            fields.append(
                (
                    "Stated by the posting, not checkable from your profile",
                    "\n".join("• " + clip(u, 110) for u in data["unverified"][:4]),
                    False,
                )
            )
        return [
            embed(
                f"Job fit · {label}",
                brief(data.get("rationale", ""), 240),
                color=color,
                fields=fields,
                footer="Requirements read by Qwen · facts checked by code",
            )
        ]
    if kind == "qwen_answer_proposal":
        footer = "Draft by Qwen · unused until you reply"
        if data.get("sources"):
            footer += " · from " + sources_line(data["sources"])
        fields = []
        if data.get("approve_command"):
            fields.append(
                (
                    "Reply to approve this exact text",
                    command_block([data["approve_command"]]),
                    False,
                )
            )
        return [
            embed(
                "Draft · " + clip(data.get("label") or data.get("key", ""), 150),
                clip(data.get("value", ""), 1500),
                color="qwen",
                fields=fields,
                footer=clip(footer, 300),
            )
        ]
    if kind == "qwen_question":
        line = f"→ Only you can answer “{clip(data.get('label') or data.get('key', ''), 90)}”"
        if data.get("explanation"):
            line += " · " + brief(data["explanation"], 160)
        return [line]
    if kind == "lifecycle":
        to = str(data.get("to", ""))
        state = STATE_WORDS.get(to, to.replace("_", " ").title())
        line = f"→ {state} · {clip(data.get('trigger', ''), 160)}"
        if data.get("detail"):
            line += f" ({clip(data['detail'], 160)})"
        return [line]
    if kind == "submission_confirmed":
        checks = data.get("checks", {})
        fields = [("Package", f"`{short_hash(data.get('package_hash'))}`", True)]
        if data.get("confirmation_url"):
            fields.append(("Confirmation page", data["confirmation_url"], False))
        signals = [
            k.replace("confirmation_", "").replace("_", " ")
            for k, v in checks.items()
            if k.startswith("confirmation") and v
        ]
        if signals:
            fields.append(("Confirmed by", ", ".join(signals), False))
        return [
            embed("Applied ✅", brief(data.get("reason", ""), 300), color="applied", fields=fields)
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
                "Reply",
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
        return [f"→ Submitting once · package `{short_hash(data.get('package_hash'))}`"]
    if kind == "needs_action":
        return [
            embed(
                data.get("headline") or "Needs you",
                clip(data.get("reason", ""), 600),
                color="needs",
                fields=hold_fields(data),
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
        return [f"→ Opened · {clip(data.get('title', ''), 160)}"]
    if kind == "application_link":
        return [f"→ Clicked {clip(data.get('clicked', ''), 60)}"]
    if kind == "resume_prepared":
        line = "→ Resume ready · " + (
            "tailored from your evidence" if data.get("tailored") else "your approved base PDF"
        )
        line += f" `{short_hash(data.get('sha256'))}`"
        if data.get("warning"):
            line += f" · {clip(data['warning'], 160)}"
        return [line]
    if kind == "resume_preparation_started":
        return ["→ Preparing the resume from your approved evidence"]
    if kind == "browser_retry":
        return ["→ Retried once through the site's front door"]
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
            f"→ You answered `{data.get('field_key', '')}` = {clip(data.get('value', ''), 300)}"
        ]
    if kind.endswith("_requested"):
        word = kind[: -len("_requested")]
        if word == "submit":
            return [
                f"→ You approved the submission · package `{short_hash(data.get('package_hash'))}`"
            ]
        return [f"→ You replied `{word}`"]
    if kind == "account_created":
        return [
            embed(
                "Account created",
                f"{data.get('host', '')} · {data.get('username', '')}\nPassword stored encrypted on this Mac; never posted here.",
                color="preparing",
                fields=[("Filled", ", ".join(data.get("filled", []))[:1000] or "—", False)],
            )
        ]
    if kind in {"signed_in", "sign_in_failed"}:
        return [
            embed(
                "Signed in" if kind == "signed_in" else "Sign-in failed",
                f"{data.get('host', '')} · {data.get('username', '')}",
                color="preparing" if kind == "signed_in" else "problem",
            )
        ]
    if kind == "form_step":
        return [f"→ Clicked {clip(data.get('clicked', 'Next'), 40)} · next form step"]
    if kind == "auto_draft_used":
        return [
            (
                f"→ Used Qwen's draft for “{clip(data.get('label', ''), 80)}” · auto-approve is "
                f"on; reply `answer {application_id} {data.get('key', '')} = …` before it is sent "
                "to change it"
            )
        ]
    if kind == "auto_submit_queued":
        return [
            f"→ Auto-submit is on · sending once · package `{short_hash(data.get('package_hash'))}`"
        ]
    if kind == "optional_skipped":
        return ["→ Left blank · optional: " + clip(", ".join(data.get("labels", [])), 300)]
    if kind == "posting_closed":
        return [f"→ {data.get('source', 'The feed')} reports the posting closed"]
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
        items = event_embeds(application_id, row["kind"], json.loads(row["data"]))
        lines = [item for item in items if isinstance(item, str)]
        cards = [item for item in items if isinstance(item, dict)]
        if not items:
            with db() as conn:
                conn.execute(
                    "UPDATE application_events SET delivery='sent' WHERE id=?", (row["id"],)
                )
            continue
        # Persist before sending; each message independently has a stable nonce.
        with db() as conn:
            conn.execute(
                "UPDATE application_events SET delivery='sending' WHERE id=? AND delivery='pending'",
                (row["id"],),
            )
        try:
            batches = [cards[start : start + 10] for start in range(0, len(cards), 10)] or [[]]
            for index, batch in enumerate(batches):
                message = {
                    "allowed_mentions": {"parse": []},
                    "nonce": f"{row['id']}:{index}",
                    "enforce_nonce": True,
                }
                if batch:
                    message["embeds"] = batch
                if lines and index == 0:
                    message["content"] = clip("\n".join(lines), 1900)
                discord("POST", f"/channels/{thread}/messages", message)
        except (httpx.HTTPError, OSError) as error:
            # Discord is down or rate-limiting: keep the entry pending and try again on
            # the next tick. The application keeps working; the record stays durable.
            delivery_failed("event", row["id"], error)
            with db() as conn:
                conn.execute(
                    "UPDATE application_events SET delivery='pending' WHERE id=?", (row["id"],)
                )
            return
        with db() as conn:
            conn.execute("UPDATE application_events SET delivery='sent' WHERE id=?", (row["id"],))


def delivery_failed(kind: str, row_id, error: Exception):
    """One private line per failed Discord delivery, so a stuck card is diagnosable."""
    detail = ""
    response = getattr(error, "response", None)
    if response is not None:
        detail = f"{response.status_code} {response.text[:200]}"
    line = " ".join(f"{now()} {kind} {row_id} {type(error).__name__} {detail}".split())
    log = state_root() / "logs/delivery-failures.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(line + "\n")


NOTICE_CHANNELS = {"action": "action_channel_id", "shortlist": "shortlist_channel_id"}


def notice(application_id: str, channel: str, payload: dict):
    """One live card per application and channel: the previous card leaves, the new one
    is recorded first and delivered now or on a later tick."""
    if channel not in NOTICE_CHANNELS:
        raise ValueError("Unknown notice channel")
    withdraw_notices(application_id, [channel])
    with db() as conn:
        conn.execute(
            "INSERT INTO owner_notices(application_id,channel,data,created_at) VALUES(?,?,?,?)",
            (application_id, channel, json.dumps(payload), now()),
        )
    flush_notices()
    refresh_status(application_id)


def withdraw_notices(application_id: str, channels=None):
    """Remove the application's cards from the owner channels; the thread keeps the record."""
    settings = config()
    with db() as conn:
        rows = conn.execute(
            "SELECT id,channel,message_id FROM owner_notices WHERE application_id=? "
            "AND delivery='sent'",
            (application_id,),
        ).fetchall()
    for row in rows:
        if channels and row["channel"] not in channels:
            continue
        channel = settings.get(NOTICE_CHANNELS.get(row["channel"], ""))
        if channel and row["message_id"] and settings.get("enabled"):
            try:
                discord("DELETE", f"/channels/{channel}/messages/{row['message_id']}")
            except (httpx.HTTPError, OSError) as error:
                delivery_failed("withdraw", row["id"], error)
        with db() as conn:
            conn.execute("UPDATE owner_notices SET delivery='withdrawn' WHERE id=?", (row["id"],))


def hold_fields(payload: dict) -> list:
    fields = []
    if payload.get("items"):
        fields.append(("Why", "\n".join("• " + clip(i, 140) for i in payload["items"][:4]), False))
    if payload.get("questions"):
        fields.append(("Only you can answer", question_lines(payload["questions"]), False))
    if payload.get("commands"):
        fields.append(("Reply", command_block(payload["commands"][:6]), False))
    return fields


def notice_embed(application_id: str, channel: str, payload: dict) -> dict:
    """The channel card carries the decision; its title links to the thread for the record."""
    item = get(application_id)
    headline = payload.get("headline") or ("Your call" if channel == "shortlist" else "Needs you")
    return embed(
        clip(display_title(item), 120),
        f"**{headline}**\n" + clip(payload.get("reason", ""), 500),
        color="needs",
        url=forum_url(application_id) or item["url"],
        fields=hold_fields(payload),
    )


def flush_notices():
    settings = config()
    if not settings.get("enabled"):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM owner_notices WHERE delivery='pending' ORDER BY id"
        ).fetchall()
    for row in rows:
        channel = settings.get(NOTICE_CHANNELS[row["channel"]])
        if not channel:
            with db() as conn:
                conn.execute("UPDATE owner_notices SET delivery='skipped' WHERE id=?", (row["id"],))
            continue
        card = notice_embed(row["application_id"], row["channel"], json.loads(row["data"]))
        try:
            sent = discord(
                "POST",
                f"/channels/{channel}/messages",
                {
                    "embeds": [card],
                    "allowed_mentions": {"parse": []},
                    "nonce": f"notice:{row['id']}",
                    "enforce_nonce": True,
                },
            )
        except (httpx.HTTPError, OSError) as error:
            delivery_failed("notice", row["id"], error)
            return
        with db() as conn:
            conn.execute(
                "UPDATE owner_notices SET delivery='sent', message_id=? WHERE id=?",
                (str(sent.get("id", "")), row["id"]),
            )


STATUS_LINES = {
    "QUEUED": "Queued · waits for its turn in the recruiting browser",
    "PREPARING": "Preparing in the recruiting browser",
    "SUBMITTING": "Submitting once",
    "APPLIED": "Applied ✅",
    "DEFERRED": "Parked",
}


def refresh_status(application_id: str):
    """The thread's first post is the live status card; the forum list previews it."""
    settings = config()
    item = get(application_id)
    if not settings.get("enabled") or not item["thread_id"]:
        return
    status = item["status"]
    payload = None
    if status in WAITING:
        with db() as conn:
            row = conn.execute(
                "SELECT data FROM owner_notices WHERE application_id=? AND delivery='sent' "
                "ORDER BY id DESC LIMIT 1",
                (application_id,),
            ).fetchone()
        payload = json.loads(row["data"]) if row else None
    if payload:
        headline = payload.get("headline") or "Needs you"
        line = clip(payload.get("reason", ""), 300)
        commands = list(payload.get("commands", []))[:4]
    else:
        headline = STATUS_LINES.get(status, status.replace("_", " ").title())
        line = (
            f"Reply `resume {application_id}` to pick it up again." if status == "DEFERRED" else ""
        )
        commands = []
    fields = [("Posting", clip(item["source_url"], 200), False)]
    if commands:
        fields.append(("Reply", command_block(commands), False))
    card = embed(
        clip(display_title(item), 200),
        f"**{headline}**" + (f"\n{line}" if line else ""),
        color=STATE_COLORS.get(status, "info" if status == "DEFERRED" else "preparing"),
        url=item["url"],
        fields=fields,
        footer=f"Live status · application {application_id} · the thread is the full record",
    )
    try:
        discord(
            "PATCH",
            f"/channels/{item['thread_id']}/messages/{item['thread_id']}",
            {"embeds": [card], "allowed_mentions": {"parse": []}},
        )
    except (httpx.HTTPError, OSError) as error:
        delivery_failed("status", application_id, error)


def flush_pending():
    """Retry every undelivered thread entry and owner-channel card; the worker calls this each tick."""
    with db() as conn:
        waiting = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT application_id FROM application_events "
                "WHERE delivery='pending' AND kind!='forum_creation_attempt'"
            )
        ]
    for application_id in waiting:
        try:
            flush_events(application_id)
        except (RuntimeError, httpx.HTTPError, OSError):
            # An uncertain forum creation waits for reconciliation; a failed one is retried later.
            continue
    flush_notices()


def action_needed(
    application_id: str,
    reason: str,
    *,
    questions=None,
    commands=None,
    headline: str | None = None,
    items=None,
    channel: str = "action",
):
    """One card in the thread and one in an owner channel: what happened, what to reply."""
    item = get(application_id)
    payload = {
        "reason": reason,
        "questions": list(questions or []),
        "commands": list(commands or []),
        "headline": headline,
        "items": list(items or []),
    }
    record(application_id, "needs_action", payload)
    flush_events(application_id)
    notice(application_id, channel, payload)
    apply_tags(application_id, STATE_TAGS.get(item["status"], ["Preparing", "Needs Action"]))
    sync_note(application_id)


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
