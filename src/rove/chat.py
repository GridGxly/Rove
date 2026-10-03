"""Plain answers for the owner's common questions in agent-control.

The Hermes agent answers in agent-control. For the handful of questions he asks most
(status, what is waiting, sends today, pause and resume, what happened with a company)
it calls one MCP tool from this module and relays the `say` line. Code reads the
queue and writes the switch; the model only passes the words on, so there is little
for it to rephrase and nothing for it to guess.

Every `say` is written for a phone: short lines, company and role names, no ids,
hashes, field keys or state names.
"""

import hashlib
import json
import os
import re
from datetime import UTC, datetime

from . import workflow
from .jobs import plain, plain_company
from .runtime import state_root

LISTED = 6  # names in one answer before "and N more"


def clip(text, limit: int) -> str:
    return workflow.clip(" ".join(str(text or "").split()), limit)


def title_of(item: dict) -> str:
    """Company and role without markup a title could carry."""
    return clip(re.sub(r"[`*_~|<>\[\]\\@]", " ", workflow.display_title(item)), 60)


def names(items: list[dict]) -> str:
    shown = [title_of(item) for item in items[:LISTED]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def channel(settings: dict, key: str, name: str) -> str:
    """A tappable channel mention when its id is configured, the plain name otherwise."""
    value = settings.get(key)
    return f"<#{value}>" if value else f"#{name}"


def set_setting(key: str, value) -> None:
    """Change one key of the private workflow config, written whole and swapped in."""
    path = state_root() / "config/workflow.json"
    stored = json.loads(path.read_text()) if path.exists() else {}
    stored[key] = value
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(stored, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


# --- what Rove counts ---------------------------------------------------------


def today() -> str:
    """The day the daily cap counts: the UTC date, as `intake.daily_cap_reached` uses."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def sends(conn) -> list[dict]:
    """Today's submission attempts, oldest first, with the application's title."""
    rows = conn.execute(
        "SELECT a.status AS attempt,q.* FROM live_submission_attempts a JOIN application_queue q "
        "ON q.id=a.application_id WHERE a.created_at LIKE ? ORDER BY a.created_at",
        (today() + "%",),
    ).fetchall()
    return [dict(row) for row in rows]


def daily_cap(settings: dict) -> int:
    from .intake import number

    return number(settings, "max_submissions_per_day", 30)


def sends_line(conn, settings: dict) -> str:
    rows = sends(conn)
    sent = [r for r in rows if r["attempt"] == "APPLIED"]
    unclear = [r for r in rows if r["attempt"] in {"SUBMITTING", "UNKNOWN_SUBMISSION"}]
    line = f"Sent today: {len(sent)}"
    if unclear:
        line += f", and {len(unclear)} I could not confirm"
    return line + f". The cap is {daily_cap(settings)} a day."


def queue_rows() -> list[dict]:
    with workflow.db() as conn:
        rows = conn.execute("SELECT * FROM application_queue ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def latest_notice(application_id: str) -> tuple[str, dict] | None:
    """The owner card that is live for an application: its channel name and payload."""
    with workflow.db() as conn:
        row = conn.execute(
            "SELECT channel,data FROM owner_notices WHERE application_id=? AND delivery='sent' "
            "ORDER BY id DESC LIMIT 1",
            (application_id,),
        ).fetchone()
    return (row["channel"], json.loads(row["data"])) if row else None


def open_digest_lines() -> int:
    """Jobs on today's daily list that still wait for a yes or no."""
    from .intake import db as intake_db

    with intake_db() as conn:
        rows = conn.execute("SELECT data FROM intake_digests WHERE delivery='sent'").fetchall()
    return sum(
        1 for row in rows for line in json.loads(row["data"])["lines"] if not line.get("answer")
    )


# --- the answers ----------------------------------------------------------------


def status() -> dict:
    """What Rove is doing, what waits on the owner, the queue, today's sends, the feed."""
    settings = workflow.config()
    if not settings.get("enabled"):
        return {"say": "The application queue is switched off, so nothing is being prepared."}
    rows = queue_rows()
    by_state: dict[str, list[dict]] = {}
    for row in rows:
        by_state.setdefault(row["status"], []).append(row)
    lines = []
    busy = by_state.get("SUBMITTING", []) + by_state.get("PREPARING", [])
    if busy:
        lines.append(f"Working on {names(busy)} now.")
    waiting = [row for row in rows if row["status"] in workflow.WAITING]
    if waiting:
        lines.append(
            f"{len(waiting)} {'need' if len(waiting) != 1 else 'needs'} you. "
            "Ask “what's waiting” to see which."
        )
    queued, parked = len(by_state.get("QUEUED", [])), len(by_state.get("DEFERRED", []))
    if queued or parked:
        lines.append(f"In the queue: {queued}. Parked: {parked}.")
    if not (busy or waiting or queued):
        lines.append("Nothing is in the queue.")
    with workflow.db() as conn:
        lines.append(sends_line(conn, settings))
    if settings.get("feed_paused") is True:
        lines.append("The feed is paused: its jobs wait, your own links still go.")
    return {"say": "\n".join(lines)}


def waiting() -> dict:
    """Every application that waits on the owner, and where its card is."""
    settings = workflow.config()
    if not settings.get("enabled"):
        return {"say": "The application queue is switched off, so nothing waits on you."}
    rows = [row for row in queue_rows() if row["status"] in workflow.WAITING]
    lines = []
    where = set()
    for row in rows[:LISTED]:
        notice = latest_notice(row["id"])
        name, payload = notice if notice else ("action", workflow.latest_hold(row["id"]) or {})
        where.add(name)
        headline = payload.get("headline") or workflow.STATE_WORDS.get(row["status"], "Needs you")
        asked = [q for q in payload.get("questions") or [] if q.get("state", "open") == "open"]
        if asked and headline in {"Needs you", "Needs your answers"}:
            headline = f"{len(asked)} question{'s' if len(asked) != 1 else ''} only you can answer"
        lines.append(f"• {title_of(row)}: {clip(headline, 60).lower()}")
    if len(rows) > LISTED:
        lines.append(f"• and {len(rows) - LISTED} more")
    digest = open_digest_lines()
    if not lines and not digest:
        return {"say": "Nothing waits on you right now."}
    places = [
        channel(settings, key, label)
        for name, key, label in (
            ("action", "action_channel_id", "action-needed"),
            ("shortlist", "shortlist_channel_id", "shortlist"),
        )
        if name in where
    ]
    say = []
    if lines:
        count = len(rows)
        say.append(f"{count} {'need' if count != 1 else 'needs'} you:")
        say.extend(lines)
        say.append("Their cards are in " + " and ".join(places) + ".")
    if digest:
        say.append(
            f"Today's list in {channel(settings, 'shortlist_channel_id', 'shortlist')} has "
            f"{digest} job{'s' if digest != 1 else ''} waiting for a yes or no."
        )
    return {"say": "\n".join(say)}


def sent_today() -> dict:
    """Today's sends by name, and the daily cap."""
    settings = workflow.config()
    with workflow.db() as conn:
        rows = sends(conn)
        line = sends_line(conn, settings)
    sent = [r for r in rows if r["attempt"] == "APPLIED"]
    say = [line]
    if sent:
        say.append(names(sent) + ".")
    if not settings.get("auto_submit"):
        say.append("I send only after you reply “send it”.")
    return {"say": "\n".join(say)}


def pause_feed() -> dict:
    set_setting("feed_paused", True)
    workflow.system_line("chat", "feed paused by the owner in agent-control")
    return {
        "say": "Paused. Jobs from the feed stay in the queue and wait. Links you paste and "
        "jobs you pick still go, and the one in progress finishes."
    }


def resume_feed() -> dict:
    set_setting("feed_paused", False)
    workflow.system_line("chat", "feed resumed by the owner in agent-control")
    return {"say": "The feed is running again. Its jobs take their turn in the queue."}


DECISION_WORDS = {
    "dropped": "skipped by your feed rules",
    "capped": "left out because the backlog was full",
    "lapsed": "was on the daily list, but it closed or aged out before you picked it",
    "sibling": "the same role as another listing I already handled",
    "digest": "waiting for your yes on the daily list",
    "offered": "on today's daily list, waiting for your yes",
    "declined": "you said no to it on the daily list",
    "queued": "queued from the feed",
    "picked": "you picked it from the daily list",
}


def application_line(row: dict) -> str:
    """One application in plain words: where it stands and, when it stopped, why."""
    state = row["status"]
    if state in workflow.WAITING:
        notice = latest_notice(row["id"])
        payload = notice[1] if notice else workflow.latest_hold(row["id"]) or {}
        headline = payload.get("headline") or workflow.STATE_WORDS.get(state, "Needs you")
        reason = clip(payload.get("reason"), 140)
        return f"{headline.lower()}" + (f" — {reason}" if reason else "")
    if state == "DEFERRED":
        reason = row.get("error") or ""
        return "parked" + (f": {clip(reason, 140)}" if " " in reason else "")
    if state in {"APPLIED", "OA", "INTERVIEW", "OFFER", "REJECTED"}:
        day = (row.get("updated_at") or "")[:10]
        return workflow.STATE_WORDS[state].lower() + (f" (last change {day})" if day else "")
    return workflow.STATE_WORDS.get(state, "tracked").lower()


def company_history(company: str) -> dict:
    """What happened with one company: its applications, and feed jobs Rove did not queue."""
    from .intake import db as intake_db
    from .mail import split_title

    wanted = plain_company(company)
    if len(wanted) < 2:
        return {"say": "Which company? Give me its name."}
    matched = []
    for row in sorted(queue_rows(), key=lambda r: r["updated_at"], reverse=True):
        name = plain_company(split_title(row)[0])
        title = plain(workflow.display_title(row))
        if wanted == name or (len(wanted) >= 3 and f" {wanted} " in f" {name} {title} "):
            matched.append(row)
    lines = [f"• {title_of(row)}: {application_line(row)}" for row in matched[:3]]
    seen = {row["id"] for row in matched}
    first = wanted.split()[0]
    with intake_db() as conn:
        decisions = conn.execute(
            "SELECT status,reason,payload,application_id FROM intake_decisions "
            "WHERE payload LIKE ? ORDER BY updated_at DESC LIMIT 200",
            (f"%{first}%",),
        ).fetchall()
    shown = 0
    for decision in decisions:
        payload = json.loads(decision["payload"])
        if plain_company(payload.get("company")) != wanted or decision["application_id"] in seen:
            continue
        if decision["status"] in {"queued", "picked"} and matched:
            continue  # the application line already tells that story
        words = DECISION_WORDS.get(decision["status"], "seen in the feed")
        reason = clip(decision["reason"], 120)
        if decision["status"] in {"dropped", "capped", "lapsed", "digest", "offered"} and reason:
            words += f" ({reason})"
        lines.append(f"• {clip(payload.get('title') or 'Role', 60)}: {words}")
        shown += 1
        if shown == 3:
            break
    if not lines:
        return {"say": f"I have no record of {clip(company, 40)}: not applied, not skipped."}
    return {"say": "\n".join(lines)}


# --- the pinned help message in agent-control ----------------------------------

HELP_TITLE = "What you can ask Rove"


def help_text(settings: dict | None = None) -> str:
    """The pinned message. Every example here works today; keep it that way."""
    settings = workflow.config() if settings is None else settings
    memory = channel(settings, "memory_channel_id", "memory")
    action = channel(settings, "action_channel_id", "action-needed")
    system = channel(settings, "system_channel_id", "system-log")
    return "\n".join(
        [
            f"**{HELP_TITLE}**",
            "Type here like you would text someone. These work:",
            "• Paste a job link — I queue it as yours",
            "• `status` — what I'm doing, the queue, sends today",
            "• `what's waiting on me` — what needs you, and where",
            "• `why did you skip Acme` — what happened with one company",
            "• `how many did you send today` — sends and the daily cap",
            "• `pause` / `resume` — hold or restart jobs from the feed",
            "• `where do I go to school` — any fact from your approved profile",
            f"• `what do you know about me` — that list lives in {memory}: type `list` there",
            (
                f"Replies like `go` or `send it` belong on a card in {action} or in the "
                f"application's thread. The step-by-step log is in {system}."
            ),
        ]
    )


def help_reply() -> dict:
    return {"say": help_text()}


def ensure_help_message() -> dict:
    """Post the help message in agent-control once, pin it, and edit it when its text changes.

    The message id and a hash of its text live in the private workflow config, so a
    second run never posts a copy, and an unchanged text costs no Discord call. When the
    text changed and the old message is gone (404), a new one is posted; any other
    Discord failure raises and leaves things as they are for the next run.
    """
    import httpx

    from .discord_feed import discord

    settings = workflow.config()
    target = settings.get("control_channel_id")
    if not settings.get("enabled") or not target:
        return {"posted": False, "reason": "agent-control is not configured"}
    content = help_text(settings)
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    body = {"content": content, "allowed_mentions": {"parse": []}}
    message_id = settings.get("control_help_message_id")
    if message_id and settings.get("control_help_hash") == digest:
        return {"posted": False, "unchanged": True}
    if message_id:
        try:
            discord("PATCH", f"/channels/{target}/messages/{message_id}", body)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            message_id = None
        else:
            set_setting("control_help_hash", digest)
            return {"posted": False, "edited": True}
    # A pinned copy from before the id was kept is adopted instead of duplicated.
    pins = discord("GET", f"/channels/{target}/messages/pins")
    for pin in (pins.get("items") if isinstance(pins, dict) else pins) or []:
        message = pin.get("message", pin) if isinstance(pin, dict) else {}
        author = message.get("author") or {}
        if author.get("bot") and str(message.get("content", "")).startswith(f"**{HELP_TITLE}**"):
            discord("PATCH", f"/channels/{target}/messages/{message['id']}", body)
            set_setting("control_help_message_id", str(message["id"]))
            set_setting("control_help_hash", digest)
            return {"posted": False, "adopted": True}
    sent = discord(
        "POST",
        f"/channels/{target}/messages",
        {**body, "nonce": "help:" + digest, "enforce_nonce": True},
    )
    set_setting("control_help_message_id", str(sent["id"]))
    set_setting("control_help_hash", digest)
    try:
        discord("PUT", f"/channels/{target}/messages/pins/{sent['id']}")
    except httpx.HTTPStatusError:
        # Without the pin permission the message stays unpinned; the owner can pin it.
        return {"posted": True, "pinned": False}
    return {"posted": True, "pinned": True}
