"""Failures the owner never has to see, and the plain words for the ones he does.

A preparation that fails before anything was sent is tried once more on the next tick,
with no card: the application goes back to the queue with a hidden attempt counter, and
only the second failure of the same step reaches the owner, as one card in plain words.
Nothing at or after a send's click is ever retried here; a send has its own rules in
`submission`.

The worker marks each pass it starts and the browser service bumps that mark while it
observes and fills, so a pass whose worker died is handed back after two quiet minutes
instead of fifteen. Free disk space is checked once per tick: below the threshold no new
application starts and one owner card says why, and old feed snapshots are pruned.

Every card text built here is plain words: no exception names, no step keys, no paths.
The technical detail goes to the system log and the application's `error.json`.
"""

import contextlib
import errno
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import workflow
from .runtime import state_root

# Each step of a preparation as the owner reads it: what Rove was doing, what it could not do.
PHASES = {
    "forum": ("opening its thread in Discord", "open its thread in Discord"),
    "start": ("starting it", "start it"),
    "open": ("opening the posting", "open the posting"),
    "blocked_retry": ("opening the posting again", "open the posting"),
    "sign_in": ("signing in", "sign in"),
    "account_creation": ("creating the account", "create the account"),
    "job_fit_review": ("checking the fit", "check the fit"),
    "resume": ("preparing your resume", "prepare your resume"),
    "prepare": ("filling the form", "fill the form"),
    "answer_drafting": ("writing answers", "write the answers"),
    "follow_application_link": ("opening the application form", "open the application form"),
    "hold": ("finishing this pass", "finish this pass"),
    "interrupted": ("preparing it", "prepare it"),
}
# Steps a second try can never harm: nothing was sent, and no click there creates
# anything on the site. An account creation may have reached the site, so it is not one.
RETRIED = frozenset(PHASES) - {"account_creation", "forum", "hold"}


def doing(phase: str) -> str:
    """ "opening the posting": the step as the owner reads it."""
    return PHASES.get(phase, ("preparing it", "prepare it"))[0]


def goal(phase: str) -> str:
    """ "open the posting": what the step was for."""
    return PHASES.get(phase, ("preparing it", "prepare it"))[1]


# ---------------------------------------------------------------------------
# What went wrong, in plain words
# ---------------------------------------------------------------------------


@dataclass
class Stop:
    retry: bool  # one quiet try on the next tick before the owner hears of it
    reason: str  # the card's text, for the owner
    technical: str  # the system log's line: exception name, step key, first line


def first_line(error: BaseException) -> str:
    """The message's first line without a driver's call log, for the system log."""
    text = str(error).split("Call log:")[0]
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return workflow.clip(line, 300)


def technical(phase: str, error: BaseException) -> str:
    kind = getattr(error, "error_type", "") or type(error).__name__
    line = first_line(error)
    return f"{phase} · {kind}" + (f" · {line}" if line else "")


def browser_stops() -> dict[str, bool]:
    """The browser's own plain stops, and whether a second try may clear each one."""
    from . import live_browser as lb

    return {
        lb.BROWSER_GONE: True,
        lb.TAB_GONE: True,
        lb.BROWSER_DOWN: True,
        lb.BROWSER_UNSTEADY: True,
        lb.LOOKUP_FAILED: True,
    }


# A known failure, the reason in words, and whether a second try may clear it. Each
# pattern is read against the message and the exception's name.
KNOWN = (
    (
        re.compile(r"did not answer in time", re.IGNORECASE),
        "the recruiting browser stopped answering",
        True,
    ),
    (
        re.compile(r"Timeout \d+ ?ms exceeded", re.IGNORECASE),
        "the page did not respond in time",
        True,
    ),
    (
        re.compile(r"browser service did not start", re.IGNORECASE),
        "the browser service did not start",
        True,
    ),
    (
        re.compile(
            r"profile (version )?changed|profile bytes changed|profile versions differ",
            re.IGNORECASE,
        ),
        "your approved profile changed while I was working on it",
        True,
    ),
    (
        re.compile(r"over the model's .*budget|prompt is too long|PromptTooLong", re.IGNORECASE),
        "the posting is too long for the model to read in one go",
        False,
    ),
    (
        re.compile(r"invalid JSON|harness exit|Qwen", re.IGNORECASE),
        "the model did not give an answer I could use",
        True,
    ),
    (
        re.compile(r"database is locked|database is busy", re.IGNORECASE),
        "the local database was busy",
        True,
    ),
    (
        re.compile(
            r"Resume preparation is incomplete|Frozen resume changed|resume\.pdf", re.IGNORECASE
        ),
        "the resume file for this one went missing",
        True,
    ),
    (
        re.compile(r"tab is not open|observation changed|URL changed|link changed", re.IGNORECASE),
        "the page changed under me",
        True,
    ),
    (
        re.compile(
            r"ConnectError|ConnectTimeout|Name or service not known|nodename nor servname|"
            r"Network is unreachable|ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED",
            re.IGNORECASE,
        ),
        "the network was down",
        True,
    ),
    (re.compile(r"TimeoutError|timed out", re.IGNORECASE), "a step took too long", True),
)


def disk_full(error: BaseException) -> bool:
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return True
    return bool(
        re.search(r"No space left on device|database or disk is full", str(error), re.IGNORECASE)
    )


def classify(phase: str, error: BaseException) -> Stop:
    """Whether a failed step is tried again quietly, and the card's words if it is not.

    The browser's plain stops keep their own words. A stop that is a decision (an unsafe
    redirect, a site the owner has not let in, a pop-up code will not guess on, a value
    the site changed) goes to the owner at once. Anything else is tried once more.
    """
    from . import overlays
    from .live_browser import owner_words

    detail = str(error)
    tech = technical(phase, error)
    retryable = phase in RETRIED
    plain = owner_words(detail)
    if plain is not None:
        transient = browser_stops().get(detail.strip(), False)
        return Stop(retryable and transient, plain, tech)
    if detail.startswith("Field verification failed"):
        label = detail.partition(":")[2].strip() or "a field"
        return Stop(
            False,
            f"The site changed the value I typed for “{workflow.clip(label, 80)}”. Check it "
            "in the recruiting browser, then reply `go`.",
            tech,
        )
    if detail.startswith(overlays.HOLD_WORDS):
        return Stop(False, overlays.HOLD_WORDS, tech)
    if disk_full(error):
        return Stop(
            False,
            f"I couldn't {goal(phase)} because the disk is full. Nothing was sent. Free some "
            "space on this Mac, then reply `go`, or `park it`.",
            tech,
        )
    because, again = "", True
    names = f"{type(error).__name__} {getattr(error, 'error_type', '')}"
    for pattern, words, may_clear in KNOWN:
        if pattern.search(detail) or pattern.search(names):
            because, again = f" because {words}", may_clear
            break
    if isinstance(error, sqlite3.OperationalError) and not because:
        because = " because the local database was busy"
    tries = " after two tries" if retryable and again else ""
    return Stop(
        retryable and again,
        f"I couldn't {goal(phase)}{tries}{because}. Nothing was sent. Reply `go` to try "
        "again, or `park it`.",
        tech,
    )


# Words a card for the owner never carries: an exception's name, a step's key, a path.
CAMEL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b")
SNAKE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
PATH = re.compile(r"(?:/(?:Users|private|tmp|home)/|~/|\.py\b|\.json\b)")


def plain_text(text: str) -> bool:
    """Whether a sentence is fit for the owner: no exception or class names, no keys, no
    paths, no traceback and no driver call log."""
    text = str(text or "")
    if not text.strip() or "\n" in text.strip() or "Traceback" in text or "Call log" in text:
        return False
    if re.search(r"Error\b|Exception\b", text):
        return False
    return not (CAMEL.search(text) or SNAKE.search(text) or PATH.search(text))


# ---------------------------------------------------------------------------
# The hidden attempt counter and the worker's pass mark
# ---------------------------------------------------------------------------


def db():
    conn = workflow.db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS preparation_attempts(
        application_id TEXT NOT NULL, phase TEXT NOT NULL, failures INTEGER NOT NULL,
        last_error TEXT NOT NULL, retry_due INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, PRIMARY KEY(application_id, phase));
      CREATE TABLE IF NOT EXISTS preparation_heartbeats(
        application_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, beat_at TEXT NOT NULL);
    """)
    return conn


def failed(application_id: str, phase: str, error: str) -> int:
    """Count one more failure of this step; returns how many in a row, 1 for the first."""
    stamp = workflow.now()
    with db() as conn:
        conn.execute(
            "INSERT INTO preparation_attempts VALUES(?,?,1,?,0,?) ON CONFLICT(application_id,"
            "phase) DO UPDATE SET failures=failures+1,last_error=excluded.last_error,"
            "updated_at=excluded.updated_at",
            (application_id, phase, str(error)[:500], stamp),
        )
        return conn.execute(
            "SELECT failures FROM preparation_attempts WHERE application_id=? AND phase=?",
            (application_id, phase),
        ).fetchone()[0]


def retry_later(application_id: str, phase: str):
    """The application waits in the queue for its quiet second try, first in line."""
    with db() as conn:
        conn.execute(
            "UPDATE preparation_attempts SET retry_due=1 WHERE application_id=? AND phase=?",
            (application_id, phase),
        )


def forget(application_id: str, phase: str | None = None):
    """A card went to the owner, or a pass went through: the count starts again."""
    with db() as conn:
        if phase is None:
            conn.execute(
                "DELETE FROM preparation_attempts WHERE application_id=?", (application_id,)
            )
        else:
            conn.execute(
                "DELETE FROM preparation_attempts WHERE application_id=? AND phase=?",
                (application_id, phase),
            )


def due_retry() -> str | None:
    """The oldest queued application waiting for its quiet second try, if any may start:
    not while the approved profile does not validate, and not a feed job while the owner
    paused the feed."""
    from . import intake

    with db() as conn:
        rows = conn.execute(
            "SELECT q.id,q.source FROM preparation_attempts a JOIN application_queue q "
            "ON q.id=a.application_id WHERE a.retry_due=1 AND q.status='QUEUED' "
            "GROUP BY q.id ORDER BY MIN(a.updated_at)"
        ).fetchall()
    if not rows or intake.profile_gate() is None:
        return None
    settings = workflow.config()
    for row in rows:
        if intake.is_feed(row["source"]):
            with workflow.db() as conn:
                if intake.feed_paused(conn, settings, datetime.now(UTC)):
                    continue
        return row["id"]
    return None


def retrying(application_id: str):
    """The quiet second try has started."""
    with db() as conn:
        conn.execute(
            "UPDATE preparation_attempts SET retry_due=0 WHERE application_id=?", (application_id,)
        )


def pass_started(application_id: str):
    """The worker starts a pass: from now on its heartbeat says whether it is alive."""
    stamp = workflow.now()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO preparation_heartbeats VALUES(?,?,?)",
            (application_id, stamp, stamp),
        )


def pass_ended(application_id: str):
    with db() as conn:
        conn.execute("DELETE FROM preparation_heartbeats WHERE application_id=?", (application_id,))


# The browser service writes a beat at most this often per application.
BEAT_EVERY = 10.0
_beats: dict[str, float] = {}


def beat(application_id: str):
    """A sign of life from the browser service while it observes or fills this
    application. Only a pass the worker marked is bumped; never raises."""
    now = time.monotonic()
    if now - _beats.get(application_id, -BEAT_EVERY) < BEAT_EVERY:
        return
    _beats[application_id] = now
    # A missed beat costs nothing; a fill that failed because of one would.
    with contextlib.suppress(Exception), db() as conn:
        conn.execute(
            "UPDATE preparation_heartbeats SET beat_at=? WHERE application_id=?",
            (workflow.now(), application_id),
        )


def silent_passes(cutoff: datetime) -> list[str]:
    """Applications in preparation whose worker pass has not beaten since `cutoff`. Marks
    left by a pass that ended in another state are dropped."""
    with db() as conn:
        conn.execute(
            "DELETE FROM preparation_heartbeats WHERE application_id NOT IN "
            "(SELECT id FROM application_queue WHERE status='PREPARING')"
        )
        rows = conn.execute(
            "SELECT h.application_id FROM preparation_heartbeats h JOIN application_queue q "
            "ON q.id=h.application_id WHERE q.status='PREPARING' AND "
            "MAX(h.beat_at,q.updated_at)<?",
            (cutoff.isoformat(),),
        ).fetchall()
    return [row[0] for row in rows]


def marked_passes() -> set[str]:
    with db() as conn:
        return {row[0] for row in conn.execute("SELECT application_id FROM preparation_heartbeats")}


# ---------------------------------------------------------------------------
# Disk space
# ---------------------------------------------------------------------------

DISK_ALERT = "disk_space"
MIN_FREE_GB = 2.0
KEEP_SNAPSHOTS = 3


def min_free_gb(settings: dict) -> float:
    value = settings.get("min_free_disk_gb")
    if value is None or isinstance(value, bool):
        return MIN_FREE_GB
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return MIN_FREE_GB


def free_bytes() -> int:
    return shutil.disk_usage(state_root()).free


def prune_snapshots(keep: int = KEEP_SNAPSHOTS) -> int:
    """Keep the newest `keep` feed snapshots and the one the last import read; leftover
    partial downloads older than an hour go too. Returns how many files were removed."""
    root = state_root() / "jobs/sources"
    if not root.is_dir():
        return 0
    current = ""
    try:
        from .jobs import REPOSITORY, database

        conn = database()
        try:
            row = conn.execute(
                "SELECT snapshot_path FROM job_sources WHERE source=?", (REPOSITORY,)
            ).fetchone()
        finally:
            conn.close()
        current = str(row[0]) if row else ""
    except Exception:  # noqa: BLE001 -- without the record, keep the newest only
        current = ""
    snapshots = sorted(
        (p for p in root.glob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in snapshots[keep:]:
        if str(path) == current:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    hour_ago = time.time() - 3600
    for path in root.glob("download-*"):
        if path.is_file() and path.stat().st_mtime < hour_ago:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def disk_ok(settings: dict) -> bool:
    """Once per tick: prune old snapshots, then whether there is room for new work. Below
    the threshold one owner card says so; it leaves by itself once there is room again."""
    from . import alerts

    removed = 0
    try:
        removed = prune_snapshots()
    except OSError as error:
        workflow.system_line("disk", f"snapshot prune failed · {type(error).__name__}")
    if removed:
        workflow.system_line("disk", f"removed {removed} old feed snapshot file(s)")
    try:
        free = free_bytes()
    except OSError:
        return True  # unknown space is not a reason to stop
    threshold = min_free_gb(settings)
    gigabytes = free / 1e9
    ok = gigabytes >= threshold
    alerts.check(
        DISK_ALERT,
        ok,
        error=f"{gigabytes:.1f} GB free",
        after=1,
        headline="This Mac is almost out of disk space",
        text=(
            f"Only {gigabytes:.1f} GB is free, so I paused new applications. Nothing in "
            "progress was dropped, and nothing is sent twice. Free some space and I carry on "
            "by myself."
        ),
        first_line=f"disk low · {gigabytes:.1f} GB free · below {threshold:g} GB · new work paused",
        back_line=f"disk space back · {gigabytes:.1f} GB free · new work resumes",
        log_name="disk",
    )
    return ok


# ---------------------------------------------------------------------------
# The form an owner's answer is checked against
# ---------------------------------------------------------------------------

LOST_FORM = (
    "I can't find that question on the form I saved for this one. Reply `go` and I read the "
    "form again."
)


def answer_field(application_id: str, field_key: str) -> dict:
    """The field an owner's answer is for: from the last observation, else from the
    package's form state. An answer to a question neither holds is refused, in plain
    words: an answer is only ever bound to a question Rove saw on the form."""
    import json

    directory = state_root() / f"applications/{application_id}"
    for name, key in (("observation.json", "fields"), ("package.json", "form_state")):
        try:
            fields = json.loads((directory / name).read_text()).get(key) or []
        except (OSError, ValueError, AttributeError):
            continue
        field = next((f for f in fields if isinstance(f, dict) and f.get("key") == field_key), None)
        if field is not None:
            if name != "observation.json":
                workflow.system_line(
                    application_id, "answer checked against the saved package · no observation"
                )
            return field
    raise PermissionError(LOST_FORM)


def stale_since(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)
