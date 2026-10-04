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

from . import job_index
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
      CREATE TABLE IF NOT EXISTS owner_command_effects(
        message_id TEXT PRIMARY KEY, effects TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_checkpoints(channel_id TEXT PRIMARY KEY, message_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS application_answers(
        application_id TEXT NOT NULL, field_key TEXT NOT NULL, value TEXT NOT NULL,
        owner_message_id TEXT NOT NULL, PRIMARY KEY(application_id,field_key));
      CREATE TABLE IF NOT EXISTS live_submission_attempts(
        application_id TEXT PRIMARY KEY, package_hash TEXT NOT NULL,
        owner_message_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS submission_outcomes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT NOT NULL,
        status TEXT NOT NULL, evidence TEXT NOT NULL, plan TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS answer_memory(
        fingerprint TEXT PRIMARY KEY, label TEXT NOT NULL, options TEXT NOT NULL,
        value TEXT NOT NULL, created_at TEXT NOT NULL, owner_message_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS owner_notices(
        id INTEGER PRIMARY KEY, application_id TEXT NOT NULL, channel TEXT NOT NULL,
        data TEXT NOT NULL, created_at TEXT NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
    """)
    job_index.ensure(conn)  # one application and one send per job, not per URL spelling
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

# ---------------------------------------------------------------------------
# Recruiting lifecycle after submission.
#
# Recruiting mail (mail.py) moves an application that was already sent through
# OA, INTERVIEW, OFFER and REJECTED, in that order, and REJECTED from any of
# them. Nothing moves a sent application back into preparation: not a mail, not
# a thread reply. The forum tags of the same names are the quick status; the
# `recruiting_mail` card and the lifecycle line in the thread are the record.
# ---------------------------------------------------------------------------
POST_APPLICATION = ("APPLIED", "OA", "INTERVIEW", "OFFER", "REJECTED")
STATES |= set(POST_APPLICATION)
STATE_TAGS |= {
    "OA": ["OA", "Needs Action"],
    "INTERVIEW": ["Interview", "Needs Action"],
    "OFFER": ["Offer", "Needs Action"],
    "REJECTED": ["Rejected"],
}
# What a classified mail is called in the thread and the recruiting channel.
MAIL_LABEL_WORDS = {
    "acknowledgement": "Application received",
    "oa": "Online assessment",
    "interview": "Interview",
    "offer": "Offer",
    "rejection": "Rejected",
    "other": "Recruiting mail",
}


def advances(current: str, target: str) -> bool:
    """Whether a mail-driven target state is a step forward from the current one."""
    if current not in POST_APPLICATION or target not in POST_APPLICATION:
        return False
    if current == "REJECTED":
        return False
    if target == "REJECTED":
        return True
    return POST_APPLICATION.index(target) > POST_APPLICATION.index(current)


def guard_regression(current: str, status: str):
    if current in POST_APPLICATION and status not in POST_APPLICATION:
        raise PermissionError("An application that was sent is never prepared again")


# ---------------------------------------------------------------------------


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
    guard_regression(get(application_id)["status"], status)
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


# Nothing has been sent and nothing is being sent: the application still follows the
# approved profile.
UNSENT = ("QUEUED", "PREPARING", "NEEDS_USER", "READY_FOR_REVIEW", "MANUAL_TAKEOVER", "DEFERRED")
# Built from one profile version; rebuilt when the application takes another.
PROFILE_BOUND_FILES = ("package.json", "answer-proposals.json")


def adopt_profile() -> int:
    """Applications not yet sent follow the approved profile; returns how many changed.

    Approving a profile change is the owner's decision about his own facts. An application
    that has not been sent has nothing frozen worth keeping, so it takes the new version
    instead of stopping with a card: its frozen copy is replaced, and a package or drafts
    built from the old version are dropped so they are rebuilt (his own answers are
    kept). A package that was waiting for `send it` goes back to the queue, because what
    it would send has changed. Sent applications and sends in flight keep the snapshot
    they used. An unreadable profile changes nothing here; the profile gate reports it.
    """
    try:
        approved = read_approved()
    except Exception:  # noqa: BLE001 -- an invalid profile is reported by the profile gate
        return 0
    current = approved["profile_hash"]
    marks = ",".join("?" * len(UNSENT))
    with db() as conn:
        # Only question marks are formatted in; every value is bound.
        stale = f"profile_hash!=? AND status IN ({marks})"
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id,status FROM application_queue WHERE " + stale, (current, *UNSENT)
            )
        ]
        if not rows:
            return 0
        conn.execute(
            "UPDATE application_queue SET profile_hash=? WHERE " + stale,
            (current, current, *UNSENT),
        )
    rebuilt = []
    for row in rows:
        directory = state_root() / "applications" / row["id"]
        if not directory.is_dir():
            continue
        for name in PROFILE_BOUND_FILES:
            (directory / name).unlink(missing_ok=True)
        if (directory / "profile.json").exists():
            write_private(directory / "profile.json", approved)
        run_file = directory / "run.json"
        if run_file.exists():
            with contextlib.suppress(ValueError, OSError):
                run = json.loads(run_file.read_text())
                write_private(run_file, {**run, "profile_hash": current})
        if row["status"] == "READY_FOR_REVIEW":
            transition(
                row["id"],
                "QUEUED",
                "profile",
                "Your approved profile changed, so this one is being prepared again with it.",
            )
        rebuilt.append(row["id"])
    system_line(
        "profile",
        f"approved profile changed · {len(rows)} unsent application"
        f"{'s' if len(rows) != 1 else ''} now use it · {len(rebuilt)} with files rebuilt",
    )
    return len(rows)


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


# ---------------------------------------------------------------------------
# Who put a link in the queue decides what it may skip. Nothing a page, a mail or
# the model says can raise a link's source; only a caller that names one can.
#
#   owner_link  a link code parsed out of the owner's own message in agent-control, or
#               one he queued from his own terminal (`rove workflow enqueue`)
#   owner_pick  a job the owner picked from a digest
#   feed        the configured job feed (stored as `keryx`)
#   agent       anything else: a link the model queued or opened through MCP
#
# owner_decided  the owner already made the call: no fit hold, no exclusion pruning,
#                worked ahead of the queue and its pacing
# unattended     may be sent under the auto_submit policy without a reply
# opens_unseen   the browser may open it before the owner has seen it
# ---------------------------------------------------------------------------
SOURCES = {
    "owner_link": {"rank": 3, "owner_decided": True, "unattended": True, "opens_unseen": True},
    "owner_pick": {"rank": 2, "owner_decided": True, "unattended": True, "opens_unseen": True},
    "feed": {"rank": 1, "owner_decided": False, "unattended": True, "opens_unseen": True},
    "agent": {"rank": 0, "owner_decided": False, "unattended": False, "opens_unseen": False},
}
SOURCE_ALIASES = {"keryx": "feed"}
INTAKE_HEADLINE = "Link needs your OK"


def source_policy(source) -> dict:
    """What a queue row's source allows; a source nobody listed gets the least."""
    name = str(source or "")
    return SOURCES.get(SOURCE_ALIASES.get(name, name), SOURCES["agent"])


def known_job_host(url: str) -> bool:
    """A job board, or a host the feed lists or the owner has already sent a link to."""
    from urllib.parse import urlsplit

    from .live_browser import approved_ats

    if approved_ats(url):
        return True
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    like = "https://" + re.sub(r"([\\%_])", r"\\\1", host) + "/%"
    trusted = [name for name in (*SOURCES, *SOURCE_ALIASES) if source_policy(name)["rank"] > 0]
    marks = ",".join("?" * len(trusted))
    with db() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM jobs WHERE url LIKE ? ESCAPE '\\' LIMIT 1", (like,)
            ).fetchone()
            or conn.execute(
                f"SELECT 1 FROM application_queue WHERE source IN ({marks}) "
                "AND (url LIKE ? ESCAPE '\\' OR source_url LIKE ? ESCAPE '\\') LIMIT 1",
                (*trusted, like, like),
            ).fetchone()
        )


def owner_said_go(application_id: str) -> bool:
    return owner_override(application_id, "resume") or owner_override(application_id, "proceed")


def intake_hold(item: dict) -> str | None:
    """Why the browser may not open this queued link yet, in the owner's words, or None.

    A link the owner or the feed supplied opens as before. Any other link waits for the
    owner's `go` when it carries a query string (a place to smuggle data out) or goes to
    a host that is not a known job board or careers site.
    """
    if source_policy(item["source"])["opens_unseen"] or owner_said_go(item["id"]):
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(item["url"])
    host = re.sub(r"[^a-z0-9.-]", "", (parts.hostname or "").lower()) or "an unknown site"
    if parts.query:
        why = f"it carries extra data after a `?` (it goes to `{clip(host, 80)}`)"
    elif not known_job_host(item["url"]):
        why = f"`{clip(host, 80)}` is not a job board or careers site I know from the feed or from you"
    else:
        return None
    return (
        f"The agent queued this link, you did not paste it yourself, and {why}. Nothing was "
        "opened. Check the link in the thread, then reply `go` to work it or `park it`."
    )


def sends_unattended(item: dict) -> bool:
    """Whether the auto_submit policy covers this application: the owner's own links and
    feed jobs, and anything else only after the owner replied `go` on it."""
    return source_policy(item["source"])["unattended"] or owner_said_go(item["id"])


def raise_source(row: dict, source: str, title: str = "") -> dict:
    """A better-vouched source for a link that is already queued replaces the weaker one,
    and names the job if the row had no title; a weaker one never changes the row. A
    wait that existed only because of the old source ends."""
    if source_policy(source)["rank"] <= source_policy(row["source"])["rank"]:
        return row
    with db() as conn:
        conn.execute(
            "UPDATE application_queue SET source=?,updated_at=?,"
            "title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
            (source, now(), title[:300], row["id"]),
        )
    if (
        row["status"] == "NEEDS_USER"
        and (latest_hold(row["id"]) or {}).get("headline") == INTAKE_HEADLINE
    ):
        set_state(row["id"], "QUEUED")
    return get(row["id"])


def cleaner_link(row: dict, target: str, safe: str, source: str) -> dict:
    """The same job queued again under a link without the query string the stored one
    carries: a source at least as well vouched for replaces the link, since the job is
    pinned by its key and the query was the one thing an intake hold could be about. Only
    while nothing has been opened; a wait that existed only because of the query ends."""
    from urllib.parse import urlsplit

    held_at_intake = (
        row["status"] == "NEEDS_USER"
        and (latest_hold(row["id"]) or {}).get("headline") == INTAKE_HEADLINE
    )
    if (
        row["url"] == target
        or urlsplit(target).query
        or not urlsplit(row["url"]).query
        or source_policy(source)["rank"] < source_policy(row["source"])["rank"]
        or not (row["status"] == "QUEUED" or held_at_intake)
    ):
        return row
    with db() as conn:
        conn.execute(
            "UPDATE application_queue SET url=?,source_url=?,updated_at=? WHERE id=?",
            (target, safe, now(), row["id"]),
        )
    if held_at_intake:
        set_state(row["id"], "QUEUED")
    return get(row["id"])


def enqueue(url: str, *, source: str = "owner_link", title: str = "") -> dict:
    # Callers in src/ always name the source; the default serves tests that play the owner.
    if source not in SOURCES and source not in SOURCE_ALIASES:
        raise ValueError("Unknown intake source")
    safe = public_link(url.strip().strip("<>\"'"))
    if not safe:
        raise ValueError("Use a complete public HTTPS job link")
    target = resolve_alias(safe)
    profile = read_approved()
    application_id = uuid.uuid4().hex[:12]
    with db() as conn:
        # One transaction: the job-key lookup and the insert cannot interleave with
        # another enqueue of the same job under a different spelling of its link.
        conn.execute("BEGIN IMMEDIATE")
        existing = job_index.existing_application(conn, target)
        if existing is None:
            conn.execute(
                "INSERT INTO application_queue "
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
            job_index.register(conn, application_id, target)
        row = dict(
            conn.execute(
                "SELECT * FROM application_queue WHERE id=?", (existing or application_id,)
            ).fetchone()
        )
    already_exists = row["id"] != application_id
    if already_exists:
        row = cleaner_link(row, target, safe, source)
        row = raise_source(row, source, title)
    waits = row["status"] == "QUEUED" and intake_hold(row) is not None
    return {
        "application_id": row["id"],
        "status": row["status"],
        "url": row["url"],
        "already_exists": already_exists,
        "submission_enabled": False,
        "waits_for_owner": waits,
        "next_action": "The owner has to reply go on this link's card before the browser opens it."
        if waits
        else "The local worker opens the visible browser and records preparation in the application forum.",
    }


def record(application_id: str, kind: str, data: dict):
    # Runtime callers supply bounded application events, never credentials or raw page text.
    with db() as conn:
        conn.execute(
            "INSERT INTO application_events(application_id,kind,data,created_at) VALUES(?,?,?,?)",
            (application_id, kind, json.dumps(data), now()),
        )
    note = system_note(kind, data)
    if note:
        system_line(application_id, note)


def latest_hold(application_id: str) -> dict | None:
    """The newest needs_action payload: the headline and numbered questions the owner sees."""
    with db() as conn:
        row = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? AND kind='needs_action' "
            "ORDER BY id DESC LIMIT 1",
            (application_id,),
        ).fetchone()
    return json.loads(row["data"]) if row else None


def system_note(kind: str, data: dict) -> str | None:
    """The terse system-log line for an event, identifiers included; None for routine steps."""
    package = data.get("package_hash", "")
    reason = clip(" ".join(str(data.get("reason", "")).split()), 160)
    if kind == "lifecycle":
        return f"{data.get('from')} → {data.get('to')} · {clip(data.get('trigger', ''), 120)}"
    if kind == "needs_action":
        return f"held · {data.get('headline') or 'Needs you'}"
    if kind == "submit_requested":
        return f"submit approved · package {package}"
    if kind == "auto_submit_queued":
        return f"auto-submit queued · package {package}"
    if kind == "submit_attempt":
        return f"submit attempt · {data.get('adapter', '')} · package {package}"
    if kind == "submission_confirmed":
        return f"applied · package {package} · {data.get('confirmation_url', '')}"
    if kind == "submission_unknown":
        return f"submission unknown · package {package} · {reason}"
    if kind == "submission_rejected":
        return f"form rejected · package {package} · {reason}"
    if kind == "qwen_failure":
        return f"qwen failure · {data.get('phase', '')} · {reason}"
    if kind == "model_unavailable":
        return f"model unavailable · {data.get('phase', '')}"
    if kind == "discord_tag_failed":
        return f"forum tag not set · {', '.join(data.get('tags') or [])} · {data.get('error', '')}"
    if kind == "browser_access_blocked":
        return f"blocked · {data.get('url', '')} · {data.get('marker', '')}"
    if kind == "overlay":
        return clip(str(data.get("detail") or data.get("line") or ""), 400)
    if kind == "recruiting_mail":
        return (
            f"recruiting mail · {data.get('label', '')} · {data.get('sender_domain', '')} · "
            f"message {data.get('message_id', '')} · by {data.get('classifier', '')}"
        )
    return None


def system_line(application_id: str, text: str):
    """One plain line in system-log with the identifiers the owner's cards leave out.

    Written to the system outbox first, only when `system_channel_id` is configured, and
    delivered with the other waiting lines in as few messages as fit: at once, or at the
    end of the tick while an application is being prepared and sent. Never raises.
    Callers pass ids, hashes, urls and reasons, never secrets or applicant values.
    """
    from . import delivery

    settings = config()
    channel = settings.get("system_channel_id")
    if not settings.get("enabled") or not channel:
        return
    try:
        delivery.queue_system(application_id, clip(f"`{application_id}` · {text}", 1900))
        if not delivery.window_open():
            delivery.deliver_system(channel)
    except Exception as error:  # noqa: BLE001 -- a log line must never break the workflow
        delivery_failed("system", application_id, error)


def flush_system():
    """Deliver the waiting system-log lines; never raises."""
    from . import delivery

    settings = config()
    channel = settings.get("system_channel_id")
    if not settings.get("enabled") or not channel:
        return
    try:
        if delivery.system_waiting():
            delivery.deliver_system(channel)
    except Exception as error:  # noqa: BLE001 -- a log line must never break the workflow
        delivery_failed("system", "outbox", error)


def ensure_system_channel() -> str | None:
    """Look up the system-log channel by name once and keep its id in the private config."""
    return ensure_named_channel("system_channel_id", "system-log")


def ensure_recruiting_channel() -> str | None:
    """Look up the recruiting channel by name once and keep its id in the private config."""
    return ensure_named_channel("recruiting_channel_id", "recruiting")


def ensure_memory_channel() -> str | None:
    """Look up the memory channel by name once and keep its id in the private config."""
    return ensure_named_channel("memory_channel_id", "memory")


def ensure_named_channel(key: str, name: str) -> str | None:
    settings = config()
    if settings.get(key):
        return settings[key]
    if not settings.get("enabled") or not settings.get("guild_id"):
        return None
    try:
        channels = discord("GET", f"/guilds/{settings['guild_id']}/channels")
    except Exception:  # noqa: BLE001 -- no channel means no line there, nothing else
        return None
    found = next(
        (
            str(c["id"])
            for c in (channels if isinstance(channels, list) else [])
            if isinstance(c, dict) and c.get("name") == name and c.get("id")
        ),
        None,
    )
    if not found:
        return None
    path = state_root() / "config/workflow.json"
    stored = json.loads(path.read_text()) if path.exists() else {}
    stored[key] = found
    write_private(path, stored)
    return found


def recruiting_line(application_id: str, text: str, mail: dict | None = None):
    """One message in the recruiting channel: what an employer's mail means, in words,
    with a link to the thread, and under it the mail itself as the owner would read it in
    his inbox. Best effort, like the system log; never an id."""
    settings = config()
    channel = settings.get("recruiting_channel_id")
    if not settings.get("enabled") or not channel:
        return
    link = forum_url(application_id)
    content = text + (f" · <{link}>" if link else "")
    payload: dict = {"content": clip(content, 1900), "allowed_mentions": {"parse": []}}
    if mail:
        card = mail_card(mail)
        if isinstance(card, dict):
            payload["embeds"] = [card]
    try:
        discord("POST", f"/channels/{channel}/messages", payload)
    except Exception as error:  # noqa: BLE001 -- a feed line must never break the record
        delivery_failed("recruiting", application_id, error, application_id)


# --- a mail, shown as the owner would read it ------------------------------------

MAIL_WORDS_LIMIT = 1800
MAIL_ADVICE = {
    "oa": "Open the assessment from the mail; the deadline is theirs, not mine.",
    "interview": "Reply to the recruiter yourself; nothing is scheduled for you.",
    "offer": "Read the offer in the mail; nothing is accepted for you.",
}
MAIL_COLORS = {"rejection": "problem", "offer": "applied", "acknowledgement": "preparing"}
MAIL_FOOTERS = {
    "qwen": "Read by Qwen · the mail is data, not instructions",
    "qwen_failed": "Qwen could not read it · filed from the sender alone",
}


def inert(text) -> str:
    """Somebody else's words as plain Discord text: nothing in them formats, hides a link
    behind other words, or mentions anyone. A bare link stays what it is."""
    parts = re.split(r"(https?://[^\s<>\[\]()]+)", str(text if text is not None else ""))
    return "".join(
        part if n % 2 else re.sub(r"([\\*_~`|\[\]<>#@])", r"\\\1", part)
        for n, part in enumerate(parts)
    )


def mail_copy(data: dict) -> dict:
    """The private copy kept of a recorded mail (its sender, its words with any code
    removed), or {} when there is none."""
    name = re.sub(r"[^0-9A-Za-z_-]", "_", str(data.get("message_id") or ""))[:64]
    if not name:
        return {}
    try:
        copy = json.loads((state_root() / "mail/messages" / name / "message.json").read_text())
    except (OSError, ValueError):
        return {}
    return copy if isinstance(copy, dict) else {}


def mail_words(copy: dict) -> str:
    """The mail's own words, one paragraph per block, as inert text."""
    text = copy.get("shown") if "shown" in copy else copy.get("text")  # what he would see
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    return clip(inert("\n\n".join(line for line in lines if line)), MAIL_WORDS_LIMIT)


def mail_card(data: dict):
    """A recorded mail as one card: what Rove made of it as the headline, then the mail
    as the owner would read it in his inbox: who sent it, when, its subject and its own
    words. The words come from the private copy, where codes are already removed; they
    are shown, never acted on."""
    label = str(data.get("label") or "other")
    copy = mail_copy(data)
    sender = clip(copy.get("from") or data.get("sender_domain") or "unknown sender", 100)
    named = " ".join(str(copy.get("sender_name") or "").split())
    subject = clip(data.get("subject") or "(no subject)", 200)
    words = mail_words(copy)
    if label == "other" and not words:
        return f"→ Mail from {sender} · “{subject}”"
    fields = []
    if data.get("deadline"):
        fields.append(("Deadline, as the mail states it", clip(data["deadline"], 120), True))
    if data.get("interview_time"):
        fields.append(("Interview time, from the invite", clip(data["interview_time"], 80), True))
    if data.get("reconciled"):
        fields.append(("Submission", "The unclear submission went through.", False))
    if MAIL_ADVICE.get(label):
        fields.append(("What to do", MAIL_ADVICE[label], False))
    read_by = MAIL_FOOTERS.get(
        str(data.get("classifier") or "rule"),
        "Matched by rule · the mail is data, not instructions",
    )
    headline = MAIL_LABEL_WORDS.get(label, "Recruiting mail")
    card = embed(
        headline if label == "other" else f"{headline} · mail",
        f"**{inert(subject)}**" + (f"\n\n{words}" if words else ""),
        color=MAIL_COLORS.get(label, "needs"),
        fields=fields,
        footer=read_by,
    )
    card["author"] = {"name": clip(f"{named} · {sender}" if named else sender, 256)}
    with contextlib.suppress(ValueError):
        card["timestamp"] = datetime.fromisoformat(str(data.get("received_at"))).isoformat()
    return card


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
    # Recruiting lifecycle after submission (see POST_APPLICATION).
    "OA": "needs",
    "INTERVIEW": "needs",
    "OFFER": "applied",
    "REJECTED": "problem",
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
    if "owner" in lowered:
        return "your reply"
    if lowered == "policy.decline_self_identification":
        return "declined, as allowed"
    if lowered.startswith("policy."):
        return "your policy"
    if lowered.startswith("default."):
        return "default"
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


# Where a form's values came from, as the owner would say it, in the order he reads them.
FILLED_FROM = (
    "your profile",
    "your resume",
    "your replies",
    "your earlier answers",
    "your standing defaults",
    "Qwen's drafts",
)


def filled_from(filled) -> str:
    """One true sentence for the "Form filled" card: which kinds of source the values had."""
    seen = set()
    for field in filled or []:
        source = str(field.get("source") or "").lower()
        words = source_words(source)
        if "sha256" in field or words == "your resume":
            seen.add("your resume")
        elif words == "your profile":
            seen.add("your profile")
        elif words == "your reply":
            seen.add("your replies")
        elif source.startswith("your earlier answer"):
            seen.add("your earlier answers")
        elif source.startswith(("policy.", "default.")):
            seen.add("your standing defaults")
        elif "draft" in source:
            seen.add("Qwen's drafts")
    named = [name for name in FILLED_FROM if name in seen]
    if not named or set(named) <= {"your profile", "your resume"}:
        return "All from approved facts."
    return "From " + (", ".join(named[:-1]) + " and " if named[:-1] else "") + named[-1] + "."


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
    # Recruiting lifecycle after submission (see POST_APPLICATION).
    "OA": "Online assessment",
    "INTERVIEW": "Interview",
    "OFFER": "Offer",
    "REJECTED": "Rejected",
}


def question_fingerprint(label, options=(), *, kind: str = "", employer: str = "") -> str:
    """The key an answer is remembered under: what the question means, which way round it
    is asked, and its scope. The options take no part, and neither does the wording of a
    question code knows: "Gender" and "What is your gender?" share a key, "authorized to
    work in Canada" never shares the US one. A question code does not know is keyed by
    its wording. Without `employer`, a question whose answer depends on the employer gets
    the key of the owner's general answer. Empty when the question cannot be remembered.
    """
    from . import questions

    return questions.memory_key(questions.classify(label, kind, options), employer) or ""


MEMORY_COLUMNS = ("canonical_id", "polarity", "scope", "sensitivity", "origin")


def migrate_answer_memory(conn):
    """Add the canonical-identity columns and re-key rows stored under the old wording
    fingerprint. Every remembered answer is kept; the two old rows of one answer (with
    and without options) become one row under the question's canonical key."""
    import sqlite3

    from . import questions

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(answer_memory)")}
    for column in MEMORY_COLUMNS:
        if column not in columns:
            with contextlib.suppress(sqlite3.OperationalError):  # another process added it
                conn.execute(f"ALTER TABLE answer_memory ADD COLUMN {column} TEXT")
    legacy = conn.execute(
        "SELECT * FROM answer_memory WHERE canonical_id IS NULL ORDER BY created_at"
    ).fetchall()
    for row in legacy:
        try:
            options_list = json.loads(row["options"])
            if not isinstance(row["value"], str) or not isinstance(options_list, list):
                raise TypeError("value or options of the wrong kind")
            question = questions.classify(row["label"], "", options_list)
        except (TypeError, ValueError, AttributeError) as error:
            # One unreadable row is skipped and said once; it never stops a pass.
            skipped_memory(row["fingerprint"], error)
            continue
        # Old answers were never tied to an employer: they stay the owner's general answer.
        key = questions.memory_key(question) or row["fingerprint"]
        options = row["options"]
        twin = conn.execute(
            "SELECT options,value FROM answer_memory WHERE fingerprint=? AND canonical_id "
            "IS NOT NULL",
            (key,),
        ).fetchone()
        if twin and twin["value"] == row["value"] and options == "[]":
            options = twin["options"]
        conn.execute("DELETE FROM answer_memory WHERE fingerprint=?", (row["fingerprint"],))
        conn.execute(
            "INSERT OR REPLACE INTO answer_memory(fingerprint,label,options,value,created_at,"
            "owner_message_id,canonical_id,polarity,scope,sensitivity,origin) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                row["label"],
                options,
                row["value"],
                row["created_at"],
                row["owner_message_id"],
                question.canonical_id,
                question.polarity,
                _memory_scope(question, ""),
                question.sensitivity,
                "owner",
            ),
        )


_skipped_memory: set = set()


def skipped_memory(fingerprint, error: Exception):
    """One system-log line per unreadable remembered answer and process."""
    key = str(fingerprint)
    if key in _skipped_memory:
        return
    _skipped_memory.add(key)
    system_line(
        "memory",
        f"remembered answer skipped · row {clip(key, 40)} · unreadable "
        f"({type(error).__name__}) · fix or remove it in the memory channel",
    )


def _memory_scope(question, employer: str) -> str:
    from . import questions

    if question.scope != "employer":
        return question.scope
    return f"employer:{employer or questions.ANY_EMPLOYER}"


def policy_profile() -> dict:
    """The approved profile for its answer policies; empty when none can be read."""
    try:
        return read_approved()["profile"]
    except (ValueError, OSError, KeyError):
        return {}


def remember_answer(
    label,
    options,
    value,
    owner_message_id: str,
    *,
    kind: str = "",
    employer: str = "",
    origin: str = "owner",
) -> bool:
    """An answer the owner gave once is a fact for every later form that asks the same.

    It is stored under the question's canonical id, polarity and scope, marked with where
    it came from (`owner`, or `approved_draft` for a draft he approved). `kind` is the
    field's kind; a `checkbox_group` answer may name several options. `employer` is
    `questions.employer_key(url)` for an answer given in an application; without it the
    answer to an employer-specific question is the owner's general one.

    Returns whether the answer is kept. Not kept: `skip`, a question without a label, a
    class the profile keeps as ask-each-time (export control), an employer-specific
    question when the employer cannot be told, and a sensitive answer that is not one of
    the options the form offered. An unchanged answer is left as it was first given.
    """
    from . import form_reading, questions

    text = str(value or "").strip()
    if not text or text.lower() == "skip":
        return False
    choices = [str(o) for o in options or []]
    question = questions.classify(label, kind, choices)
    key = questions.memory_key(question, employer)
    if key is None:
        return False
    if question.topic in questions.POLICY_KEYS and questions.ask_each_time(
        question, policy_profile()
    ):
        return False
    sensitive = question.sensitivity == questions.SENSITIVE
    if choices and kind == "checkbox_group":
        ticked = questions.match_many(choices, text)
        if ticked is None and sensitive:
            return False
        if ticked is not None:
            # A lone box ticked by "yes" is remembered as that yes, whatever the box says.
            named = form_reading.match_options(text, choices)
            text = ", ".join(named) if named else "Yes"
    elif choices:
        offered = questions.match_option(choices, text)
        if offered is None and sensitive:
            return False
        text = offered or text
    with db() as conn:
        migrate_answer_memory(conn)
        kept = conn.execute(
            "SELECT value,options FROM answer_memory WHERE fingerprint=?", (key,)
        ).fetchone()
        if kept and kept["value"] == text:
            if kept["options"] == "[]" and choices:
                conn.execute(
                    "UPDATE answer_memory SET options=? WHERE fingerprint=?",
                    (json.dumps(choices[:60]), key),
                )
            return True
        conn.execute(
            "INSERT OR REPLACE INTO answer_memory(fingerprint,label,options,value,created_at,"
            "owner_message_id,canonical_id,polarity,scope,sensitivity,origin) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                str(label)[:300],
                json.dumps(choices[:60]),
                text,
                now(),
                owner_message_id,
                question.canonical_id,
                question.polarity,
                _memory_scope(question, employer),
                question.sensitivity,
                origin,
            ),
        )
        general = questions.memory_key(question) if employer else None
        if (
            general
            and general != key
            and questions.general_no(question, text)
            and not conn.execute(
                "SELECT 1 FROM answer_memory WHERE fingerprint=?", (general,)
            ).fetchone()
        ):
            # His "no" about one employer is his answer for every employer, until he
            # says otherwise for one: the same question is not asked at the next company.
            from .common_questions import BY_ID

            worded = BY_ID[question.canonical_id].label if question.canonical_id in BY_ID else label
            conn.execute(
                "INSERT INTO answer_memory(fingerprint,label,options,value,created_at,"
                "owner_message_id,canonical_id,polarity,scope,sensitivity,origin) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    general,
                    str(worded)[:300],
                    json.dumps(["Yes", "No"]),
                    "No",
                    now(),
                    owner_message_id,
                    question.canonical_id,
                    question.polarity,
                    _memory_scope(question, ""),
                    question.sensitivity,
                    origin,
                ),
            )
    try:
        from . import vault

        vault.sync_answers()
    except Exception as error:  # noqa: BLE001 -- the readable copy never blocks the fact
        delivery_failed("vault", "answers", error)
    return True


# What Rove's own record says about an employer, for the general answers that it can
# make untrue: an application Rove sent there, an interview that followed one.
HISTORY_STATES = {
    "previously_applied_here": (*POST_APPLICATION, "ACCEPTED", "WITHDRAWN"),
    "previously_interviewed_here": ("INTERVIEW", "OFFER", "ACCEPTED"),
}


def contradicted_by_history(canonical_id: str, employer: str) -> bool:
    """Whether an application Rove sent to this employer makes the owner's general answer
    to "applied here before?" or "interviewed here before?" untrue."""
    from . import questions

    states = HISTORY_STATES.get(canonical_id)
    if not states or not employer or employer in {questions.NO_EMPLOYER, questions.ANY_EMPLOYER}:
        return False
    with db() as conn:
        rows = conn.execute("SELECT url,status FROM application_queue").fetchall()
    return any(
        row["status"] in states and questions.employer_key(row["url"]) == employer for row in rows
    )


def recall_answer(
    label, options=(), *, kind: str = "", employer: str = "", profile: dict | None = None
) -> str | None:
    """The owner's earlier answer to this canonical question, only when it still fits.

    With options offered, the remembered value must be one of them; for a plain question
    a remembered yes or no also fits the single option that starts with it, and for a
    `checkbox_group` every part must name an option. A negated form or another country
    never matches. An employer-specific question gets this employer's answer, else the
    general one the owner gave outside any application.
    """
    from . import questions

    choices = [str(o) for o in options or []]
    question = questions.classify(label, kind, choices)
    keys = questions.memory_keys(question, employer)
    if not keys:
        return None
    if question.topic in questions.POLICY_KEYS and questions.ask_each_time(
        question, policy_profile() if profile is None else profile
    ):
        return None
    with db() as conn:
        migrate_answer_memory(conn)
        rows = [
            (
                key,
                conn.execute(
                    "SELECT value FROM answer_memory WHERE fingerprint=?", (key,)
                ).fetchone(),
            )
            for key in keys
        ]
    for position, (key, row) in enumerate(rows):
        if row is None:
            continue
        if position and contradicted_by_history(question.canonical_id, employer):
            continue  # his general "no" is not true here: Rove's own record says otherwise
        value = row["value"]
        if not isinstance(value, str) or not value.strip():
            skipped_memory(key, TypeError("empty or not text"))
            continue
        try:
            if not choices:
                return value
            if kind == "checkbox_group":
                ticked = questions.match_many(choices, value)
                if ticked is not None:
                    return ", ".join(ticked)
                continue
            fitting = questions.match_option(
                choices, value, loose=question.sensitivity == questions.PLAIN
            )
        except (TypeError, ValueError, AttributeError) as error:
            skipped_memory(key, error)
            continue
        if fitting is not None:
            return fitting
    return None


def forget_answer(
    label=None, options=(), *, kind: str = "", employer: str = "", key: str = ""
) -> bool:
    """Drop one remembered answer: the one for this canonical question, or the row named
    by `key` (as `remembered_answers` lists it). True when there was one."""
    key = key or question_fingerprint(label, options, kind=kind, employer=employer)
    with db() as conn:
        migrate_answer_memory(conn)
        removed = conn.execute("DELETE FROM answer_memory WHERE fingerprint=?", (key,)).rowcount
    if removed:
        try:
            from . import vault

            vault.sync_answers()
        except Exception as error:  # noqa: BLE001 -- the readable copy never blocks the change
            delivery_failed("vault", "answers", error)
    return bool(removed)


def remembered_answers() -> list[dict]:
    """Every remembered answer, oldest first: `label`, `options` (JSON text), `value`,
    `created_at`, the canonical identity it is kept under (`canonical_id`, `polarity`,
    `scope`, `sensitivity`, `origin`), its row `key`, and `sensitive` as a boolean."""
    with db() as conn:
        migrate_answer_memory(conn)
        rows = conn.execute(
            "SELECT label,options,value,created_at,canonical_id,polarity,scope,sensitivity,"
            "origin,fingerprint AS key FROM answer_memory ORDER BY created_at"
        ).fetchall()
    return [{**dict(row), "sensitive": row["sensitivity"] == "sensitive"} for row in rows]


def display_title(item: dict) -> str:
    title = (item.get("title") or "").strip()
    if title.lower().startswith("job application for "):
        title = title[len("job application for ") :]
    return title or "Application"


# Older records and the submission module still name commands with ids; the owner
# reads the word that means the same thing inside the application's thread.
LEGACY_COMMANDS = [
    (r"(resume|proceed) [a-f0-9]{12}", "go"),
    (r"defer [a-f0-9]{12}", "park it"),
    (r"account [a-f0-9]{12} create", "create account"),
    (r"submit [a-f0-9]{12} [a-f0-9]{8,64}", "send it"),
    (r"reconcile [a-f0-9]{12} applied", "applied"),
    (r"reconcile [a-f0-9]{12} not-submitted", "not sent"),
]


def command_words(command, questions=()) -> str:
    """A reply in the owner's words; an id form becomes its word, or nothing when the
    word needs context the card does not have (a draft's own card carries its reply)."""
    text = " ".join(str(command or "").split())
    for pattern, words in LEGACY_COMMANDS:
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return words
    match = re.fullmatch(
        r"answer [a-f0-9]{12} ([a-f0-9]{12})\s*=\s*(.*)", text, re.IGNORECASE | re.DOTALL
    )
    if match:
        keys = [str(q.get("key", "")).lower() for q in questions]
        if match[1].lower() in keys:
            number = keys.index(match[1].lower()) + 1
            return f"{number}: {match[2]}" if match[2] else f"{number}: "
        return ""
    if re.fullmatch(r"use [a-f0-9]{12} [a-f0-9]{12} [a-f0-9]{8,64}", text, re.IGNORECASE):
        return ""
    return str(command)


def reply_lines(payload: dict, limit: int = 6) -> list[str]:
    lines: list[str] = []
    for command in payload.get("commands") or []:
        words = command_words(command, payload.get("questions") or [])
        if words and words not in lines:
            lines.append(words)
    return lines[:limit]


def command_block(commands) -> str:
    return "```\n" + "\n".join(commands) + "\n```"


def numbered(questions) -> list[tuple[int, dict]]:
    """(number, question) pairs; a number is the question's place in the hold's list."""
    return list(enumerate(list(questions or []), start=1))


def question_lines(pairs, limit: int = 10) -> str:
    """Open questions with their numbers and options; the number is what the owner replies.

    A question whose text was not found on the form is said to be unreadable, with a
    pointer to the screenshot; an input's name or key is never shown in its place.
    """
    from . import form_reading

    pairs = list(pairs)
    lines = []
    for number, question in pairs[:limit]:
        if form_reading.unreadable(question):
            label = form_reading.owner_line(question)
        else:
            label = clip(question["label"], 120)
        options = question.get("options") or []
        line = f"{number}. {label}"
        if question.get("control_issue"):
            line += " — " + control_issue_words(question)
        if options:
            line += "  (" + clip(" / ".join(str(o) for o in options[:6]), 100) + ")"
        lines.append(line)
    if len(pairs) > limit:
        lines.append(f"…and {len(pairs) - limit} more")
    return "\n".join(lines)


def control_issue_words(question: dict) -> str:
    words = "known answer; the browser could not select it"
    reason = question.get("reason")
    if reason and reason != "Needs reviewed answer or supported control adapter":
        words += ": " + str(reason)
    return clip(words, 240)


def draft_lines(pairs, limit: int = 10) -> str:
    """Questions Qwen drafted: how to approve the draft, or how to replace a used one."""
    pairs = list(pairs)
    lines = []
    for number, question in pairs[:limit]:
        label = clip(question.get("label") or "Question", 100)
        if question.get("state") == "used":
            lines.append(
                f"{number}. {label} · Qwen's draft is the answer · reply `{number}: your text` "
                "to change it"
            )
        elif question.get("draft"):
            lines.append(f"{number}. {label} · reply `use draft {question['draft']}` to approve")
        else:
            lines.append(f"{number}. {label} · see its draft card above")
    if len(pairs) > limit:
        lines.append(f"…and {len(pairs) - limit} more")
    return "\n".join(lines)


def event_embeds(application_id: str, kind: str, data: dict) -> list:
    """Glanceable forum entries: one card per event, values in fields, commands in code.

    Items are cards (dicts), quiet lines (strings) and, for a file, one
    `{"attachment": path, "line": text}`; `delivery` packs them into messages.
    """
    if kind == "forum_creation_attempt":
        return []
    if kind == "attachment":
        return [{"attachment": data.get("path", ""), "line": data.get("line", "")}]
    if kind == "fields_prepared":
        cards = []
        filled = data.get("filled", [])
        for start in range(0, len(filled), 24):
            fields = []
            for field in filled[start : start + 24]:
                if "sha256" in field:
                    value = f"resume PDF · _{source_words(field.get('source'))}_"
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
                    filled_from(filled) if start == 0 else "",
                    color="preparing",
                    fields=fields,
                )
            )
        # An optional question nobody answers is left blank, and its own line says so.
        pending = [item for item in data.get("pending", []) if item.get("required", True)]
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
        number = draft_number(application_id, data.get("key", ""), data.get("proposal_hash", ""))
        fields = []
        if number:
            fields.append(
                ("Reply to approve this exact text", command_block([f"use draft {number}"]), False)
            )
        title = f"Draft {number} · " if number else "Draft · "
        return [
            embed(
                title + clip(data.get("label") or "Question", 150),
                clip(data.get("value", ""), 1500),
                color="qwen",
                fields=fields,
                footer=clip(footer, 300),
            )
        ]
    if kind == "qwen_question":
        line = f"→ Only you can answer “{clip(data.get('label') or 'this question', 90)}”"
        if data.get("explanation"):
            line += " · " + brief(data["explanation"], 160)
        return [line]
    if kind == "lifecycle":
        to = str(data.get("to", ""))
        state = STATE_WORDS.get(to, to.replace("_", " ").title())
        line = f"→ {state} · {clip(data.get('trigger', ''), 160)}"
        # A reconciliation names the owner's message id; the thread does not need it.
        detail = re.sub(r"owner message \S+;?\s*", "", str(data.get("detail") or "")).strip()
        if detail:
            line += f" ({clip(detail, 160)})"
        return [line]
    if kind == "submission_confirmed":
        checks = data.get("checks", {})
        fields = []
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
        fields = []
        if checks:
            fields.append(("What the page showed", check_lines(checks), False))
        fields.append(("Reply", command_block(["applied", "not sent"]), False))
        return [
            embed(
                "Submission unclear · do not click again",
                clip(data.get("reason", ""), 600),
                color="problem",
                fields=fields,
            )
        ]
    if kind == "submission_rejected":
        return [
            embed(
                "Form rejected · nothing sent",
                brief(data.get("reason", ""), 300),
                color="problem",
            )
        ]
    if kind == "submit_attempt":
        return ["→ Sending it once"]
    if kind == "needs_action":
        # The status card at the top of the thread shows the stop, its reason and its
        # replies; the record gets one line, and the questions when there are some.
        entries: list = [stop_line(data)]
        card = question_card(data)
        if card:
            entries.append(card)
        return entries
    if kind == "shortlisted":
        fields = []
        if data.get("items"):
            fields.append(("Why", "\n".join("• " + clip(i, 160) for i in data["items"][:8]), False))
        if reply_lines(data):
            fields.append(("Reply with", command_block(reply_lines(data)), False))
        return [embed("Your call", clip(data.get("reason", ""), 800), color="needs", fields=fields)]
    if kind == "opened":
        return [f"→ Opened · {clip(data.get('title', ''), 160)}"]
    if kind == "application_link":
        return [f"→ Clicked {clip(data.get('clicked', ''), 60)}"]
    if kind == "resume_prepared":
        line = "→ Resume ready · " + (
            "tailored from your evidence" if data.get("tailored") else "your approved base PDF"
        )
        if data.get("warning"):
            line += f" · {clip(data['warning'], 160)}"
        return [line]
    if kind == "resume_preparation_started":
        return ["→ Preparing the resume from your approved evidence"]
    if kind == "browser_retry":
        return ["→ Retried once through the site's front door"]
    if kind == "overlay":
        return [f"→ {clip(data.get('line', 'Closed a pop-up'), 200)}"]
    if kind == "company_research":
        return [research_line(data)]
    if kind == "qwen_failure":
        # The exception and its message are in the system log; the stop, if any, follows.
        from .recovery import doing

        return [f"→ Qwen did not finish {doing(str(data.get('phase') or ''))}"]
    if kind in {"model_unavailable", "discord_tag_failed"}:
        return []  # a wait or a cosmetic miss: the system log has it, the thread does not
    if kind == "mailed_code":
        return ["→ Entered the code the site mailed to your address"]
    if kind == "captcha_attempt":
        return [
            "→ Tried the picture check locally · "
            + ("cleared" if data.get("outcome") == "cleared" else "needs help")
        ]
    if kind == "captcha_cleared":
        return ["→ Picture check cleared · carrying on"]
    if kind == "owner_answer":
        label = clip(data.get("label") or "the question", 90)
        if data.get("proposal_hash"):
            return [f"→ You approved Qwen's draft for “{label}”"]
        kept = " · kept for every company unless you tell me otherwise for one"
        return [
            f"→ You answered “{label}”: {clip(data.get('value', ''), 300)}"
            + (kept if data.get("every_company") else "")
        ]
    if kind.endswith("_requested"):
        word = data.get("word") or kind[: -len("_requested")]
        if not data.get("word") and kind == "reconcile_requested":
            word = f"reconcile {data.get('outcome', '')}".strip()
        line = f"→ You replied `{clip(word, 40)}`"
        if kind == "submit_requested":
            line += " · sending it once"
        return [line]
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
        line = f"→ Used Qwen's draft for “{clip(data.get('label', ''), 80)}”"
        if data.get("number"):
            line += (
                f" · reply `{data['number']}: your text` in this thread before it is sent "
                "to change it"
            )
        return [line]
    if kind == "auto_submit_queued":
        return ["→ Auto-submit is on · sending it once"]
    if kind == "optional_skipped":
        return ["→ Left blank · optional: " + clip(", ".join(data.get("labels", [])), 300)]
    if kind == "posting_closed":
        return [f"→ {data.get('source', 'The feed')} reports the posting closed"]
    if kind == "redirect_blocked":
        where = clip(data.get("destination", "an address off the public web"), 120)
        if data.get("after_send"):
            # After the click nothing may claim that nothing was sent.
            return [
                embed(
                    "Redirect blocked after the send · tab closed",
                    f"After the send, the page went to {where}, which is not a public HTTPS "
                    "site. The tab was closed without reading it, so whether the application "
                    "went through is unclear.",
                    color="problem",
                )
            ]
        return [
            embed(
                "Redirect blocked · tab closed",
                f"The page sent the recruiting browser to {where}, which is not a public "
                "HTTPS site. The tab was closed and nothing on it was read, typed or sent.",
                color="problem",
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
    if kind == "recruiting_mail":
        return [mail_card(data)]
    fields = [
        (str(k).replace("_", " "), clip(v, 400), False)
        for k, v in list(data.items())[:10]
        if not looks_like_identifier(k, v)
    ]
    return [embed(kind.replace("_", " ").capitalize(), "", color="info", fields=fields)]


# The adapters' confirmation checks, in the owner's words.
CHECK_WORDS = {
    "post_accepted": "the site accepted a request",
    "post_rejected": "the site refused a request",
    "posts_answered": "every request was answered",
    "post_status": "the form's own request was answered",
    "url_changed": "the page address changed",
    "confirmation_url": "the address looks like a confirmation",
    "no_failure_url": "the address carries no failure word",
    "confirmation_text": "the page says the application was received",
    "confirmation_region": "a status message says it was received",
    "confirmation_content": "the board's confirmation is shown",
    "form_gone": "the form is gone",
    "no_form_error": "no new validation message",
    "no_failure_text": "no failure message",
    "captcha_rejected": "the human check refused the send",
    "rejected_form": "the form was refused",
    "no_pending_step": "no step left to finish (an email to verify, a sign-in)",
    "posts_kept": "a request left the page and was not refused",
    "all_posts_answered": "every request, to any site, was answered",
}


def check_lines(checks: dict) -> str:
    """One line per check: a tick or a cross and a plain phrase, never a key."""
    lines = []
    for key, value in checks.items():
        if key == "confirmed" or isinstance(value, str) or value is None:
            continue  # the verdict and any excerpt are already in the card's text
        words = CHECK_WORDS.get(key, str(key).replace("_", " "))
        if isinstance(value, bool):
            lines.append(f"{'✅' if value else '❌'} {words}")
        else:
            lines.append(f"· {words}: {clip(value, 40)}")
    return "\n".join(lines) or "—"


def looks_like_identifier(key, value) -> bool:
    """Ids, keys and hashes are for the system log; a generic card leaves them out."""
    name = str(key).lower()
    if name in {"key", "field_key", "sha256"} or name.endswith(("_id", "_hash", "_key")):
        return True
    return bool(re.fullmatch(r"[a-f0-9]{12,64}", str(value)))


# ---------------------------------------------------------------------------
# Company research the owner can see: one quiet thread line per outcome, saying which
# pages were read. The page text and the links stay in the private research record.
# ---------------------------------------------------------------------------
RESEARCH_PAGES = 5


def research_page_name(url: str) -> str:
    """A page as a word or two from its path: `/about-us` is "about us", `/` is "home"."""
    from urllib.parse import urlsplit

    try:
        path = urlsplit(str(url)).path
    except ValueError:
        return "page"
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "home"
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", segments[-1].lower())
    return clip(" ".join(re.findall(r"[a-z0-9]+", name)), 30) or "page"


def research_outcome(found: dict) -> dict:
    """What a research record amounts to, without its text or its links."""
    site = clip("".join(re.findall(r"[a-z0-9.-]+", str(found.get("site") or "").lower())), 80)
    listed = found.get("urls") if isinstance(found.get("urls"), list) else []
    urls = [u for u in listed if isinstance(u, str)][:RESEARCH_PAGES]
    pages = list(dict.fromkeys(research_page_name(url) for url in urls))
    if found.get("text"):
        outcome = "read"
    elif str(found.get("note") or "").startswith("fetch failed"):
        outcome = "unreachable"
    elif not site:
        outcome = "no_site"
    else:
        outcome = "nothing_useful"
    return {"outcome": outcome, "site": site, "pages": pages if outcome == "read" else []}


def research_line(data: dict) -> str:
    site = clip(data.get("site") or "", 80)
    pages = [clip(page, 30) for page in data.get("pages") or []][:RESEARCH_PAGES]
    outcome = data.get("outcome")
    if outcome == "read" and pages:
        count = len(pages)
        where = f" on {site}" if site else ""
        return (
            f"→ Looked up the company before drafting · read {count} "
            f"page{'s' if count != 1 else ''}{where} ({', '.join(pages)})"
        )
    if outcome == "no_site":
        return "→ No company site to look up from this posting · drafting from the posting only"
    if outcome == "nothing_useful":
        return (
            f"→ Looked at {site or 'the company site'} and found nothing to use · "
            "drafting from the posting only"
        )
    return "→ Could not reach the company site · drafting from the posting only"


def record_research(application_id: str):
    """Leave the thread line for the research done before drafting, once per outcome.

    Reads the private record `research.company_context` wrote for the application. A
    preparation that reuses the cached research, or fails the same way again, adds
    nothing. The technical detail is already in the system log.
    """
    path = state_root() / f"applications/{application_id}/research.json"
    try:
        found = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(found, dict):
        return
    data = research_outcome(found)
    with db() as conn:
        last = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? "
            "AND kind='company_research' ORDER BY id DESC LIMIT 1",
            (application_id,),
        ).fetchone()
    if last and json.loads(last["data"]) == data:
        return
    record(application_id, "company_research", data)


def draft_number(application_id: str, key: str, proposal_hash: str = "") -> int | None:
    """Which `use draft N` names this draft: the Nth proposal in the proposals file."""
    path = state_root() / f"applications/{application_id}/answer-proposals.json"
    if not path.exists():
        return None
    number = 0
    for answer in json.loads(path.read_text()).get("answers", []):
        if answer.get("kind") != "proposal":
            continue
        number += 1
        if answer.get("key") == key and (
            not proposal_hash or answer.get("proposal_hash") == proposal_hash
        ):
            return number
    return None


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
    try:
        thread = create_forum_post(application_id, settings, item, title)
    except httpx.TransportError as error:
        if not isinstance(error, httpx.ReadTimeout):
            # Nothing reached Discord: the attempt marker must not block the next tick.
            with db() as conn:
                conn.execute(
                    "DELETE FROM application_events WHERE application_id=? AND kind='forum_creation_attempt'",
                    (application_id,),
                )
        raise
    with db() as conn:
        conn.execute(
            "UPDATE application_queue SET thread_id=?,updated_at=? WHERE id=?",
            (thread["id"], now(), application_id),
        )
    if item["status"] not in WAITING and item["status"] != "QUEUED":
        # A queued application is about to be prepared; the post already says so, and the
        # preparation's own state change edits the card. One edit fewer on the way in.
        refresh_status(application_id)
    system_line(application_id, f"opened · {item['url']} · {forum_url(application_id)}")
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


def create_forum_post(application_id: str, settings: dict, item: dict, title: str) -> dict:
    return discord(
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
                        "Preparing in the recruiting browser. "
                        + (
                            "It is sent once it is complete; this thread is the record."
                            if settings.get("auto_submit")
                            else "Nothing is sent until you reply `send it` here."
                        ),
                        color="preparing",
                        url=item["url"],
                        fields=[
                            ("Source", item["source"].replace("_", " "), True),
                            ("Posting", clip(item["source_url"], 300), False),
                        ],
                    )
                ],
                "allowed_mentions": {"parse": []},
            },
        },
    )


def flush_events(application_id: str):
    """Deliver the application's waiting thread entries, packed into as few messages as
    Discord's limits allow (see `delivery`).

    While the worker's delivery window is open (an application being prepared and sent),
    entries are only kept, unless one of them is a hold card: then everything waiting is
    delivered at once, in order. The window's end delivers the rest. Never raises for a
    Discord failure: the entries stay waiting for the next pass.
    """
    from . import delivery

    if delivery.window_open() and not delivery.urgent_pending(application_id):
        return
    thread = ensure_forum(application_id)
    if not thread:
        return
    delivery.deliver_events(application_id, thread)


def delivery_deferred() -> bool:
    """Whether thread entries and system lines wait for the end of the tick right now."""
    from . import delivery

    return delivery.window_open()


@contextlib.contextmanager
def delivery_window():
    """Keep the record off the critical path: while open, thread entries, files and
    system-log lines are written but not posted; the status card, owner cards and hold
    cards still go at once. Closing delivers everything that waited, in order."""
    from . import delivery

    delivery.open_window()
    try:
        yield
    finally:
        delivery.close_windows()
        try:
            flush_pending()
        except Exception as error:  # noqa: BLE001 -- the record waits; the tick's result stands
            delivery_failed("window", "flush", error, announce=False)


ATTACHMENT_LIMIT = 8 * 1024 * 1024


def attach_file(application_id: str, path, line: str) -> bool:
    """A private file (screenshot, resume) in the application's thread, with one plain line.

    The file is copied aside as it is now (a later screenshot cannot replace it) and goes
    into the thread record like any entry: posted at once, or after the send while an
    application is being prepared. False when there is no forum or the file is unusable.
    """
    import shutil
    from pathlib import Path

    file_path = Path(path)
    settings = config()
    if not settings.get("enabled") or not settings.get("forum_channel_id"):
        return False
    if not file_path.is_file() or file_path.stat().st_size > ATTACHMENT_LIMIT:
        return False
    outbox = state_root() / f"applications/{application_id}/outbox"
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    copy = outbox / f"{uuid.uuid4().hex[:8]}-{file_path.name}"
    try:
        shutil.copyfile(file_path, copy)
        copy.chmod(0o600)
    except OSError as error:
        delivery_failed("attachment", application_id, error, announce=False)
        return False
    record(application_id, "attachment", {"path": str(copy), "line": clip(line, 1800)})
    try:
        flush_events(application_id)
    except (RuntimeError, httpx.HTTPError, OSError) as error:
        # The thread could not be opened; the file waits with the rest of the record.
        delivery_failed("attachment", application_id, error, announce=False)
    return True


def delivery_failed(
    kind: str, row_id, error: Exception, application_id: str = "", *, announce: bool | None = None
):
    """One private line per failed Discord delivery, so a stuck card is diagnosable.

    `announce` adds a system-log line. By default only a definite refusal (a 4xx that is
    not a rate limit) is announced; an outage is not, and callers that retry a refusal
    announce only the last one.
    """
    detail = ""
    response = getattr(error, "response", None)
    if response is not None:
        detail = f"{response.status_code} {response.text[:200]}"
    line = " ".join(f"{now()} {kind} {row_id} {type(error).__name__} {detail}".split())
    log = state_root() / "logs/delivery-failures.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(line + "\n")
    if announce is None:
        status = response.status_code if response is not None else 0
        announce = kind != "system" and 400 <= status < 500 and status != 429
    if announce and kind != "system":
        # Discord answered and refused: worth a system-log line. Unreachable Discord is not.
        system_line(
            application_id, f"delivery failed · {kind} {row_id} · {type(error).__name__} {detail}"
        )


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
    """Remove the application's cards from the owner channels; the thread keeps the record.

    A card still waiting for Discord is dropped before it is ever posted: a stop the
    owner already left behind never arrives late as a second card."""
    settings = config()
    with db() as conn:
        for name in channels or NOTICE_CHANNELS:
            conn.execute(
                "UPDATE owner_notices SET delivery='withdrawn' WHERE application_id=? "
                "AND channel=? AND delivery IN ('pending','skipped','failed')",
                (application_id, name),
            )
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
                gone = getattr(getattr(error, "response", None), "status_code", 0) == 404
                # A card someone already deleted is withdrawn; nothing to report.
                delivery_failed(
                    "withdraw", row["id"], error, application_id, announce=False if gone else None
                )
        with db() as conn:
            conn.execute("UPDATE owner_notices SET delivery='withdrawn' WHERE id=?", (row["id"],))


NUMBERED_REPLY = re.compile(r"\d+:\s*")
# Room under one field value for the question lines of a hold card.
QUESTION_FIELD = 1000


def prompt_lines(pairs) -> list[str]:
    """Each open question with the reply that answers it: "`3:` Label  (A / B)"."""
    return [
        f"`{number}:` " + question_lines([(number, question)]).split(". ", 1)[1]
        for number, question in pairs
    ]


def question_fields(pairs, budget: int | None = None) -> list[tuple]:
    """The open questions as fields of whole lines, all of them, or as many as `budget`
    characters allow with "…and N more in the thread" for the rest."""
    from .delivery import units

    lines = prompt_lines(pairs)
    shown, used = [], 0
    for index, line in enumerate(lines):
        cost = units(line) + 1
        rest = len(lines) - index
        tail = units(f"…and {rest} more in the thread") + 1
        if budget is not None and used + cost + (tail if rest > 1 else 0) > budget:
            shown.append(f"…and {rest} more in the thread")
            break
        shown.append(line)
        used += cost
    fields, chunk = [], []
    for line in shown:
        if chunk and units("\n".join([*chunk, line])) > QUESTION_FIELD:
            fields.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        fields.append(chunk)
    return [
        ("Only you can answer" if index == 0 else "\u200b", "\n".join(chunk), False)
        for index, chunk in enumerate(fields)
    ]


def hold_fields(payload: dict, budget: int | None = None) -> list:
    """Why, every open question with its `N:` reply, Qwen's drafts, and the word replies.

    With a `budget` (a card that must stay one embed) the question list is cut to fit and
    says how many more the thread lists; without one, every question is listed and the
    card is split across embeds when it has to be.
    """
    from .delivery import units

    fields = []
    if payload.get("items"):
        fields.append(("Why", "\n".join("• " + clip(i, 140) for i in payload["items"][:4]), False))
    pairs = numbered(payload.get("questions"))
    open_pairs = [(n, q) for n, q in pairs if q.get("state", "open") == "open"]
    draft_pairs = [(n, q) for n, q in pairs if q.get("state") in {"drafted", "used"}]
    replies = reply_lines(payload)
    if open_pairs:
        # Each question carries its own `N:` reply, so the block keeps only the words.
        replies = [r for r in replies if not NUMBERED_REPLY.fullmatch(r)]
    rest = []
    if draft_pairs:
        rest.append(("Qwen drafted", draft_lines(draft_pairs), False))
    if replies:
        rest.append(("Reply", command_block(replies), False))
    if open_pairs:
        room = None
        if budget is not None:
            spent = sum(units(n) + units(v) for n, v, _ in [*fields, *rest])
            room = max(budget - spent - units("Only you can answer"), 200)
        fields.extend(question_fields(open_pairs, room))
    return fields + rest


# How a stop reads after "Stopped:" when its headline alone would read oddly there.
STOP_WORDS = {
    "Preparation stopped": "a step did not finish",
    "Preparation interrupted": "the preparation was interrupted",
    "Submission unclear": "the send is unclear",
    "Submission not attempted": "the check before sending failed",
}


def stop_line(payload: dict) -> str:
    """The thread's one line for a stop: what it waits for, in a few words. The status
    card at the top holds the reason and the replies."""
    headline = str(payload.get("headline") or "Needs you")
    questions = list(payload.get("questions") or [])
    asked = sum(1 for q in questions if q.get("state", "open") == "open")
    drafted = sum(1 for q in questions if q.get("state") == "drafted")
    if asked or drafted:
        parts = []
        if asked:
            parts.append(f"needs your answer to {asked} question{'s' if asked != 1 else ''}")
        if drafted:
            parts.append(f"{drafted} draft{'s' if drafted != 1 else ''} to approve")
        return "→ Stopped: " + " and ".join(parts)
    if headline.startswith("Ready"):
        return f"→ {headline} · waiting for your reply"
    words = STOP_WORDS.get(headline) or headline[:1].lower() + headline[1:]
    return f"→ Stopped: {clip(words, 160)}"


def question_card(payload: dict) -> dict | None:
    """The questions of a stop with their `N:` replies and Qwen's drafts: the one part of a
    stop the status card cannot hold. None when the stop asks no question."""
    pairs = numbered(payload.get("questions"))
    open_pairs = [(n, q) for n, q in pairs if q.get("state", "open") == "open"]
    draft_pairs = [(n, q) for n, q in pairs if q.get("state") in {"drafted", "used"}]
    fields = list(question_fields(open_pairs)) if open_pairs else []
    if draft_pairs:
        fields.append(("Qwen drafted", draft_lines(draft_pairs), False))
    if not fields:
        return None
    return embed(
        "Your answers",
        "Reply with the number and your answer, like `1: your answer`."
        if open_pairs
        else "Each draft says the reply that approves it.",
        color="needs",
        fields=fields,
    )


def notice_embed(application_id: str, channel: str, payload: dict) -> dict:
    """The channel card carries the decision; its title links to the thread for the record.

    It is one embed, so a long question list is cut to fit and points to the thread."""
    from .delivery import EMBED_BUDGET, units

    item = get(application_id)
    headline = payload.get("headline") or ("Your call" if channel == "shortlist" else "Needs you")
    title = clip(display_title(item), 120)
    description = f"**{headline}**\n" + clip(payload.get("reason", ""), 500)
    budget = EMBED_BUDGET - units(title) - units(description) - 100
    return embed(
        title,
        description,
        color="needs",
        url=forum_url(application_id) or item["url"],
        fields=hold_fields(payload, budget=budget),
    )


def flush_notices():
    """Post waiting owner-channel cards. An outage stops the pass (the next tick resumes);
    a card Discord refuses is tried again on later passes, at most three times, then
    marked failed with one system-log line, and never holds up the cards behind it."""
    from . import delivery

    settings = config()
    if not settings.get("enabled"):
        return
    with delivery.conn() as conn:
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
            if delivery.transient(error):
                delivery_failed("notice", row["id"], error, row["application_id"])
                return
            attempts = row["attempts"] + 1
            final = attempts >= delivery.REJECTIONS
            with db() as conn:
                conn.execute(
                    "UPDATE owner_notices SET delivery=?,attempts=? WHERE id=?",
                    ("failed" if final else "pending", attempts, row["id"]),
                )
            delivery_failed("notice", row["id"], error, row["application_id"], announce=final)
            continue
        with db() as conn:
            conn.execute(
                "UPDATE owner_notices SET delivery='sent', message_id=? WHERE id=? "
                "AND delivery='pending'",
                (str(sent.get("id", "")), row["id"]),
            )
            now_row = conn.execute(
                "SELECT delivery FROM owner_notices WHERE id=?", (row["id"],)
            ).fetchone()
        if now_row and now_row["delivery"] == "withdrawn" and sent.get("id"):
            # Withdrawn while it was on its way: it leaves the channel at once.
            with contextlib.suppress(httpx.HTTPError, OSError):
                discord("DELETE", f"/channels/{channel}/messages/{sent['id']}")


STATUS_LINES = {
    "QUEUED": "Queued · waits for its turn in the recruiting browser",
    "PREPARING": "Preparing in the recruiting browser",
    "NEEDS_USER": "Needs you",
    "MANUAL_TAKEOVER": "Needs you in the recruiting browser",
    "UNKNOWN_SUBMISSION": "Submission unclear · do not click Submit again",
    "SUBMITTING": "Submitting once",
    "READY_FOR_REVIEW": "Ready to send",
    "APPLIED": "Applied ✅",
    "DEFERRED": "Parked",
    # Recruiting lifecycle after submission (see POST_APPLICATION).
    "OA": "Online assessment · the employer's mail has the link and the deadline",
    "INTERVIEW": "Interview · the employer's mail has the details",
    "OFFER": "Offer · read the employer's mail; nothing is accepted for you",
    "REJECTED": "Rejected",
}


def status_replies(payload: dict) -> list[str]:
    """Up to four replies for the status card; three or more numbered answers become one
    line, so `go` and `park it` still show."""
    replies = reply_lines(payload)
    numbers = [r for r in replies if NUMBERED_REPLY.fullmatch(r)]
    waiting = [q for q in payload.get("questions") or [] if q.get("state", "open") == "open"]
    if len(numbers) >= 3 or (numbers and len(waiting) >= 3):
        words = [r for r in replies if r not in numbers]
        count = max(len(waiting), len(numbers))
        replies = [f"N: your answer  ({count} questions)", *words]
    return replies[:4]


def refresh_status(application_id: str):
    """The thread's first post is the live status card; the forum list previews it.

    The card is edited only when what it says changed. An archived thread is reopened
    once; a closed one is left alone. A failed edit is tried again at the end of a tick.
    """
    from . import delivery

    settings = config()
    item = get(application_id)
    if not settings.get("enabled") or not item["thread_id"]:
        return
    if delivery.thread_closed(item["thread_id"]):
        return
    status = item["status"]
    payload = None
    if status in WAITING:
        # The stop the owner channel shows, delivered or not: the thread's own record
        # carries only a line for it, so this card holds its reason, why and replies.
        with db() as conn:
            row = conn.execute(
                "SELECT data FROM owner_notices WHERE application_id=? AND delivery!='withdrawn' "
                "ORDER BY id DESC LIMIT 1",
                (application_id,),
            ).fetchone()
        payload = json.loads(row["data"]) if row else None
    items: list = []
    if payload:
        headline = payload.get("headline") or "Needs you"
        line = clip(payload.get("reason", ""), 1200)
        commands = status_replies(payload)
        items = list(payload.get("items") or [])
    else:
        headline = STATUS_LINES.get(status) or STATE_WORDS.get(status, "Update")
        line = "Reply `go` to pick it up again." if status == "DEFERRED" else ""
        commands = []
    fields = []
    if items:
        fields.append(("Why", "\n".join("• " + clip(i, 140) for i in items[:4]), False))
    fields.append(("Posting", clip(item["source_url"], 200), False))
    if commands:
        fields.append(("Reply", command_block(commands), False))
    card = embed(
        clip(display_title(item), 200),
        f"**{headline}**" + (f"\n{line}" if line else ""),
        color=STATE_COLORS.get(status, "info" if status == "DEFERRED" else "preparing"),
        url=item["url"],
        fields=fields,
        footer="Live status · the thread below is the full record",
    )
    digest = delivery.card_digest(card)
    if delivery.status_unchanged(application_id, digest):
        return
    thread = item["thread_id"]
    try:
        delivery.to_thread(
            thread,
            lambda: discord(
                "PATCH",
                f"/channels/{thread}/messages/{thread}",
                {"embeds": [card], "allowed_mentions": {"parse": []}},
            ),
        )
    except delivery.Closed as closed:
        delivery.mark_closed(application_id, thread, str(closed))
        return
    except delivery.Rejected as refused:
        delivery_failed("status", application_id, refused.error, application_id)
        return
    except (httpx.HTTPError, OSError) as error:
        delivery_failed("status", application_id, error, application_id)
        delivery.remember_status(application_id, "")  # tried again at the end of the tick
        return
    delivery.remember_status(application_id, digest)


def flush_pending():
    """Deliver every waiting thread entry, owner-channel card, status card edit and
    system-log line; the worker calls this each tick, and the delivery window's end."""
    from . import delivery

    with delivery.conn() as conn:
        waiting = [
            r[0]
            for r in conn.execute(
                "SELECT application_id FROM application_events "
                "WHERE delivery IN ('pending','sending') AND kind!='forum_creation_attempt' "
                "GROUP BY application_id ORDER BY MIN(id)"
            )
        ]
    before = delivery.outages[0]
    for application_id in waiting:
        if delivery.outages[0] != before:
            return  # Discord is down: one failed call is enough for this pass
        try:
            flush_events(application_id)
        except (RuntimeError, httpx.HTTPError, OSError):
            # An uncertain forum creation waits for reconciliation; a failed one is retried later.
            continue
    flush_notices()
    for application_id in delivery.stale_status_cards():
        with contextlib.suppress(ValueError):  # an application that no longer exists
            refresh_status(application_id)
    if delivery.outages[0] == before:
        flush_system()


def action_needed(
    application_id: str,
    reason: str,
    *,
    questions=None,
    commands=None,
    headline: str | None = None,
    items=None,
    channel: str = "action",
    watch: str = "",
    in_place: bool = False,
):
    """One card in the thread and one in an owner channel: what happened, what to reply.

    `watch` names something Rove keeps an eye on to carry on by itself ("captcha": the
    picture check leaving the screen). `in_place` says the next pass reads the tab as the
    owner left it instead of loading the posting again."""
    item = get(application_id)
    payload: dict = {
        "reason": reason,
        "questions": list(questions or []),
        "commands": list(commands or []),
        "headline": headline,
        "items": list(items or []),
    }
    if watch:
        payload["watch"] = watch
    if in_place:
        payload["in_place"] = True
    record(application_id, "needs_action", payload)
    # The owner's card first: the thread may have a pass worth of entries to post. The
    # card also becomes the thread's status card; the thread itself gets one line.
    notice(application_id, channel, payload)
    try:
        flush_events(application_id)
    except (RuntimeError, httpx.HTTPError, OSError) as error:
        # The thread could not be reached; its record waits for a later pass.
        delivery_failed("event", application_id, error, application_id, announce=False)
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
    name, element_id = field.get("name"), field.get("id")
    # Oracle redraws fields as lastName-15, lastName-27, etc. The declared field name
    # still identifies the same question; a render counter must not discard answers.
    if (
        field.get("kind") not in {"radio", "checkbox", "radio_group", "checkbox_group", "choice"}
        and name
        and element_id
        and re.fullmatch(re.escape(name) + r"-\d+", element_id)
    ):
        identity["id"] = name
    if field.get("occurrence"):
        # A later twin of another question on the same form: its section and its place
        # among the twins tell it apart. A field without a twin keys as it always has.
        identity.update(section=field.get("section") or "", occurrence=field["occurrence"])
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


# Rows Rove wrote into application_answers on its own: a Qwen draft used under the
# owner's policy, or an optional field left blank. They are never the owner's words.
AUTOMATIC_ANSWERS = {"auto-draft:": "draft", "auto-skip:": "skip"}


def _answers(application_id: str) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT field_key,value,owner_message_id FROM application_answers WHERE application_id=?",
            (application_id,),
        ).fetchall()


def forget_skips(application_id: str):
    """Drop the blanks Rove itself left for this application; the owner's answers and
    the drafts it used stay."""
    with db() as conn:
        conn.execute(
            "DELETE FROM application_answers WHERE application_id=? AND owner_message_id "
            "LIKE 'auto-skip:%'",
            (application_id,),
        )


def approved_answers(application_id: str) -> dict:
    """What the owner himself answered for this application, by field key.

    Drafts Rove used and blanks it left are not here; see `automatic_answers`.
    """
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
        for r in _answers(application_id)
        if not r["owner_message_id"].startswith(tuple(AUTOMATIC_ANSWERS))
    }


def automatic_answers(application_id: str) -> dict:
    """What Rove recorded on its own for this application, by field key: `kind` is
    `draft` (a Qwen draft used under the owner's policy) or `skip` (an optional field
    left blank). The source says so in words and never names the owner."""
    from . import questions

    result = {}
    for r in _answers(application_id):
        kind = next(
            (k for p, k in AUTOMATIC_ANSWERS.items() if r["owner_message_id"].startswith(p)), None
        )
        if kind:
            result[r["field_key"]] = {
                "value": r["value"],
                "kind": kind,
                "source": questions.USED_DRAFT if kind == "draft" else "left blank",
            }
    return result


def save_result(application_id: str, result: dict):
    write_private(state_root() / f"applications/{application_id}/workflow-result.json", result)
