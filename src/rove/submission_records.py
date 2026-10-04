"""Commit submission outcomes before repairing their files and readable mirrors.

The journal contains recording work only. Recovery never opens an employer page or
replays a Submit click. Callers hold submission-records.lock while committing/recovering.
"""

import json

from . import job_index, workflow
from .runtime import state_root, write_private, write_private_bytes


def description(application_id: str, status: str, evidence: dict) -> dict:
    reason = str(evidence.get("reason") or "")
    if status == "APPLIED":
        return {
            "target": "APPLIED",
            "kind": "submission_confirmed",
            "trigger": reason or "verified employer confirmation after one submit",
        }
    if status == "UNKNOWN_SUBMISSION":
        return {
            "target": status,
            "kind": "submission_unknown",
            "trigger": "submit clicked once; confirmation incomplete",
            "headline": "Submission unclear",
            "reason": "One submission was attempted but not confirmed. Do not click Submit again. "
            "Check the recruiting browser and any employer email, then tell me the outcome.",
            "commands": [
                f"reconcile {application_id} applied",
                f"reconcile {application_id} not-submitted",
            ],
        }
    if status != "NOT_SUBMITTED":
        raise ValueError("Invalid submission outcome")
    if evidence.get("reconciled"):
        return {
            "target": "QUEUED" if workflow.config().get("auto_submit") else "NEEDS_USER",
            "kind": "submission_rejected",
            "trigger": "owner verified nothing was submitted",
            "headline": "Ready to try again",
            "reason": "You confirmed nothing was sent. Reply `go` to prepare and review it again.",
            "commands": ["go", "park it"],
        }
    owner = evidence.get("owner_finishes")
    return {
        "target": "MANUAL_TAKEOVER" if owner else "NEEDS_USER",
        "kind": "submission_rejected",
        "trigger": "the site's CAPTCHA rejected the send and kept the form open"
        if owner
        else "the site rejected the form and kept it open",
        "headline": "The site's CAPTCHA rejected the send"
        if owner
        else "The site rejected the form",
        "reason": reason[:300]
        if owner
        else "The site rejected the form and kept it open; nothing was sent. " + reason[:300],
        "commands": ["applied", "park it"]
        if owner
        else [f"resume {application_id}", f"defer {application_id}"],
    }


def event(conn, application_id: str, kind: str, data: dict, stamp: str):
    conn.execute(
        "INSERT INTO application_events(application_id,kind,data,created_at) VALUES(?,?,?,?)",
        (application_id, kind, json.dumps(data), stamp),
    )


def settle_job(conn, item: dict, status: str):
    if status == "NOT_SUBMITTED":
        job_index.release_send(conn, item["id"])
    elif status == "APPLIED":
        job_index.keep_send(conn, item["id"], item["url"], job_index.form_url(item["id"]))
        job_index.approve_tenant(conn, item["url"], item["id"], "applied")


def complete_command(conn, application_id: str, evidence: dict, stamp: str, target: str):
    if not evidence.get("reconciled"):
        return
    message_id = evidence["owner_message_id"]
    changed = conn.execute(
        "UPDATE owner_commands SET status='applied' WHERE message_id=? AND application_id=? "
        "AND kind='reconcile' AND status='pending'",
        (message_id, application_id),
    ).rowcount
    if changed:
        event(
            conn,
            application_id,
            "reconcile_requested",
            {
                "kind": "reconcile",
                "outcome": evidence["outcome"],
                "owner_message_id": message_id,
            },
            stamp,
        )
    if evidence["outcome"] == "not-submitted" and target == "QUEUED":
        conn.execute(
            "INSERT OR IGNORE INTO owner_commands VALUES(?,?,?,?,?,?)",
            (
                f"{message_id}:resume",
                application_id,
                "resume",
                json.dumps({"kind": "resume", "application_id": application_id}),
                "applied",
                stamp,
            ),
        )


def commit(application_id: str, status: str, evidence: dict, expect: tuple | None) -> bool:
    evidence = {**evidence, "application_id": application_id, "status": status}
    plan = description(application_id, status, evidence)
    with workflow.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = dict(
            conn.execute(
                "SELECT * FROM application_queue WHERE id=?",
                (application_id,),
            ).fetchone()
        )
        attempt = conn.execute(
            "SELECT status FROM live_submission_attempts WHERE application_id=?",
            (application_id,),
        ).fetchone()
        if expect and (not attempt or attempt["status"] not in expect):
            return False
        workflow.guard_regression(item["status"], plan["target"])
        if evidence.get("reconciled") and item["status"] not in {
            "UNKNOWN_SUBMISSION",
            "MANUAL_TAKEOVER",
        }:
            raise PermissionError(
                "Only an unknown submission or a manual application can be reconciled"
            )
        conn.execute(
            "UPDATE live_submission_attempts SET status=? WHERE application_id=?",
            (status, application_id),
        )
        settle_job(conn, item, status)
        stamp = workflow.now()
        plan["recorded_at"] = stamp
        # A late acknowledgement must not rewind an interview/offer/rejection.
        target = item["status"] if item["status"] in workflow.POST_APPLICATION else plan["target"]
        conn.execute(
            "UPDATE application_queue SET status=?,updated_at=? WHERE id=?",
            (target, stamp, application_id),
        )
        event(conn, application_id, plan["kind"], evidence, stamp)
        event(
            conn,
            application_id,
            "lifecycle",
            {
                "from": item["status"],
                "to": target,
                "trigger": plan["trigger"],
                "detail": "receipt receipt.json"
                if status == "APPLIED"
                else str(evidence.get("reason", ""))[:1200],
            },
            stamp,
        )
        complete_command(conn, application_id, evidence, stamp, target)
        conn.execute(
            "INSERT INTO submission_outcomes(application_id,status,evidence,plan) VALUES(?,?,?,?)",
            (application_id, status, json.dumps(evidence), json.dumps(plan)),
        )
    return True


def ensure_notice(application_id: str, plan: dict):
    payload = {
        "reason": plan["reason"],
        "questions": [],
        "commands": plan["commands"],
        "headline": plan["headline"],
        "items": [],
    }
    if workflow.latest_hold(application_id) != payload:
        workflow.record(application_id, "needs_action", payload)
    with workflow.db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM owner_notices WHERE application_id=? AND channel='action' "
            "AND data=? AND delivery!='withdrawn'",
            (application_id, json.dumps(payload)),
        ).fetchone()
    if not exists:
        workflow.notice(application_id, "action", payload)
    else:
        workflow.flush_notices()


def repair_receipt(row: dict, evidence: dict):
    directory = state_root() / "applications" / row["application_id"]
    receipt = directory / "receipt.json"
    archive = directory / f"receipt-superseded-outcome-{row['id']}.json"
    # Copy before replacement. Retrying an interrupted write never archives the new
    # receipt over the old one, and no gap can leave receipt.json missing.
    expected = (json.dumps(evidence, indent=2) + "\n").encode()
    if receipt.exists() and not archive.exists() and receipt.read_bytes() != expected:
        write_private_bytes(archive, receipt.read_bytes())
    write_private(receipt, evidence)


def recover(confirm):
    with workflow.db() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM submission_outcomes ORDER BY id")]
    for row in rows:
        application_id = row["application_id"]
        evidence, plan = json.loads(row["evidence"]), json.loads(row["plan"])
        repair_receipt(row, evidence)
        item = workflow.get(application_id)
        current = item["status"]
        if row["status"] == "APPLIED" and current == "APPLIED":
            try:
                evidence["erga"] = confirm(application_id)
            except Exception as error:  # noqa: BLE001 -- employer success stands even if Erga is down
                evidence["erga"] = {"synced": False, "error": type(error).__name__}
            write_private(state_root() / "applications" / application_id / "receipt.json", evidence)
        # The external sync may take time; notifications still use the latest state.
        item = workflow.get(application_id)
        current = item["status"]
        if current not in workflow.WAITING:
            workflow.withdraw_notices(application_id)
        elif (current, item["updated_at"]) == (
            plan["target"],
            plan["recorded_at"],
        ) and "headline" in plan:
            ensure_notice(application_id, plan)
        workflow.flush_events(application_id)
        workflow.refresh_status(application_id)
        workflow.apply_tags(application_id, workflow.STATE_TAGS[current])
        workflow.sync_note(application_id)
        note = workflow.system_note(plan["kind"], evidence)
        if note:
            workflow.system_line(application_id, note)
        with workflow.db() as conn:
            conn.execute("DELETE FROM submission_outcomes WHERE id=?", (row["id"],))
