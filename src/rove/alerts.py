"""Owner cards about Rove itself rather than one application: the job feed stopped, mail
tracking stopped, the profile note needs approval.

Each concern has a key and at most one live card. Raising a concern that already has a
card changes nothing, so a failure repeated every tick is one card, not one per tick; the
card leaves the channel when the concern clears. A streak counter per check decides when
a repeated failure is worth the owner's attention. Rows are written before Discord is
called, so an outage delays a card and never loses or repeats it.
"""

import hashlib
import json

import httpx

from . import workflow

CHANNELS = {"action": "action_channel_id", "recruiting": "recruiting_channel_id"}
LIVE = ("pending", "sent", "skipped")


def db():
    conn = workflow.db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS owner_alerts(
        key TEXT PRIMARY KEY, channel TEXT NOT NULL, data TEXT NOT NULL,
        delivery TEXT NOT NULL, message_id TEXT, raised_at TEXT NOT NULL, cleared_at TEXT);
      CREATE TABLE IF NOT EXISTS health_streaks(
        key TEXT PRIMARY KEY, failures INTEGER NOT NULL, since TEXT NOT NULL,
        last_error TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    return conn


# --- streaks ------------------------------------------------------------------


def failed(key: str, error: str) -> int:
    """Count one more failure of a check in a row; returns the streak, 1 for the first.
    `error` is a short technical word for the system log, never a secret."""
    stamp = workflow.now()
    with db() as conn:
        conn.execute(
            "INSERT INTO health_streaks VALUES(?,1,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "failures=failures+1,last_error=excluded.last_error,updated_at=excluded.updated_at",
            (key, stamp, str(error)[:300], stamp),
        )
        return conn.execute("SELECT failures FROM health_streaks WHERE key=?", (key,)).fetchone()[0]


def recovered(key: str) -> int:
    """The check passed: end its streak. Returns how many failures in a row it ended."""
    with db() as conn:
        row = conn.execute("SELECT failures FROM health_streaks WHERE key=?", (key,)).fetchone()
        if row:
            conn.execute("DELETE FROM health_streaks WHERE key=?", (key,))
    return row[0] if row else 0


def streak(key: str) -> int:
    with db() as conn:
        row = conn.execute("SELECT failures FROM health_streaks WHERE key=?", (key,)).fetchone()
    return row[0] if row else 0


# --- cards --------------------------------------------------------------------


def live(key: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT delivery FROM owner_alerts WHERE key=?", (key,)).fetchone()
    return bool(row) and row["delivery"] in LIVE


def raise_card(key: str, channel: str, headline: str, text: str) -> bool:
    """One card in an owner channel for this concern. True when it is new; a concern that
    already has a live card is left alone."""
    if channel not in CHANNELS:
        raise ValueError("Unknown alert channel")
    with db() as conn:
        row = conn.execute("SELECT delivery FROM owner_alerts WHERE key=?", (key,)).fetchone()
        if row and row["delivery"] in LIVE:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO owner_alerts(key,channel,data,delivery,message_id,raised_at,"
            "cleared_at) VALUES(?,?,?,'pending',NULL,?,NULL)",
            (key, channel, json.dumps({"headline": headline, "text": text}), workflow.now()),
        )
    flush()
    return True


def clear(key: str) -> bool:
    """Take the concern's card out of its channel. True when one was live."""
    with db() as conn:
        row = conn.execute("SELECT * FROM owner_alerts WHERE key=?", (key,)).fetchone()
    if not row or row["delivery"] not in LIVE:
        return False
    settings = workflow.config()
    channel = settings.get(CHANNELS.get(row["channel"], ""))
    if row["delivery"] == "sent" and row["message_id"] and channel and settings.get("enabled"):
        try:
            workflow.discord("DELETE", f"/channels/{channel}/messages/{row['message_id']}")
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("alert-withdraw", key, error)
    with db() as conn:
        conn.execute(
            "UPDATE owner_alerts SET delivery='withdrawn',cleared_at=? WHERE key=?",
            (workflow.now(), key),
        )
    return True


def card(data: dict) -> dict:
    return workflow.embed(data["headline"], data["text"], color="problem")


def flush():
    """Deliver every card still waiting for Discord; retried by any later call."""
    settings = workflow.config()
    if not settings.get("enabled"):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM owner_alerts WHERE delivery='pending' ORDER BY raised_at"
        ).fetchall()
    for row in rows:
        channel = settings.get(CHANNELS[row["channel"]])
        if not channel:
            with db() as conn:
                conn.execute(
                    "UPDATE owner_alerts SET delivery='skipped' WHERE key=?", (row["key"],)
                )
            continue
        nonce = "alert:" + hashlib.sha256(f"{row['key']}{row['raised_at']}".encode()).hexdigest()
        try:
            sent = workflow.discord(
                "POST",
                f"/channels/{channel}/messages",
                {
                    "embeds": [card(json.loads(row["data"]))],
                    "allowed_mentions": {"parse": []},
                    "nonce": nonce[:25],
                    "enforce_nonce": True,
                },
            )
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("alert", row["key"], error)
            return
        with db() as conn:
            conn.execute(
                "UPDATE owner_alerts SET delivery='sent',message_id=? WHERE key=? "
                "AND delivery='pending'",
                (str(sent.get("id", "")), row["key"]),
            )


def check(
    key: str,
    ok: bool,
    *,
    error: str = "",
    after: int = 3,
    channel: str = "action",
    headline: str = "",
    text: str = "",
    first_line: str = "",
    card_line: str = "",
    back_line: str = "",
    log_name: str = "health",
    quiet_until_card: bool = False,
) -> int:
    """One run of a recurring check: count the streak, say it once in the system log,
    raise the owner's card after `after` failures in a row, withdraw it on recovery.

    `first_line` goes to the system log on the first failure of a streak, `card_line`
    when the card is raised, `back_line` when the streak ends: always, or with
    `quiet_until_card` only when a card was up. Returns the current streak (0 when the
    check passed).
    """
    if ok:
        ended = recovered(key)
        had_card = clear(key)
        if back_line and (had_card or (ended and not quiet_until_card)):
            workflow.system_line(log_name, back_line)
        flush()
        return 0
    count = failed(key, error)
    if count == 1 and first_line:
        workflow.system_line(log_name, first_line)
    if count >= after and raise_card(key, channel, headline, text) and card_line:
        workflow.system_line(log_name, card_line)
    flush()
    return count
