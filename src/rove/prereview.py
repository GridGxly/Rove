"""Model work done while the worker waits on its pacing, its holds or the owner.

Two things, both only when the worker has nothing it may start:

- Fit reviews of queued postings whose text is already kept, in the order the queue
  would take them, a bounded number per tick, stored under the key a pass reads
  (posting hash, profile snapshot, fit prompt version). A pass whose posting reads the
  same then skips its model call; any other text is reviewed live, as before. Nothing
  is posted to Discord here; the review reaches the thread with the first pass.
- A one-token ping that keeps the model loaded while jobs are queued
  (`model_keepalive`, on by default), so the next pass does not pay the reload.

The server answers one request at a time, so neither starts while the server reports
a request of anyone's in progress or waiting (a mail label, a pop-up question, the
Discord agent). A live pass never overlaps: it runs in this same worker, in place of
this idle work.
"""

import contextlib
import json

from . import fastpath, intake, model_client, reasoning, workflow
from .onboarding import read_approved
from .runtime import state_root, write_private

# Each review holds the worker for up to a minute; an owner reply waits that long at most.
PER_TICK = 1
# A posting whose review failed is left alone until its text, the profile or the prompt
# changes; the pass will review it live and say why if it fails again.
FAILED_FILE = "background-review-failed.json"


def queue_order() -> list[dict]:
    """Queued applications in the order `worker.next_queued` would take them, without its
    brakes: the owner's own choices first, then by score band, the newest first."""
    with workflow.db() as conn:
        intake.ensure_tables(conn)
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT q.id,q.url,q.source,q.profile_hash,q.created_at,"
                "COALESCE(s.score,0) AS score FROM application_queue q "
                "LEFT JOIN queue_scores s ON s.application_id=q.id "
                "WHERE q.status='QUEUED' ORDER BY q.created_at"
            )
        ]
    policies = {row["id"]: workflow.source_policy(row["source"]) for row in rows}
    owner = sorted(
        (row for row in rows if policies[row["id"]]["owner_decided"]),
        key=lambda row: -policies[row["id"]]["rank"],
    )
    rest = [row for row in reversed(rows) if not policies[row["id"]]["owner_decided"]]
    rest.sort(key=lambda row: -(row["score"] // intake.SCORE_BAND))
    return owner + rest


def failed_before(application_id: str, key: tuple) -> bool:
    path = state_root() / "applications" / application_id / FAILED_FILE
    try:
        return path.is_file() and tuple(json.loads(path.read_text())["key"]) == key
    except (OSError, ValueError, KeyError, TypeError):
        return False


def review_queued(limit: int = PER_TICK) -> int:
    """Review up to `limit` queued postings that have kept text and no stored review."""
    approved = read_approved()
    done = 0
    for row in queue_order():
        if done >= limit:
            break
        if row["profile_hash"] != approved["profile_hash"]:
            continue  # the queue row is rebuilt for the new profile before it is prepared
        text = reasoning.stored_posting(row["id"])
        if not text:
            continue
        key = fastpath.review_key(text, approved["profile_hash"], reasoning.FIT_PROMPT_VERSION)
        if fastpath.stored_review(row["id"], key) or failed_before(row["id"], key):
            continue
        if model_client.busy():
            break
        try:
            reasoning.prereview_job(row["id"], text)
        except model_client.ModelUnavailable:
            break
        except Exception as error:  # noqa: BLE001 -- background work never stops the tick
            write_private(
                state_root() / "applications" / row["id"] / FAILED_FILE,
                {"key": list(key), "error": type(error).__name__, "at": workflow.now()},
            )
            workflow.system_line(
                row["id"], f"background fit review failed · {type(error).__name__}"
            )
        else:
            workflow.system_line(row["id"], "fit reviewed in the background")
        done += 1
    return done


def keep_warm(settings: dict) -> bool:
    """Ping the model when jobs are queued and nothing has used it for a while."""
    if not settings.get("model_keepalive", True):
        return False
    if model_client.idle_seconds() < model_client.KEEPALIVE_SECONDS:
        return False
    with workflow.db() as conn:
        if not conn.execute(
            "SELECT 1 FROM application_queue WHERE status='QUEUED' LIMIT 1"
        ).fetchone():
            return False
    status = model_client.server_status()
    if not isinstance(status, dict):
        return False  # down or unreadable: never start the server just to keep it warm
    if int(status.get("active_requests") or 0) + int(status.get("waiting_requests") or 0):
        return False  # in use, which keeps it loaded anyway
    return model_client.ping()


def idle(settings: dict) -> dict:
    """The worker's idle tick: background reviews, then the keepalive. Never raises.

    Nothing runs until the model has served a request from this state root, so a fresh
    install (and the offline test suite) never reaches for the server from here.
    """
    done: dict = {}
    if not model_client.last_use_path().exists():
        return done
    for name, work in (("reviewed", review_queued), ("pinged", lambda: keep_warm(settings))):
        try:
            done[name] = work()
        except Exception as error:  # noqa: BLE001 -- idle work never stops the tick
            with contextlib.suppress(Exception):
                workflow.system_line("worker", f"idle {name} step failed · {type(error).__name__}")
    return done
