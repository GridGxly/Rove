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
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from . import workflow
from .jobs import plain, plain_company, public_link
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
        return {
            "say": "The application queue is switched off, so nothing is being prepared.",
            "outcome": "queue is switched off",
        }
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
    return {
        "say": "\n".join(lines),
        "outcome": f"{len(waiting)} need him, {queued} queued, {len(busy)} in progress",
    }


def waiting() -> dict:
    """Every application that waits on the owner, and where its card is."""
    settings = workflow.config()
    if not settings.get("enabled"):
        return {
            "say": "The application queue is switched off, so nothing waits on you.",
            "outcome": "queue is switched off",
        }
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
        return {"say": "Nothing waits on you right now.", "outcome": "nothing waits on him"}
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
    return {
        "say": "\n".join(say),
        "outcome": f"{len(rows)} wait on him, {digest} on the daily list",
    }


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
    return {"say": "\n".join(say), "outcome": f"{len(sent)} sent today"}


def pause_feed() -> dict:
    was = workflow.config().get("feed_paused") is True
    set_setting("feed_paused", True)
    return {
        "say": "Paused. Jobs from the feed stay in the queue and wait. Links you paste and "
        "jobs you pick still go, and the one in progress finishes.",
        "outcome": "feed was already paused" if was else "feed paused",
    }


def resume_feed() -> dict:
    was = workflow.config().get("feed_paused") is True
    set_setting("feed_paused", False)
    return {
        "say": "The feed is running again. Its jobs take their turn in the queue.",
        "outcome": "feed resumed" if was else "feed was not paused",
    }


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
        return {"say": "Which company? Give me its name.", "outcome": "no company named"}
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
        name = plain_company(payload.get("company"))
        named = wanted == name or (len(wanted) >= 3 and f" {wanted} " in f" {name} ")
        if not named or decision["application_id"] in seen:
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
        return {
            "say": f"I have no record of {clip(company, 40)}: not applied, not skipped.",
            "outcome": "no record of that company",
        }
    return {"say": "\n".join(lines), "outcome": f"{len(lines)} records found"}


# --- what he asks for in his own words ------------------------------------------
#
# The model works out what he wants, however he phrases it, and calls one of these
# with its reading as arguments. Code never re-reads his wording to decide what he
# meant; it checks facts and acts: a link must be in a message he wrote, a name must
# match one of his applications, an answer must be in his own words. Each returns the
# line to send him and, for the system log, what actually happened.

LOOKBACK = timedelta(minutes=30)  # how far back his own messages count as his
OWNER_MESSAGES = 50  # how many of the newest messages in agent-control are read
# Where `go` and `park it` mean something: what waits on him, or what was parked.
RETRYABLE = {"NEEDS_USER", "READY_FOR_REVIEW", "MANUAL_TAKEOVER", "DEFERRED"}
UNPARKABLE = {"APPLIED", "SUBMITTING", "UNKNOWN_SUBMISSION", "DEFERRED", *workflow.POST_APPLICATION}


def owner_id() -> str:
    from .discord_feed import private_env

    env = private_env()
    return env.get("DISCORD_OWNER_USER_ID") or env.get("DISCORD_ALLOWED_USERS", "").split(",")[0]


def owner_messages() -> list[dict]:
    """His own messages in agent-control from the last LOOKBACK, newest first, read from
    Discord: the author id is checked, and no bot, webhook or other user counts."""
    from . import inbound
    from .discord_feed import discord

    target = workflow.config().get("control_channel_id")
    owner = owner_id()
    if not target or not owner:
        return []
    messages = discord("GET", f"/channels/{target}/messages?limit={OWNER_MESSAGES}")
    cutoff = datetime.now(UTC) - LOOKBACK
    mine = []
    for message in messages if isinstance(messages, list) else []:
        stamp = str(message.get("timestamp") or "1970-01-01T00:00:00+00:00")
        if datetime.fromisoformat(stamp) >= cutoff and inbound.from_owner(message, owner):
            mine.append(message)
    return sorted(mine, key=lambda m: int(m["id"]), reverse=True)


def link_key(url) -> str:
    """A link compared loosely: host and path, without query, fragment or a final slash."""
    safe = public_link(str(url or "").strip().strip("<>\"'`"))
    if not safe:
        return ""
    parts = urlsplit(safe)
    return parts.netloc.lower() + parts.path.rstrip("/")


def his_link(url) -> str | None:
    """The link as he pasted it, when one of his recent messages has it; None otherwise."""
    from . import inbound

    wanted = public_link(str(url or "").strip().strip("<>\"'`"))
    key = link_key(url)
    if not wanted or not key:
        return None
    close = None
    for message in owner_messages():
        for link in inbound.links_in(message.get("content")):
            safe = public_link(link)
            if safe == wanted:
                return safe
            if safe and close is None and link_key(safe) == key:
                close = safe
    return close


def latest_link() -> str | None:
    """The link he pasted most recently, from his own messages of the last half hour."""
    from . import inbound

    cutoff = datetime.now(UTC) - inbound.JUST_PASTED
    for message in owner_messages():  # newest first
        stamp = str(message.get("timestamp") or "1970-01-01T00:00:00+00:00")
        if datetime.fromisoformat(stamp) < cutoff:
            break
        for link in inbound.links_in(message.get("content")):
            safe = public_link(link)
            if safe:
                return safe
    return None


def in_his_words(text) -> bool:
    """Whether every word of `text` is in one recent message of his."""
    wanted = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    if not wanted:
        return False
    return any(
        wanted <= set(re.findall(r"[a-z0-9]+", str(m.get("content") or "").lower()))
        for m in owner_messages()
    )


def apply_to_link(url: str = "", first: bool = False) -> dict:
    """Queue a job link he asked for, as his own, when he pasted it himself. With no link
    named ("apply now", "start it"), it is the link he pasted last, and it goes first."""
    from . import inbound

    if not workflow.config().get("enabled"):
        return {
            "say": "The application queue is switched off, so I can't queue links right now.",
            "outcome": "queue is switched off, nothing queued",
        }
    if not str(url or "").strip():
        url = latest_link() or ""
        if not url:
            return {
                "say": "Paste the job link and I'll start on it.",
                "outcome": "no link named and none pasted lately, nothing queued",
            }
        first = True  # "now" about the link he just pasted: ahead of his other links
    if not public_link(str(url or "").strip().strip("<>\"'`")):
        return {
            "say": "That isn't a full job link. Paste the https:// link itself.",
            "outcome": "not a public link, nothing queued",
        }
    link = his_link(url)
    if link is None:
        return {
            "say": "I can only queue a link you pasted here yourself. Paste it and I'll queue it.",
            "outcome": "link not in his messages, nothing queued",
        }
    try:
        line = inbound.queue_pasted([link], first=bool(first))
    except (ValueError, PermissionError) as error:
        return {"say": str(error), "outcome": f"nothing queued, {type(error).__name__}"}
    done = "link already tracked" if line.startswith("Already") else "queued his link"
    return {"say": line, "outcome": done + (", put first" if first else "")}


def find_applications(name) -> list[dict]:
    """His applications whose company or role carries the words given, newest first.

    Every word must start a word of the company, the role or the title; failing that, a
    company that has one of the words is enough.
    """
    from .mail import split_title

    wanted = [w for w in plain(name).split() if len(w) >= 2]
    if not wanted:
        return []
    rows = sorted(queue_rows(), key=lambda r: r["updated_at"], reverse=True)
    texts = []
    for row in rows:
        company, role = split_title(row)
        texts.append(
            (
                row,
                f" {plain(company)} ",
                f" {plain(company)} {plain(role)} {plain(workflow.display_title(row))} ",
            )
        )
    strict = [row for row, _c, every in texts if all(f" {w}" in every for w in wanted)]
    if strict:
        return strict
    return [
        row for row, company, _e in texts if any(len(w) >= 3 and f" {w}" in company for w in wanted)
    ]


def which_of(rows: list[dict]) -> str:
    return "Which one: " + "; ".join(title_of(row) for row in rows[:LISTED]) + "?"


def command_id() -> str:
    """A fresh id for a command the chat asked for, so it is applied exactly once."""
    return "chat:" + uuid.uuid4().hex


def act_on(name, word: str, allowed: set, doing: str) -> dict:
    """`go` or `park it` on the one application `name` points to, as in its thread."""
    from . import worker

    matches = find_applications(name)
    if not matches:
        return {
            "say": f"I don't have an application matching “{clip(name, 40)}”.",
            "outcome": "no application matches",
        }
    actionable = [row for row in matches if row["status"] in allowed]
    if len(actionable) > 1:
        return {
            "say": which_of(actionable),
            "outcome": f"{len(actionable)} applications match, asked which",
        }
    if not actionable:
        row = matches[0]
        state = workflow.STATE_WORDS.get(row["status"], "tracked").lower()
        return {
            "say": f"{title_of(row)} is {application_line(row)}, so there's nothing to {doing}.",
            "outcome": f"nothing to {doing}, it is {state}",
        }
    row = actionable[0]
    command = worker.thread_command(word, row["id"])
    if command is None:
        raise ValueError(f"{word!r} is not a reply an application thread takes")
    try:
        worker.apply_command(command, command_id())
    except (ValueError, PermissionError) as error:
        return {"say": str(error), "outcome": f"could not {doing}, {type(error).__name__}"}
    return {"row": row}


def retry_application(name: str) -> dict:
    """`go` on one of his applications that waits or was parked: prepare it again."""
    from . import inbound

    # One still in the queue is not retried: his word moves it to the front.
    done = act_on(name, "go", RETRYABLE | {"QUEUED"}, "retry")
    if "row" not in done:
        return done
    row = done["row"]
    where = inbound.where_it_stands(row["id"])
    if row["status"] == "QUEUED":
        return {
            "say": f"Moved {title_of(row)} to the front. {where}",
            "outcome": "moved one queued application to the front",
        }
    return {"say": f"Going again on {title_of(row)}. {where}", "outcome": "retried one application"}


def park_application(name: str) -> dict:
    """`park it` on one of his applications: it stops and waits until he says go."""
    allowed = set(workflow.STATES) - UNPARKABLE
    done = act_on(name, "park it", allowed, "park")
    if "row" not in done:
        return done
    return {
        "say": f"Parked {title_of(done['row'])}. Say “try it again” when you want it back.",
        "outcome": "parked one application",
    }


def open_questions(application_id: str) -> list[tuple[int, dict]]:
    """The open questions of an application's latest stop, with their numbers in its list."""
    hold = workflow.latest_hold(application_id) or {}
    return [
        (number, q)
        for number, q in enumerate(hold.get("questions") or [], start=1)
        if q.get("state", "open") == "open"
    ]


def waiting_application(name) -> dict:
    """{"row": row} for the one application `name` points to that waits on an answer;
    otherwise what to say."""
    matches = find_applications(name)
    waiting = [r for r in matches if r["status"] == "NEEDS_USER" and open_questions(r["id"])]
    if len(waiting) == 1:
        return {"row": waiting[0]}
    if waiting:
        return {
            "say": which_of(waiting),
            "outcome": f"{len(waiting)} applications match, asked which",
        }
    if matches:
        return {
            "say": f"Nothing on {title_of(matches[0])} is waiting for an answer right now.",
            "outcome": "no open question there, nothing saved",
        }
    return {
        "say": f"I don't have an application matching “{clip(name, 40)}”.",
        "outcome": "no application matches",
    }


def pick_question(asked: list[tuple[int, dict]], question: str | None) -> tuple[int, dict] | None:
    """The one open question his answer is for: the only one open, or the one whose
    label best carries the words of `question`. None when that is not exactly one."""
    if not question:
        return asked[0] if len(asked) == 1 else None
    terms = [w for w in plain(question).split() if len(w) >= 3]
    scored = [(sum(f" {t}" in f" {plain(q.get('label'))} " for t in terms), n, q) for n, q in asked]
    best = max((s for s, _n, _q in scored), default=0)
    top = [(n, q) for s, n, q in scored if s == best and s > 0]
    return top[0] if len(top) == 1 else None


def checked_answer(answer: str, picked: dict, named: bool, title: str) -> dict:
    """{"value": text} for an answer that may be saved; otherwise what to say. A legal or
    personal question must be named, the answer must be in his own words, and a question
    with options takes one of them."""
    from . import questions

    label = str(picked.get("label") or "")
    options = questions.real_options(picked.get("options"))
    if questions.is_sensitive(label, picked.get("kind") or "", options) and not named:
        return {
            "say": f"To be sure: is “{clip(answer, 60)}” your answer to "
            f"“{clip(label, 80)}” for {title}?",
            "outcome": "asked to confirm a personal question",
        }
    if not in_his_words(answer):
        return {
            "say": "I only fill in answers you typed yourself. Tell me the answer in your words.",
            "outcome": "answer not in his messages, nothing saved",
        }
    if not options:
        return {"value": answer}
    key = " ".join(plain(answer).split())
    value = next((o for o in options if " ".join(plain(o).split()) == key), "")
    if value:
        return {"value": value}
    return {
        "say": f"“{clip(label, 60)}” takes one of: " + " / ".join(options[:8]) + ". Which one?",
        "outcome": "answer is not one of the options, nothing saved",
    }


def answer_application(name: str, answer: str, question: str | None = None) -> dict:
    """His answer to an open question of one application, applied like `N: answer` in
    its thread. The answer must be in his own words; a legal or personal question also
    needs `question` naming it, and an answer with options must be one of them."""
    from . import worker

    answer = " ".join(str(answer or "").split())
    if not answer:
        return {"say": "What's the answer?", "outcome": "no answer given, nothing saved"}
    found = waiting_application(name)
    if "row" not in found:
        return found
    row = found["row"]
    asked = open_questions(row["id"])
    chosen = pick_question(asked, question)
    if chosen is None:
        listing = "; ".join(f"{n}) {clip(q.get('label'), 70)}" for n, q in asked[:6])
        return {
            "say": f"{title_of(row)} has {len(asked)} open questions: {listing}. "
            "Which one is it for?",
            "outcome": "asked which question",
        }
    number, picked = chosen
    label = str(picked.get("label") or "")
    checked = checked_answer(answer, picked, bool(question), title_of(row))
    if "value" not in checked:
        return checked
    value = checked["value"]
    command = {
        "kind": "answer",
        "application_id": row["id"],
        **worker.question_by_number(row["id"], number),
        "value": value,
        "number": number,
    }
    try:
        worker.apply_command(command, command_id())
    except PermissionError as error:
        from .recovery import LOST_FORM

        if str(error) == LOST_FORM:
            return {"say": LOST_FORM, "outcome": "question not on the saved form, nothing saved"}
        return {
            "say": f"“{clip(label, 60)}” has to be answered by you in the browser, not here.",
            "outcome": "question is manual only, nothing saved",
        }
    except ValueError as error:
        return {"say": str(error), "outcome": "answer refused, nothing saved"}
    left = len(asked) - 1
    then = (
        f" {left} more question{'s' if left != 1 else ''} open there."
        if left
        else " Say “try it again” and I'll go on with it."
    )
    return {
        "say": f"Saved for {title_of(row)}: “{clip(label, 60)}” → {clip(value, 80)}.{then}",
        "outcome": f"saved his answer to question {number}",
    }


# --- the pinned help message in agent-control ----------------------------------

HELP_TITLE = "What you can ask Rove"


def examples(settings: dict) -> list[str]:
    """What he can type, one line each. Every example here works today; keep it that way."""
    memory = channel(settings, "memory_channel_id", "memory")
    return [
        "• Paste a job link — I queue it as yours (add `first` to jump the line)",
        "• `status` — what I'm doing, the queue, sends today",
        "• `what's waiting on me` — what needs you, and where",
        "• `why did you skip Acme` — what happened with one company",
        "• `how many did you send today` — sends and the daily cap",
        "• `pause` / `resume` — hold or restart jobs from the feed",
        "• `where do I go to school` — any fact from your approved profile",
        f"• `what do you know about me` — that list lives in {memory}: type `list` there",
    ]


def help_text(settings: dict | None = None) -> str:
    """The pinned message in agent-control."""
    settings = workflow.config() if settings is None else settings
    action = channel(settings, "action_channel_id", "action-needed")
    system = channel(settings, "system_channel_id", "system-log")
    return "\n".join(
        [
            f"**{HELP_TITLE}**",
            "Type here like you would text someone. These work:",
            *examples(settings),
            (
                f"Replies like `go` or `send it` belong on a card in {action} or in the "
                f"application's thread. The step-by-step log is in {system}."
            ),
        ]
    )


def help_reply() -> dict:
    """The same examples as a short chat answer: fewer lines for the model to pass on."""
    return {
        "say": "\n".join(["Things you can ask me:", *examples(workflow.config())]),
        "outcome": "showed the help",
    }


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
