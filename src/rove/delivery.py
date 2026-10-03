"""What reaches Discord, when, and in how many messages.

The thread record (`application_events`) and the system log (`system_outbox`) are
outboxes in SQLite: a row is written first and delivered afterward, so a Discord outage
or a crash delays the record without losing it.

Batching. Every pending row of one outbox is claimed as one batch and packed into as
few messages as Discord's limits allow: quiet lines share one message's text, cards
share one message's embeds (up to 10, 6,000 characters across them), and a card too
big for one embed is split across embeds and messages. Text is shown above embeds, so a
line that comes after a card starts a new message and the thread still reads in order.

Crash safety. A batch is durable: its rows carry its token, and `delivery_batches` counts
the messages already posted. A batch that was interrupted is resumed before anything new
is claimed, rebuilt from the same rows into the same messages, each with the same nonce,
which Discord is asked to enforce; a message that did reach Discord just before the crash
is not posted twice.

Refusals. A message Discord refuses with a 4xx that is not a rate limit is retried on
later passes, its rows alone, at most three times; then the rows are marked failed with
one system-log line. A thread that is archived is reopened once; a locked or deleted
thread stops taking posts, with one system-log line, and the application carries on.

Off the critical path. While the worker's delivery window is open (one application being
prepared and sent), thread rows and system-log lines are only written; the live status
card, the owner-channel cards and a hold card are still delivered at once. The window
closes at the end of the tick and everything waiting is delivered then.
"""

import contextlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .runtime import state_root

# Discord's limits for one message.
CONTENT_LIMIT = 2000
EMBEDS_PER_MESSAGE = 10
EMBED_TOTAL = 6000
FIELDS_PER_EMBED = 25
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
# Rove stays a little under the hard caps.
CONTENT_BUDGET = 1900
EMBED_BUDGET = 5800
# A message Discord refuses this many times is given up on.
REJECTIONS = 3
# A batch takes at most this many rows; the rest wait for the next pass.
BATCH_ROWS = 200
# A window older than this was left by a crash and no longer defers anything.
WINDOW = timedelta(minutes=30)
# Rows that end a pass on the owner: delivered at once even inside the window.
URGENT = frozenset({"needs_action", "submission_unknown", "shortlisted"})
# Outages this process met while delivering; a flush of every outbox stops at the first.
outages = [0]

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_batches(
  token TEXT PRIMARY KEY, scope TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS system_outbox(
  id INTEGER PRIMARY KEY, application_id TEXT NOT NULL, text TEXT NOT NULL,
  created_at TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending', batch TEXT,
  attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS delivery_windows(name TEXT PRIMARY KEY, opened_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closed_threads(
  thread_id TEXT PRIMARY KEY, reason TEXT NOT NULL, closed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS status_cards(application_id TEXT PRIMARY KEY, digest TEXT NOT NULL);
"""
COLUMNS = {
    "application_events": (("batch", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0")),
    "owner_notices": (("attempts", "INTEGER NOT NULL DEFAULT 0"),),
}
_ready: set = set()


def _workflow():
    from . import workflow

    return workflow


def conn():
    """The state database with the delivery tables and columns in place."""
    db = _workflow().db()
    key = str(state_root())
    if key not in _ready:
        db.executescript(SCHEMA)
        for table, columns in COLUMNS.items():
            have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    # Another service may add it in the same moment; either one will do.
                    with contextlib.suppress(Exception):
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        _ready.add(key)
    return db


def now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Sizes, in the units Discord counts (UTF-16 code units).
# ---------------------------------------------------------------------------


def units(text) -> int:
    return len(str(text if text is not None else "").encode("utf-16-le")) // 2


def clip(text, limit: int) -> str:
    text = str(text if text is not None else "")
    if units(text) <= limit:
        return text
    cut = text[:limit]
    while cut and units(cut) > limit - 1:
        cut = cut[:-1]
    return cut + "…"


def embed_size(card: dict) -> int:
    size = units(card.get("title", "")) + units(card.get("description", ""))
    size += units((card.get("footer") or {}).get("text", ""))
    size += units((card.get("author") or {}).get("name", ""))
    for item in card.get("fields") or []:
        size += units(item.get("name", "")) + units(item.get("value", ""))
    return size


def fit_embed(card: dict) -> list[dict]:
    """One card as one or more embeds, each within every per-embed limit and the budget.

    Fields that do not fit move to continuation embeds titled "… (continued)", in order;
    the footer goes on the last one. Nothing is dropped except text over a field's cap.
    """
    title = clip(card.get("title", ""), TITLE_LIMIT)
    footer = clip((card.get("footer") or {}).get("text", ""), FOOTER_LIMIT)
    head = {k: v for k, v in card.items() if k not in {"fields", "footer"}}
    if title:
        head["title"] = title
    if "description" in head:
        room = EMBED_BUDGET - units(title) - units(footer)
        head["description"] = clip(head["description"], min(DESCRIPTION_LIMIT, max(room, 1)))
    fields = [
        {
            "name": clip(item.get("name") or "\u200b", FIELD_NAME_LIMIT) or "\u200b",
            "value": clip(item.get("value") or "—", FIELD_VALUE_LIMIT) or "—",
            "inline": bool(item.get("inline")),
        }
        for item in card.get("fields") or []
    ]
    parts: list[dict] = []
    current = head
    for item in fields:
        grown = embed_size(current) + units(item["name"]) + units(item["value"])
        has_body = bool(current.get("fields") or current.get("description"))
        if len(current.get("fields", [])) >= FIELDS_PER_EMBED or (
            has_body and grown + units(footer) > EMBED_BUDGET
        ):
            parts.append(current)
            current = {"title": clip(title, TITLE_LIMIT - 12) + " (continued)"}
            if "color" in card:
                current["color"] = card["color"]
        current.setdefault("fields", []).append(item)
    parts.append(current)
    if footer:
        parts[-1]["footer"] = {"text": footer}
    return parts


# ---------------------------------------------------------------------------
# Packing rows into messages.
# ---------------------------------------------------------------------------


@dataclass
class Message:
    rows: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    embeds: list = field(default_factory=list)
    file: dict | None = None

    def empty(self) -> bool:
        return not (self.lines or self.embeds or self.file)

    def copy(self) -> "Message":
        return Message(list(self.rows), list(self.lines), list(self.embeds), self.file)

    def take(self, piece: tuple) -> bool:
        """Add one piece if it fits and keeps the reading order; False leaves it out."""
        kind, value = piece
        if self.file is not None:
            return False  # anything after a file would show above it
        if kind == "embed":
            if len(self.embeds) >= EMBEDS_PER_MESSAGE:
                return False
            if sum(embed_size(e) for e in self.embeds) + embed_size(value) > EMBED_BUDGET:
                return False
            self.embeds.append(value)
            return True
        line = value["line"] if kind == "file" else value
        if self.embeds or units("\n".join([*self.lines, line])) > CONTENT_BUDGET:
            return False  # text shows above embeds: a line after a card starts a new message
        self.lines.append(line)
        if kind == "file":
            self.file = value
        return True

    def payload(self, nonce: str) -> dict:
        body: dict = {"allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}
        if self.lines:
            body["content"] = "\n".join(self.lines)
        if self.embeds:
            body["embeds"] = self.embeds
        return body


def pieces(items) -> list[tuple]:
    """A row's rendered items as packable pieces: lines, embeds within limits, files."""
    result = []
    for item in items or []:
        if isinstance(item, str):
            result.append(("line", clip(item, CONTENT_BUDGET)))
        elif isinstance(item, dict) and "attachment" in item:
            result.append(
                ("file", {"path": item["attachment"], "line": clip(item.get("line", ""), 1800)})
            )
        elif isinstance(item, dict):
            result.extend(("embed", part) for part in fit_embed(item))
    return result


def pack(entries) -> list[Message]:
    """Rows in order as few messages as the limits and the reading order allow. A row may
    span messages (a card and the line after it); it counts as delivered when its last
    message is. A `solo` row (one Discord refused before) shares a message with no other."""
    messages: list[Message] = []
    current = Message()

    def close():
        nonlocal current
        if not current.empty():
            messages.append(current)
        current = Message()

    for row_id, items, solo in entries:
        parts = pieces(items)
        if not parts:
            continue
        if solo:
            close()
        for part in parts:
            if not current.take(part):
                close()
                current.take(part)  # an empty message takes any single piece
            if row_id not in current.rows:
                current.rows.append(row_id)
        if solo:
            close()
    close()
    return messages


# Qwen's drafting step records each question in form order, a draft card or an "only you"
# line by turns; read together, the cards come first and the lines after them.
DRAFT_KINDS = ("qwen_answer_proposal", "qwen_question")


def reading_order(rows) -> list:
    """Rows in id order, except that a run of drafting rows lists its cards first."""
    ordered, run = [], []
    for row in [*rows, None]:
        if row is not None and row["kind"] in DRAFT_KINDS:
            run.append(row)
            continue
        run.sort(key=lambda r: DRAFT_KINDS.index(r["kind"]))  # stable: form order kept
        ordered.extend(run)
        run = []
        if row is not None:
            ordered.append(row)
    return ordered


# ---------------------------------------------------------------------------
# The delivery window and thread state.
# ---------------------------------------------------------------------------


def window_open() -> bool:
    cutoff = (datetime.now(UTC) - WINDOW).isoformat()
    with conn() as db:
        return bool(
            db.execute("SELECT 1 FROM delivery_windows WHERE opened_at>?", (cutoff,)).fetchone()
        )


def open_window(name: str = "worker"):
    with conn() as db:
        db.execute("INSERT OR REPLACE INTO delivery_windows VALUES(?,?)", (name, now()))


def close_windows():
    with conn() as db:
        db.execute("DELETE FROM delivery_windows")


def urgent_pending(application_id: str) -> bool:
    marks = ",".join("?" * len(URGENT))
    with conn() as db:
        return bool(
            db.execute(
                f"SELECT 1 FROM application_events WHERE application_id=? "
                f"AND delivery='pending' AND kind IN ({marks}) LIMIT 1",
                (application_id, *sorted(URGENT)),
            ).fetchone()
        )


def thread_closed(thread: str) -> str | None:
    with conn() as db:
        row = db.execute(
            "SELECT reason FROM closed_threads WHERE thread_id=?", (thread,)
        ).fetchone()
    return row[0] if row else None


class Closed(Exception):
    """The thread takes no posts: locked, deleted, or archived beyond reopening."""


class Rejected(Exception):
    def __init__(self, error: httpx.HTTPStatusError):
        super().__init__(str(error))
        self.error = error


def status_of(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    return response.status_code if response is not None else None


def error_code(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    try:
        return int(response.json().get("code"))
    except Exception:  # noqa: BLE001 -- a body without a code is just a status
        return None


def transient(error: Exception) -> bool:
    """An outage or a rate limit: worth the next pass, never counted as a refusal."""
    status = status_of(error)
    return status is None or status == 429 or status >= 500


def reopen(thread: str, error: httpx.HTTPStatusError) -> bool:
    """After Discord refused a post to a thread: True when the thread was archived and is
    open again, False when the refusal was about the message. Raises Closed when the
    thread is locked, deleted, or out of reach."""
    discord = _workflow().discord
    status, code = status_of(error), error_code(error)
    if status == 404 or code == 10003:
        raise Closed("the thread was deleted")
    if code == 160005:
        raise Closed("the thread is locked")
    if code != 50083 and status != 403:
        return False
    if code != 50083:
        try:
            info = discord("GET", f"/channels/{thread}") or {}
        except httpx.HTTPStatusError as failure:
            if transient(failure):
                raise
            raise Closed("the thread is out of reach") from failure
        meta = (info.get("thread_metadata") or {}) if isinstance(info, dict) else {}
        if meta.get("locked"):
            raise Closed("the thread is locked")
        if not meta.get("archived"):
            return False
    try:
        discord("PATCH", f"/channels/{thread}", {"archived": False})
    except httpx.HTTPStatusError as failure:
        if transient(failure):
            raise
        raise Closed("the thread is archived and could not be reopened") from failure
    return True


def to_thread(thread: str, call):
    """Run one Discord call aimed at a thread; an archived thread is reopened once and the
    call made again. Raises Closed, Rejected, or the transport's own error."""
    try:
        return call()
    except httpx.HTTPStatusError as error:
        if transient(error):
            raise
        if not reopen(thread, error):
            raise Rejected(error) from error
    try:
        return call()
    except httpx.HTTPStatusError as error:
        if transient(error):
            raise
        raise Rejected(error) from error


def mark_closed(application_id: str, thread: str, reason: str):
    """A thread that takes no posts: what is waiting stays in SQLite, marked closed, and
    one system-log line says so. Later entries are kept the same way, silently."""
    with conn() as db:
        db.execute("INSERT OR IGNORE INTO closed_threads VALUES(?,?,?)", (thread, reason, now()))
        count = db.execute(
            "UPDATE application_events SET delivery='closed',batch=NULL WHERE application_id=? "
            "AND delivery IN ('pending','sending')",
            (application_id,),
        ).rowcount
        db.execute("DELETE FROM delivery_batches WHERE scope=?", (application_id,))
    _workflow().system_line(
        application_id,
        f"thread closed · {reason} · {count} entr{'y' if count == 1 else 'ies'} kept locally, "
        "not posted; the application carries on",
    )


# ---------------------------------------------------------------------------
# The outboxes.
# ---------------------------------------------------------------------------


@dataclass
class Box:
    table: str
    scope: str  # the application id, or "system"
    channel: str
    prefix: str  # nonce prefix: Discord allows 25 characters in all
    thread: bool
    kind: str  # the word in the private delivery log

    @property
    def where(self) -> tuple[str, tuple]:
        if self.table == "application_events":
            return "application_id=?", (self.scope,)
        return "1=1", ()

    def render(self, row) -> list:
        if self.table == "application_events":
            if row["kind"] == "forum_creation_attempt":
                return []
            return _workflow().event_embeds(self.scope, row["kind"], json.loads(row["data"]))
        return [row["text"]]


def open_tokens(box: Box) -> list[str]:
    clause, args = box.where
    with conn() as db:
        return [
            row[0]
            for row in db.execute(
                f"SELECT batch FROM {box.table} WHERE {clause} AND delivery='sending' "
                "AND batch IS NOT NULL GROUP BY batch ORDER BY MIN(id)",
                args,
            )
        ]


def claim(box: Box) -> str | None:
    """Take every waiting row (up to BATCH_ROWS) as one new batch; None when none wait.
    A row left 'sending' without a batch by older code is taken again."""
    clause, args = box.where
    token = uuid.uuid4().hex[:10]
    with conn() as db:
        claimed = db.execute(
            f"UPDATE {box.table} SET delivery='sending',batch=? WHERE id IN "
            f"(SELECT id FROM {box.table} WHERE {clause} AND (delivery='pending' OR "
            f"(delivery='sending' AND batch IS NULL)) ORDER BY id LIMIT ?)",
            (token, *args, BATCH_ROWS),
        ).rowcount
        if claimed:
            db.execute(
                "INSERT INTO delivery_batches(token,scope,sent,created_at) VALUES(?,?,0,?)",
                (token, box.scope, now()),
            )
    return token if claimed else None


def post(box: Box, message: Message, nonce: str):
    from . import discord_feed

    payload = message.payload(nonce)
    path = Path(message.file["path"]) if message.file else None
    if path is not None and not path.is_file():
        payload["content"] = (
            payload.get("content", "") + " · the file is no longer on this Mac"
        ).strip()
        path = None

    def call():
        if path is not None:
            return discord_feed.discord_upload(box.channel, path, payload)
        return _workflow().discord("POST", f"/channels/{box.channel}/messages", payload)

    if box.thread:
        return to_thread(box.channel, call)
    try:
        return call()
    except httpx.HTTPStatusError as error:
        if transient(error):
            raise
        raise Rejected(error) from error


def send(box: Box, token: str, fresh: bool = False) -> bool:
    """Post one batch from where it stopped. True when it is done (delivered, or refused
    and put back); False when the pass should stop here (an outage or a closed thread).
    `fresh` is a batch just claimed, none of whose messages was ever attempted."""
    workflow = _workflow()
    with conn() as db:
        rows = db.execute(
            f"SELECT * FROM {box.table} WHERE batch=? ORDER BY id", (token,)
        ).fetchall()
        mark = db.execute("SELECT sent FROM delivery_batches WHERE token=?", (token,)).fetchone()
    if box.table == "application_events":
        rows = reading_order(rows)
    start = mark[0] if mark else 0
    entries = []
    silent = []
    broken = []
    for row in rows:
        try:
            items = box.render(row)
        except Exception:  # noqa: BLE001 -- one unreadable row must not hold the record up
            broken.append(row["id"])
            continue
        if not pieces(items):
            silent.append(row["id"])
        entries.append((row["id"], items, bool(row["attempts"])))
    messages = pack(entries)
    if silent or broken:
        with conn() as db:
            db.executemany(
                f"UPDATE {box.table} SET delivery=? WHERE id=? AND delivery='sending'",
                [("sent", row_id) for row_id in silent] + [("failed", r) for r in broken],
            )
    if broken:
        workflow.delivery_failed(
            box.kind,
            ",".join(map(str, broken)),
            ValueError("unreadable row"),
            box.scope,
            announce=False,
        )
    last = {}
    for index, message in enumerate(messages):
        for row_id in message.rows:
            last[row_id] = index
    for index in range(start, len(messages)):
        message = messages[index]
        try:
            post(box, message, f"{box.prefix}{token}.{index}")
        except Closed as closed:
            mark_closed(box.scope, box.channel, str(closed))
            return False
        except Rejected as refused:
            reject(box, token, message, refused.error)
            return False
        except (httpx.HTTPError, OSError) as error:
            # Discord is down or rate-limiting. When nothing of this batch can have reached
            # Discord, its rows simply wait again; otherwise the batch stays as it is and
            # the next pass resumes it at this message with the same nonce.
            outages[0] += 1
            workflow.delivery_failed(box.kind, token, error, box.scope, announce=False)
            if fresh and index == 0 and not_delivered(error):
                release(box, token)
            return False
        done = [row_id for row_id, at in last.items() if at == index]
        with conn() as db:
            db.execute("UPDATE delivery_batches SET sent=? WHERE token=?", (index + 1, token))
            db.executemany(
                f"UPDATE {box.table} SET delivery='sent' WHERE id=? AND delivery='sending'",
                [(row_id,) for row_id in done],
            )
        if message.file:
            Path(message.file["path"]).unlink(missing_ok=True)
    with conn() as db:
        db.execute(
            f"UPDATE {box.table} SET delivery='sent' WHERE batch=? AND delivery='sending'",
            (token,),
        )
        db.execute("DELETE FROM delivery_batches WHERE token=?", (token,))
    return True


def not_delivered(error: Exception) -> bool:
    """Discord certainly did not take the message: no connection, or a rate limit."""
    if isinstance(error, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return True
    return status_of(error) == 429


def release(box: Box, token: str):
    with conn() as db:
        db.execute(
            f"UPDATE {box.table} SET delivery='pending',batch=NULL WHERE batch=? "
            "AND delivery='sending'",
            (token,),
        )
        db.execute("DELETE FROM delivery_batches WHERE token=?", (token,))


def reject(box: Box, token: str, message: Message, error: httpx.HTTPStatusError):
    """Discord refused one message: its rows count one refusal and go back alone; a row
    refused REJECTIONS times is failed, with one system-log line. The batch's other
    undelivered rows simply wait again."""
    workflow = _workflow()
    failed = []
    with conn() as db:
        for row_id in message.rows:
            row = db.execute(f"SELECT attempts FROM {box.table} WHERE id=?", (row_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            final = attempts >= REJECTIONS
            db.execute(
                f"UPDATE {box.table} SET delivery=?,attempts=?,batch=NULL WHERE id=?",
                ("failed" if final else "pending", attempts, row_id),
            )
            if final:
                failed.append(row_id)
        db.execute(
            f"UPDATE {box.table} SET delivery='pending',batch=NULL WHERE batch=? "
            "AND delivery='sending'",
            (token,),
        )
        db.execute("DELETE FROM delivery_batches WHERE token=?", (token,))
    rows = ",".join(str(r) for r in message.rows)
    workflow.delivery_failed(box.kind, rows, error, box.scope, announce=False)
    if failed and box.table != "system_outbox":
        workflow.system_line(
            box.scope,
            f"delivery failed · {box.kind} {','.join(map(str, failed))} · Discord refused it "
            f"{REJECTIONS} times ({status_of(error)} {error_code(error) or ''}) · given up".strip(),
        )


def deliver(box: Box):
    """Resume interrupted batches first, then claim and send what waits, in order."""
    for token in open_tokens(box):
        if not send(box, token):
            return
    token = claim(box)
    if token:
        send(box, token, fresh=True)


def deliver_events(application_id: str, thread: str):
    if thread_closed(thread):
        with conn() as db:
            db.execute(
                "UPDATE application_events SET delivery='closed',batch=NULL "
                "WHERE application_id=? AND delivery IN ('pending','sending')",
                (application_id,),
            )
        return
    deliver(Box("application_events", application_id, thread, "e", True, "event"))


def queue_system(application_id: str, text: str):
    with conn() as db:
        db.execute(
            "INSERT INTO system_outbox(application_id,text,created_at) VALUES(?,?,?)",
            (str(application_id), text, now()),
        )


def deliver_system(channel: str):
    deliver(Box("system_outbox", "system", channel, "s", False, "system"))


def system_waiting() -> bool:
    with conn() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM system_outbox WHERE delivery IN ('pending','sending') LIMIT 1"
            ).fetchone()
        )


# ---------------------------------------------------------------------------
# The live status card: edited only when what it says changes.
# ---------------------------------------------------------------------------


def card_digest(card: dict) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()


def status_unchanged(application_id: str, digest: str) -> bool:
    with conn() as db:
        row = db.execute(
            "SELECT digest FROM status_cards WHERE application_id=?", (application_id,)
        ).fetchone()
    return bool(row) and row[0] == digest


def remember_status(application_id: str, digest: str):
    with conn() as db:
        db.execute("INSERT OR REPLACE INTO status_cards VALUES(?,?)", (application_id, digest))


def stale_status_cards() -> list[str]:
    """Status cards whose last edit failed; the tick tries them again."""
    with conn() as db:
        return [r[0] for r in db.execute("SELECT application_id FROM status_cards WHERE digest=''")]
