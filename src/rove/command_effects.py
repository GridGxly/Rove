"""Recover the local effects of an accepted owner reply before doing new work.

All command callers serialize through owner-commands.lock. Answers and this journal
are saved together. Completion commits the queue state, timeline and command status in
one transaction; Discord and Obsidian are retried afterward from that committed state.
Submission itself is never an effect here: its separate claim prevents a second send.
"""

import json

from . import questions, vault, workflow

QUEUED = {"resume", "proceed", "account"}


def stage(conn, message_id: str, event: dict, remember: tuple | None, how: dict):
    conn.execute(
        "INSERT INTO owner_command_effects VALUES(?,?)",
        (message_id, json.dumps({"event": event, "remember": remember, "how": how})),
    )


def commit_local(row: dict, effects: dict):
    application_id, kind = row["application_id"], row["kind"]
    event = effects["event"]
    if effects["remember"]:
        label, offered, given = effects["remember"]
        how = effects["how"]
        workflow.remember_answer(label, offered, given, row["message_id"], **how)
        if questions.general_no(questions.classify(label, how["kind"], offered), given):
            event = {**event, "every_company": True}
    target = "QUEUED" if kind in QUEUED else "DEFERRED" if kind == "defer" else None
    with workflow.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status FROM owner_commands WHERE message_id=?", (row["message_id"],)
        ).fetchone()[0]
        if current != "pending":
            return
        stamp = workflow.now()
        if target:
            previous = conn.execute(
                "SELECT status FROM application_queue WHERE id=?", (application_id,)
            ).fetchone()[0]
            if previous not in workflow.UNSENT:
                reject_stale(conn, row, previous)
                return
            conn.execute(
                "UPDATE application_queue SET status=?,updated_at=? WHERE id=?",
                (target, stamp, application_id),
            )
            conn.execute(
                "INSERT INTO application_events(application_id,kind,data,created_at) "
                "VALUES(?, 'lifecycle', ?, ?)",
                (
                    application_id,
                    json.dumps(
                        {
                            "from": previous,
                            "to": target,
                            "trigger": kind,
                            "detail": "",
                            "owner_message_id": row["message_id"],
                        }
                    ),
                    stamp,
                ),
            )
        conn.execute(
            "INSERT INTO application_events(application_id,kind,data,created_at) VALUES(?,?,?,?)",
            (
                application_id,
                "owner_answer" if kind in {"answer", "use"} else kind + "_requested",
                json.dumps({**event, "owner_message_id": row["message_id"]}),
                stamp,
            ),
        )
        conn.execute(
            "UPDATE owner_commands SET status='applied' WHERE message_id=?", (row["message_id"],)
        )


def reject_stale(conn, row: dict, status: str):
    """A late reply cannot rewind a send or stop unrelated applications from running."""
    conn.execute(
        "UPDATE owner_commands SET status='failed' WHERE message_id=?", (row["message_id"],)
    )
    conn.execute(
        "INSERT INTO application_events(application_id,kind,data,created_at) VALUES(?,?,?,?)",
        (
            row["application_id"],
            "command_refused",
            json.dumps(
                {
                    "owner_message_id": row["message_id"],
                    "status": status,
                    "reason": "Application advanced before the interrupted reply completed",
                }
            ),
            workflow.now(),
        ),
    )


def recover(close_tab):
    """Finish saved replies in order; leave the journal intact if any step fails."""
    with workflow.db() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT c.*,e.effects FROM owner_commands c JOIN owner_command_effects e "
                "ON c.message_id=e.message_id ORDER BY c.created_at,c.rowid"
            )
        ]
    for row in rows:
        application_id = row["application_id"]
        effects = json.loads(row["effects"])
        if row["status"] == "pending":
            commit_local(row, effects)
        # Re-read state: retrying a notification must not undo later browser/mail work.
        status = workflow.get(application_id)["status"]
        if status not in workflow.WAITING:
            workflow.withdraw_notices(application_id)
            workflow.refresh_status(application_id)
        workflow.flush_events(application_id)
        workflow.apply_tags(application_id, workflow.STATE_TAGS[status])
        workflow.sync_note(application_id)
        if effects["remember"]:
            vault.sync_answers()
        with workflow.db() as conn:
            outcome = conn.execute(
                "SELECT status FROM owner_commands WHERE message_id=?", (row["message_id"],)
            ).fetchone()[0]
        workflow.system_line(application_id, f"owner {row['kind']} {outcome} · {status}")
        if row["kind"] == "defer" and status == "DEFERRED":
            close_tab(application_id)
        with workflow.db() as conn:
            conn.execute(
                "DELETE FROM owner_command_effects WHERE message_id=?", (row["message_id"],)
            )
