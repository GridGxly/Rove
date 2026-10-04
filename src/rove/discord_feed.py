"""Deterministic Keryx notifications; never run Qwen or authorize an application."""

import contextlib
import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

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


# ---------------------------------------------------------------------------
# One HTTPS client per process for every Discord call: the worker's tick, the browser
# service and the mail and feed services each keep theirs, so a tick pays one TLS
# handshake instead of one per request. A transport error drops the client (a connection
# that went stale over sleep is not reused) and the next call opens a fresh one.
# ---------------------------------------------------------------------------
API = "https://discord.com/api/v10"
_client: dict = {}
_client_lock = threading.Lock()
# Per thread: Discord's own clock from the latest response this thread received.
_seen = threading.local()
# A bucket Discord said is empty is left alone until its reset, up to this long.
_quiet: dict = {}
QUIET_WAIT_LIMIT = 5.0
DISCORD_EPOCH_MS = 1420070400000


def client() -> httpx.Client:
    """The process's Discord client, opened on first use and again after a fork or error."""
    with _client_lock:
        current = _client.get("client")
        if current is None or current.is_closed or _client.get("pid") != os.getpid():
            current = httpx.Client(
                base_url=API,
                timeout=30,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=8, max_keepalive_connections=4, keepalive_expiry=20
                ),
            )
            _client.update(client=current, pid=os.getpid())
        return current


def drop_client():
    """Forget the client after a transport error; a forked child never closes its parent's."""
    with _client_lock:
        current = _client.pop("client", None)
        owned = _client.pop("pid", None) == os.getpid()
    if current is not None and owned:
        with contextlib.suppress(Exception):
            current.close()


def server_time() -> datetime | None:
    """Discord's clock from this thread's latest response, or None when there was none."""
    return getattr(_seen, "date", None)


def empty_cursor() -> str:
    """The cursor for a channel with no messages: a minute before Discord's own clock, or
    zero when that clock is unknown. Never the local clock: a Mac whose clock runs ahead
    of Discord's would skip the owner's next messages."""
    seen = server_time()
    if seen is None:
        return "0"
    millis = int(seen.timestamp() * 1000) - 60_000 - DISCORD_EPOCH_MS
    return str(max(millis, 0) << 22)


def _bucket(method: str, path: str) -> tuple:
    parts = path.split("?")[0].strip("/").split("/")
    return (method, *parts[:2])


def _wait_for_bucket(key: tuple):
    wait = _quiet.get(key, 0.0) - time.monotonic()
    if 0 < wait <= QUIET_WAIT_LIMIT:
        time.sleep(wait)


def _note(key: tuple, response: httpx.Response):
    date = response.headers.get("date")
    if date:
        with contextlib.suppress(TypeError, ValueError, IndexError):
            _seen.date = parsedate_to_datetime(date)
    if response.headers.get("x-ratelimit-remaining") == "0":
        with contextlib.suppress(TypeError, ValueError):
            reset = float(response.headers.get("x-ratelimit-reset-after", ""))
            _quiet[key] = time.monotonic() + min(max(reset, 0.0), QUIET_WAIT_LIMIT)


def _replayable(method: str, payload, error: httpx.TransportError) -> bool:
    """Whether to try once more at once: only a kept-alive connection that went stale
    (Discord closed it while idle, or the Mac slept), and only when a second try cannot
    post twice: the request is idempotent, or Discord drops a repeated nonce. No
    connection at all is an outage, and a timeout is not paid for twice."""
    if not isinstance(error, httpx.RemoteProtocolError | httpx.ReadError | httpx.WriteError):
        return False
    if method in {"GET", "PATCH", "DELETE", "PUT"}:
        return True
    return isinstance(payload, dict) and bool(payload.get("enforce_nonce"))


def _request(method: str, path: str, *, payload=None, send=None) -> httpx.Response:
    """One Discord request through the shared client: a stale connection is replaced once,
    a definite rate limit is waited out up to three times, anything else is the caller's."""
    token = private_env().get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("Discord bot credential is missing")
    headers = {"Authorization": "Bot " + token}
    key = _bucket(method, path)

    def once() -> httpx.Response:
        _seen.date = None
        _wait_for_bucket(key)
        for attempt in range(2):
            try:
                if send is not None:
                    response = send(client(), headers)
                else:
                    response = client().request(method, path, headers=headers, json=payload)
            except httpx.TransportError as error:
                drop_client()
                if attempt or not _replayable(method, payload, error):
                    raise
                continue
            _note(key, response)
            return response
        raise RuntimeError("unreachable")

    response = once()
    for _ in range(3):
        if response.status_code != 429:
            break
        # Only a definite rate-limit rejection is retryable. Timeouts are ambiguous.
        try:
            delay = float(response.json().get("retry_after", 1))
        except ValueError:
            delay = 1.0
        if not 0 <= delay <= 30:
            break
        time.sleep(delay + 0.05)
        response = once()
    response.raise_for_status()
    return response


@timing.call("discord")
def discord_upload(channel: str, path, payload: dict) -> dict:
    """One message with one private file attached (a screenshot or the resume as sent)."""
    import mimetypes
    from pathlib import Path

    file_path = Path(path)
    kind = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    def send(http: httpx.Client, headers: dict) -> httpx.Response:
        # The file is opened per attempt, so a retry after a rate limit sends it whole.
        with file_path.open("rb") as handle:
            return http.post(
                f"/channels/{channel}/messages",
                headers=headers,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (file_path.name, handle, kind)},
                timeout=60,
            )

    return _request("POST", f"/channels/{channel}/messages", payload=payload, send=send).json()


@timing.call("discord")
def discord(method: str, path: str, payload: dict | None = None):
    response = _request(method, path, payload=payload)
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


FEED_ALERT = "feed"
FAILURES_BEFORE_CARD = 3


def failure_words(error: Exception) -> str:
    """What went wrong with the job list, in the owner's words."""
    if isinstance(error, httpx.HTTPStatusError):
        return "the job list's server refused the download"
    if isinstance(error, (httpx.TransportError, OSError)):
        return "I could not reach the job list"
    if isinstance(error, ValueError):
        return "the job list arrived in a shape I do not accept, so I kept the jobs I had"
    return "reading the job list failed"


def checked_sync() -> tuple[dict | None, Exception | None]:
    """Import the feed once; a failure is counted, not raised.

    After FAILURES_BEFORE_CARD failures in a row the system log gets one line and
    action-needed one card; both are said once, and the card leaves on the next import
    that works.
    """
    from . import alerts

    try:
        result = sync_keryx()
    except Exception as error:  # noqa: BLE001 -- counted and surfaced, never a traceback loop
        words = failure_words(error)
        alerts.check(
            FEED_ALERT,
            False,
            error=type(error).__name__,
            after=FAILURES_BEFORE_CARD,
            headline="The job feed stopped updating",
            text=(
                f"The last {FAILURES_BEFORE_CARD} tries to import the job list failed: {words}. "
                "No new jobs come in until it works; jobs already queued still go. This card "
                "leaves on its own when an import works again."
            ),
            card_line=(
                f"feed · {FAILURES_BEFORE_CARD} imports failed in a row · last: "
                f"{type(error).__name__} · {words} · owner card posted"
            ),
            back_line="feed · import works again · card withdrawn",
            log_name="intake",
        )
        return None, error
    alerts.check(
        FEED_ALERT,
        True,
        back_line="feed · import works again · card withdrawn",
        log_name="intake",
        quiet_until_card=True,
    )
    return result, None


def tick(seed: bool = False) -> dict:
    from . import alerts, intake

    config = json.loads((state_root() / "config/feed.json").read_text())
    if not config.get("enabled"):
        return {"enabled": False}
    result, failure = checked_sync()
    if failure is not None:
        result = {
            "sync_failed": type(failure).__name__,
            "failures_in_a_row": alerts.streak(FEED_ALERT),
        }
    if result.get("changed_source") and result.get("revision"):
        close_withdrawn_postings(result["revision"])
    approved = intake.profile_gate(read_approved)
    if approved is None:
        # Nothing is scored or queued against a profile that does not validate; the
        # cursor stays, so these jobs are decided once it does.
        result.update(intake.run_digest(config), profile="needs approval")
        result["finished_at"] = datetime.now(UTC).isoformat()
        write_private(state_root() / "jobs/feed-service.json", result)
        return result
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
        pending = db.execute(
            """SELECT o.* FROM feed_outbox o JOIN jobs j ON j.id=o.job_id
            WHERE o.status='pending' AND j.active=1 ORDER BY o.score DESC,o.rowid DESC LIMIT ?""",
            (intake.number(config, "batch_size", 10),),
        ).fetchall()
        from .workflow import enqueue

        stamp = intake.basis(approved.get("profile_hash", ""))
        # The same role through another board is not queued twice: one listing of it is
        # already queued, in progress or sent. It is kept on record, never announced.
        slots = intake.taken_slots(db) if pending else []
        for row in pending:
            job = json.loads(row["payload"])
            held = intake.slot_taken(job, slots) if job.get("url") else None
            if held:
                with db:
                    intake.mark_duplicate(db, row["key"], held["id"])
                    db.execute(
                        "UPDATE feed_outbox SET status='duplicate' WHERE key=?", (row["key"],)
                    )
                counts["duplicates"] = counts.get("duplicates", 0) + 1
                continue
            queued = (
                enqueue(job["url"], source="keryx", title=job["company"] + " — " + job["title"])
                if job.get("url")
                else None
            )
            card = job_card(job, queued)
            slot = intake.role_slot(job) if queued and not queued.get("already_exists") else None
            if slot:
                slots.append(
                    {"id": queued["application_id"], "urls": {queued["url"]}, "slot": slot}
                )
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
        intake.log_counts(counts, expired)
        result.update(
            sent=sent,
            expired=expired,
            pending=db.execute(
                "SELECT COUNT(*) FROM feed_outbox WHERE status='pending'"
            ).fetchone()[0],
            **{key: counts.get(key, 0) for key in ("queued", "digest", "dropped", "duplicates")},
        )
    finally:
        db.close()
    result.update(intake.run_digest(config))
    result["finished_at"] = datetime.now(UTC).isoformat()
    write_private(state_root() / "jobs/feed-service.json", result)
    return result
