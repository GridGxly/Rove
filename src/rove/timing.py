"""Wall time and counts per application stage, kept in the state database.

Measurement only. Nothing here decides, gates or changes an application, and no failure
of the timing table (missing, locked, a bad fact) reaches the caller. Facts are counts and
short words chosen by code: never a label, an answer, a URL, or text from a page or model.

Three shapes:

- `stage(application_id, name)` times a block or, as a decorator, a function. One row.
- `lap(name)` splits the innermost stage into consecutive parts without a block. A lap
  ends at the next lap, at a stage that starts inside it, or with its stage, and its row
  is written when that stage ends.
- `call(name)` decorates a leaf the stages spend their time in: the model, a browser
  round trip, a page observation, a Discord request. One row per call, and every open
  stage also counts it as `<name>_calls` and `<name>_seconds`.

Rows belong to an application. A call made outside any stage, with no application, is
kept only in the browser service, whose requests always act for one.
"""

import contextlib
import contextvars
import functools
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

from .runtime import state_root

# Leaf calls measured inside stages; `rove bench report` lists them apart from the stages.
CALLS = ("model", "browser", "observe", "discord")
# A timing write waits this long for a busy database, then gives the row up.
BUSY_SECONDS = 0.2
# After a failed write the table is left alone this long, so a stuck database costs the
# caller one short wait rather than one per row.
RETRY_SECONDS = 30.0
KEEP_DAYS = 30
PRUNE_EVERY_SECONDS = 6 * 3600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_timings(
  id INTEGER PRIMARY KEY, application_id TEXT NOT NULL, stage TEXT NOT NULL,
  parent TEXT NOT NULL, started_at TEXT NOT NULL, seconds REAL NOT NULL,
  ok INTEGER NOT NULL, facts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS stage_timings_application ON stage_timings(application_id, id);
"""

_frames: contextvars.ContextVar[tuple] = contextvars.ContextVar("rove_timing", default=())
_state = {"quiet_until": 0.0, "prune_after": 0.0}
_WORD = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


class Frame:
    """One open stage or lap."""

    def __init__(self, application_id: str, name: str, parent: str, facts: dict):
        self.application_id = application_id
        self.name = name
        self.parent = parent
        self.started_at = datetime.now(UTC).isoformat()
        self.clock = time.perf_counter()
        self.facts = dict(facts)
        self.lap: Frame | None = None
        self.laps: list[tuple] = []  # finished laps, written when the stage ends


def enabled() -> bool:
    """On unless the private workflow config says `"timing": false`."""
    try:
        from . import workflow

        return bool(workflow.config().get("timing", True))
    except Exception:  # noqa: BLE001 -- an unreadable config never stops the work
        return True


def _browser_service() -> bool:
    return sys.argv[1:3] == ["browser", "serve"]


def connect(timeout: float = BUSY_SECONDS) -> sqlite3.Connection:
    """The state database for measurement: a busy database is given up on, not waited for."""
    path = state_root() / "recruiting.sqlite3"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    return sqlite3.connect(path, timeout=timeout)


def _clean(facts: dict) -> dict:
    """Numbers, booleans and single short words only; anything else is dropped."""
    kept = {}
    for key, value in facts.items():
        if isinstance(value, bool | int):
            kept[str(key)] = value
        elif isinstance(value, float):
            kept[str(key)] = round(value, 3)
        elif isinstance(value, str) and _WORD.fullmatch(value):
            kept[str(key)] = value
    return kept


def _insert(application_id, name, parent, started_at, seconds, ok, facts):
    try:
        if not application_id and not _browser_service():
            return
        if time.monotonic() < _state["quiet_until"] or not enabled():
            return
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            with conn:
                conn.execute(
                    "INSERT INTO stage_timings"
                    "(application_id,stage,parent,started_at,seconds,ok,facts) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        str(application_id or ""),
                        str(name),
                        str(parent or ""),
                        started_at,
                        round(max(float(seconds), 0.0), 4),
                        1 if ok else 0,
                        json.dumps(_clean(facts), sort_keys=True),
                    ),
                )
                if time.monotonic() >= _state["prune_after"]:
                    cutoff = (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()
                    conn.execute("DELETE FROM stage_timings WHERE started_at<?", (cutoff,))
                    _state["prune_after"] = time.monotonic() + PRUNE_EVERY_SECONDS
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- a measurement never breaks the work it measures
        _state["quiet_until"] = time.monotonic() + RETRY_SECONDS


def _end_lap(frame: Frame, ok: bool = True):
    """Close the open lap in memory. Its row waits for the stage to end, so a `lap()` in
    the middle of the work (between a click and the wait for its result) does no I/O."""
    lap_frame, frame.lap = frame.lap, None
    if lap_frame is not None:
        frame.laps.append(
            (
                lap_frame.application_id,
                lap_frame.name,
                lap_frame.parent,
                lap_frame.started_at,
                time.perf_counter() - lap_frame.clock,
                ok,
                lap_frame.facts,
            )
        )


def _application(given, names: list, args: tuple, kwargs: dict) -> str:
    """The application a decorated call acts for, from its own arguments."""
    with contextlib.suppress(Exception):  # an unknown application is an empty one
        if callable(given):
            return str(given(*args, **kwargs) or "")
        if given:
            return str(given)
        for key in ("application_id", "run_id"):
            if kwargs.get(key):
                return str(kwargs[key])
            if key in names and names.index(key) < len(args):
                return str(args[names.index(key)] or "")
    return ""


# Lower case because it reads as a function at every use: `with stage(...)`, `@stage(...)`.
class stage:
    """Time a block (`with stage(app, "drafting"):`) or a function (`@stage(None, "pass")`).

    As a decorator the application is found on each call: the id given here, a callable
    given the call's arguments, an argument named `application_id` or `run_id`, or else
    the enclosing stage's application.
    """

    tally = False
    result = None  # for `call`: reads facts out of what the function returned

    def __init__(self, application_id, name: str, **facts):
        self.application_id = application_id
        self.name = name
        self.facts = facts
        self.frame: Frame | None = None
        self.token = None

    def __enter__(self):
        with contextlib.suppress(Exception):
            frames = _frames.get()
            outer = frames[-1] if frames else None
            application_id = self.application_id if isinstance(self.application_id, str) else ""
            parent = ""
            if outer is not None:
                application_id = application_id or outer.application_id
                if self.tally:
                    parent = (outer.lap or outer).name
                else:
                    _end_lap(outer)
                    parent = outer.name
            self.frame = Frame(application_id, self.name, parent, self.facts)
            self.token = _frames.set((*frames, self.frame))
        return self

    def __exit__(self, kind, error, trace):
        with contextlib.suppress(Exception):
            frame, self.frame = self.frame, None
            if frame is not None:
                seconds = time.perf_counter() - frame.clock
                ok = kind is None
                _end_lap(frame, ok)
                try:
                    _frames.reset(self.token)
                except (ValueError, RuntimeError):
                    _frames.set(tuple(f for f in _frames.get() if f is not frame))
                if self.tally:
                    for outer in _frames.get():
                        for target in (outer, outer.lap):
                            if target is not None:
                                _add(target.facts, self.name, seconds)
                for finished in frame.laps:
                    _insert(*finished)
                _insert(
                    frame.application_id,
                    frame.name,
                    frame.parent,
                    frame.started_at,
                    seconds,
                    ok,
                    frame.facts,
                )
        return False

    def __call__(self, function):
        try:
            names = list(inspect.signature(function).parameters)
        except (TypeError, ValueError):
            names = []

        @functools.wraps(function)
        def timed(*args, **kwargs):
            timer = stage(
                _application(self.application_id, names, args, kwargs), self.name, **self.facts
            )
            timer.tally = self.tally
            with timer:
                value = function(*args, **kwargs)
                if self.result is not None and timer.frame is not None:
                    with contextlib.suppress(Exception):
                        timer.frame.facts.update(self.result(value))
                return value

        return timed


def _add(facts: dict, name: str, seconds: float):
    facts[name + "_calls"] = int(facts.get(name + "_calls", 0)) + 1
    facts[name + "_seconds"] = round(float(facts.get(name + "_seconds", 0.0)) + seconds, 3)


def call(name: str, application=None, result=None) -> stage:
    """Decorator for a leaf call the stages spend their time in; see the module docstring.

    `application` is a callable given the call's arguments when the application is not one
    of them (a method that keeps it on `self`). `result` is a callable given what the
    call returned; the facts it gives go on the call's own row.
    """
    timer = stage(application, name)
    timer.tally = True
    timer.result = result
    return timer


def tokens(generated) -> dict:
    """Token counts from a model result, when the local server reported usage.

    Reads the server's own names (`prompt_tokens`, `completion_tokens`,
    `prompt_tokens_details.cached_tokens`) or the harness's totals (`input_tokens`,
    `output_tokens`, `cache_read_tokens`), under `usage` or on the result itself. A
    count that is absent is left out; nothing is estimated.
    """
    result = generated.get("result") if isinstance(generated, dict) else None
    places = []
    for holder in (result, generated):
        if isinstance(holder, dict):
            places += [p for p in (holder.get("usage"), holder) if isinstance(p, dict)]
    details = [p.get("prompt_tokens_details") for p in places]

    def first(names: tuple, among: list):
        for place in among:
            for name in names:
                value = place.get(name) if isinstance(place, dict) else None
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
        return None

    counts = {
        "tokens_in": first(("prompt_tokens", "input_tokens"), places),
        "tokens_out": first(("completion_tokens", "output_tokens"), places),
        "tokens_cached": first(("cache_read_tokens", "cached_tokens"), [*places, *details]),
    }
    return {name: value for name, value in counts.items() if value is not None}


def lap(name: str | None, **facts):
    """End the innermost stage's open lap and, given a name, start the next one."""
    with contextlib.suppress(Exception):
        frames = _frames.get()
        if not frames:
            return
        frame = frames[-1]
        _end_lap(frame)
        if name:
            frame.lap = Frame(frame.application_id, name, frame.name, facts)


def note(**facts):
    """Add facts to the open lap, or to the innermost stage when no lap is open."""
    with contextlib.suppress(Exception):
        frames = _frames.get()
        if frames:
            (frames[-1].lap or frames[-1]).facts.update(facts)


def record(application_id: str, name: str, seconds: float, **facts):
    """One row for something measured elsewhere, or a step that was decided away."""
    _insert(application_id, name, "", datetime.now(UTC).isoformat(), seconds, True, facts)


def queue_wait(item: dict):
    """How long a queued application waited for the worker, read from its queue row."""
    with contextlib.suppress(Exception):
        if item.get("status") != "QUEUED":
            return
        waited = datetime.now(UTC) - datetime.fromisoformat(item["updated_at"])
        record(item["id"], "queue_wait", max(waited.total_seconds(), 0.0))


def rows(last: int | None = None) -> list[dict]:
    """Recorded rows, oldest first; `last` keeps the most recent N applications.

    Empty when nothing was recorded or the table cannot be read. A row the browser service
    wrote without an application belongs to the browser round trip it happened inside, so
    it is kept when one of the selected applications' round trips covers it.
    """
    try:
        if not (state_root() / "recruiting.sqlite3").exists():
            return []
        # A report can wait for a busy database; only the measured work must not.
        conn = connect(timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if last:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT application_id FROM stage_timings WHERE application_id!='' "
                        "GROUP BY application_id ORDER BY MAX(id) DESC LIMIT ?",
                        (int(last),),
                    )
                ]
                if not ids:
                    return []
                marks = ",".join("?" * len(ids))
                found = conn.execute(
                    f"SELECT * FROM stage_timings WHERE application_id IN ({marks})", ids
                ).fetchall()
                trips = [
                    (start, start + timedelta(seconds=r["seconds"]))
                    for r in found
                    if r["stage"] == "browser"
                    for start in [datetime.fromisoformat(r["started_at"])]
                ]
                if trips:
                    unowned = conn.execute(
                        "SELECT * FROM stage_timings WHERE application_id='' "
                        "AND started_at BETWEEN ? AND ?",
                        (
                            min(t[0] for t in trips).isoformat(),
                            max(t[1] for t in trips).isoformat(),
                        ),
                    ).fetchall()
                    found += [
                        r
                        for r in unowned
                        if any(
                            start <= datetime.fromisoformat(r["started_at"]) <= end
                            for start, end in trips
                        )
                    ]
            else:
                found = conn.execute("SELECT * FROM stage_timings").fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- no table, no rows
        return []
    result = []
    for row in sorted(found, key=lambda r: r["id"]):
        try:
            facts = json.loads(row["facts"])
        except ValueError:
            facts = {}
        result.append({**dict(row), "ok": bool(row["ok"]), "facts": facts})
    return result
