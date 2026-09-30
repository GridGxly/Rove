"""Single-application local worker and authenticated Discord review commands."""

import fcntl
import json
import re
from datetime import UTC, datetime, timedelta

from . import workflow
from .discord_feed import discord, private_env
from .live_browser import browser_call
from .resumes import prepare_resume
from .runtime import state_root, write_private

INTERRUPTED_AFTER = timedelta(minutes=15)


class PhaseError(Exception):
    """Carries the workflow phase that failed so owner-facing text names it."""

    def __init__(self, phase: str, error: Exception):
        super().__init__(f"{phase}: {type(error).__name__}")
        self.phase = phase
        self.error = error


def fit_hold_reason(application_id: str, fit: dict) -> str:
    lines = [f"Job-fit review says **{fit['decision']}**: {fit['rationale'][:700]}"]
    for item in fit.get("requirements", []):
        if item["status"] != "satisfied":
            lines.append(
                f"• {item['status']} · {item['requirement'][:160]}"
                + (f" ({item['note']})" if item.get("note") else "")
            )
    for unknown in fit.get("unknowns", []):
        lines.append("• unknown · " + unknown[:200])
    lines.append(
        f"Reply `proceed {application_id}` to prepare it anyway, or `defer {application_id}` to park it."
    )
    return "\n".join(lines)


def process(application_id: str) -> dict:
    item = workflow.get(application_id)
    workflow.ensure_forum(application_id)
    workflow.set_state(application_id, "PREPARING", error=None)
    phase = "open"
    final_state = "NEEDS_USER"
    try:
        page = browser_call("open", url=item["url"])
        for _ in range(6):
            if page.get("title", "").strip().lower() == "access denied":
                reason = "The employer denied browser access. No application was submitted. The visible page is available for manual inspection."
                workflow.set_state(application_id, "MANUAL_TAKEOVER")
                workflow.action_needed(application_id, reason)
                return {
                    "application_id": application_id,
                    "status": "MANUAL_TAKEOVER",
                    "reason": reason,
                    "submitted": False,
                }
            if page.get("manual_takeover_required"):
                reason = (
                    "Login or identity verification needs you in the visible browser. Complete it, then reply `resume "
                    + application_id
                    + "`."
                )
                break
            if page.get("fields") and any(
                f["kind"] == "file"
                or "first name" in f["label"].lower()
                or "full name" in f["label"].lower()
                for f in page["fields"]
            ):
                from .reasoning import review_application, review_job

                phase = "job_fit_review"
                fit = review_job(application_id, page)
                if fit["decision"] != "fit" and not workflow.owner_override(
                    application_id, "proceed"
                ):
                    reason = fit_hold_reason(application_id, fit)
                    workflow.shortlist(application_id, reason)
                    break
                phase = "resume"
                directory = state_root() / "applications" / application_id
                if not (directory / "resume-manifest.json").exists():
                    workflow.record(
                        application_id,
                        "resume_preparation_started",
                        {"source": "approved Erga evidence", "job_url": item["url"]},
                    )
                    workflow.flush_events(application_id)
                    resume = prepare_resume(application_id, item["url"])
                    if not resume["ready"]:
                        reason = (
                            resume["reason"] + ". Resume preparation needs review before upload."
                        )
                        break
                    workflow.record(
                        application_id,
                        "resume_prepared",
                        {
                            "sha256": resume["resume_sha256"],
                            "tailored": resume.get("tailored", False),
                            "warning": resume.get("warning", ""),
                            "review": "Review the exact PDF before approving submission.",
                        },
                    )
                phase = "prepare"
                page = browser_call("prepare", run_id=application_id)
                pending = page.get("pending", [])
                if pending:
                    phase = "answer_drafting"
                    proposals = review_application(application_id, page)
                    page["qwen_review"] = proposals
                    reason = "\n".join(
                        f"• {f['label']} (`{f.get('key', '')}`)" for f in pending[:10]
                    )
                    reason += f"\nQwen's drafts and exact `use` approval commands are in the application forum. For missing facts reply `answer {application_id} FIELD_KEY = your answer`; `skip` is only for optional fields. Then `resume {application_id}`. To park this application and process another, reply `defer {application_id}`."
                elif page.get("package_hash"):
                    final_state = "READY_FOR_REVIEW"
                    reason = (
                        f"Ready for your review. Check the visible form and the exact resume; the forum lists every value. "
                        f"Package `{page['package_hash']}`. To send it once, reply `submit {application_id} {page['package_hash']}`. "
                        f"To change an answer first, reply `answer {application_id} FIELD_KEY = value` then `resume {application_id}`."
                    )
                else:
                    reason = page.get("reason", page.get("status", "Browser requires review"))
                break
            links = page.get("application_links", [])
            if not links:
                reason = (
                    "No supported application-start control is visible. The page is open for your inspection; use `resume "
                    + application_id
                    + "` after reaching the form."
                )
                break
            phase = "follow_application_link"
            page = browser_call(
                "follow",
                run_id=application_id,
                observation_id=page["observation_id"],
                ref=links[0]["ref"],
            )
        else:
            reason = "Application navigation reached its bounded step limit. Inspect the visible page before resuming."
    except Exception as error:
        raise PhaseError(phase, error) from error
    result = {"application_id": application_id, "page": page, "reason": reason, "submitted": False}
    workflow.save_result(application_id, result)
    workflow.set_state(application_id, final_state)
    workflow.action_needed(application_id, reason)
    return {"application_id": application_id, "status": final_state, "reason": reason}


def parse_command(message: dict, owner: str, channel: str, allowed: set[str]) -> dict | None:
    if (
        channel not in allowed
        or message.get("author", {}).get("id") != owner
        or message.get("author", {}).get("bot")
    ):
        return None
    text = message.get("content", "").strip()
    match = re.fullmatch(r"(resume|defer|proceed) ([a-f0-9]{12})", text, re.IGNORECASE)
    if match:
        return {"kind": match[1].lower(), "application_id": match[2].lower()}
    match = re.fullmatch(r"submit ([a-f0-9]{12}) ([a-f0-9]{64})", text, re.IGNORECASE)
    if match:
        return {
            "kind": "submit",
            "application_id": match[1].lower(),
            "package_hash": match[2].lower(),
        }
    match = re.fullmatch(r"reconcile ([a-f0-9]{12}) (applied|not-submitted)", text, re.IGNORECASE)
    if match:
        return {
            "kind": "reconcile",
            "application_id": match[1].lower(),
            "outcome": match[2].lower(),
        }
    match = re.fullmatch(r"use ([a-f0-9]{12}) ([a-f0-9]{12}) ([a-f0-9]{64})", text, re.IGNORECASE)
    if match:
        return {
            "kind": "use",
            "application_id": match[1].lower(),
            "field_key": match[2].lower(),
            "proposal_hash": match[3].lower(),
        }
    match = re.fullmatch(
        r"answer ([a-f0-9]{12}) ([a-f0-9]{12})\s*=\s*(.{1,6000})", text, re.IGNORECASE | re.DOTALL
    )
    if match:
        return {
            "kind": "answer",
            "application_id": match[1].lower(),
            "field_key": match[2].lower(),
            "value": match[3].strip(),
        }
    return None


def apply_command(command: dict, message_id: str):
    application_id = command["application_id"]
    item = workflow.get(application_id)
    with workflow.db() as conn:
        if conn.execute(
            "SELECT 1 FROM owner_commands WHERE message_id=?", (message_id,)
        ).fetchone():
            return
        if command["kind"] == "reconcile":
            if item["status"] != "UNKNOWN_SUBMISSION":
                raise PermissionError("Only an unknown submission can be reconciled")
        elif item["status"] in {"APPLIED", "SUBMITTING", "UNKNOWN_SUBMISSION"}:
            raise PermissionError("This application cannot be prepared again")
        if command["kind"] == "submit" and (
            item["status"] != "READY_FOR_REVIEW" or item["package_hash"] != command["package_hash"]
        ):
            raise PermissionError(
                "Submission needs the exact package hash of a reviewed, ready application"
            )
        if command["kind"] == "proceed" and item["status"] != "NEEDS_USER":
            raise PermissionError("Only a held application can be told to proceed")
        if command["kind"] == "use":
            from .onboarding import read_approved

            proposal_path = state_root() / f"applications/{application_id}/answer-proposals.json"
            proposals = json.loads(proposal_path.read_text())
            if proposals["profile_hash"] != read_approved()["profile_hash"]:
                raise PermissionError("Profile changed; regenerate the answer proposal")
            answer = next(
                (
                    a
                    for a in proposals["answers"]
                    if a["key"] == command["field_key"]
                    and a["kind"] == "proposal"
                    and a["proposal_hash"] == command["proposal_hash"]
                ),
                None,
            )
            if answer is None:
                raise PermissionError(
                    "Draft changed or is not an answer proposal; review the current draft"
                )
            command = {**command, "value": answer["value"]}
        if command["kind"] in {"answer", "use"}:
            observation_path = state_root() / f"applications/{application_id}/observation.json"
            observation = json.loads(observation_path.read_text())
            field = next(
                (f for f in observation["fields"] if f["key"] == command["field_key"]), None
            )
            if (
                field is None
                or field["kind"] in {"password", "file", "hidden"}
                or re.search(
                    r"social security|passport|bank account|verification code|driver.?s license",
                    field["label"],
                    re.IGNORECASE,
                )
            ):
                raise PermissionError("Unknown or manual-only question")
            if command["value"].lower() == "skip" and field["required"]:
                raise ValueError("Required questions cannot be skipped")
            conn.execute(
                "INSERT OR REPLACE INTO application_answers VALUES(?,?,?,?)",
                (application_id, field["key"], command["value"], message_id),
            )
        conn.execute(
            "INSERT INTO owner_commands VALUES(?,?,?,?,?,?)",
            (
                message_id,
                application_id,
                command["kind"],
                json.dumps(command),
                "applied",
                workflow.now(),
            ),
        )
    workflow.record(
        application_id,
        "owner_answer" if command["kind"] in {"answer", "use"} else command["kind"] + "_requested",
        {k: v for k, v in command.items() if k != "application_id"},
    )
    workflow.flush_events(application_id)
    if command["kind"] in {"resume", "proceed"}:
        workflow.set_state(application_id, "QUEUED")
    elif command["kind"] == "defer":
        workflow.set_state(application_id, "DEFERRED")
    elif command["kind"] == "reconcile":
        from .submission import reconcile

        reconcile(application_id, command["outcome"], message_id)


def poll_commands():
    settings = workflow.config()
    owner = (
        private_env().get("DISCORD_OWNER_USER_ID")
        or private_env().get("DISCORD_ALLOWED_USERS", "").split(",")[0]
    )
    if not owner:
        raise ValueError("Owner numeric Discord ID is not configured")
    with workflow.db() as conn:
        threads = {
            r[0]
            for r in conn.execute(
                "SELECT thread_id FROM application_queue WHERE thread_id IS NOT NULL AND status NOT IN ('APPLIED','SUBMITTING')"
            )
        }
    channels = {settings.get("control_channel_id"), settings.get("action_channel_id"), *threads} - {
        None
    }
    for channel in channels:
        with workflow.db() as conn:
            checkpoint = conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id=?", (channel,)
            ).fetchone()
        # Read-only bootstrap establishes a cursor. Do not replay old commands.
        route = f"/channels/{channel}/messages?limit=100"
        if checkpoint:
            route += "&after=" + checkpoint[0]
        messages = discord("GET", route)
        for message in sorted(messages, key=lambda m: int(m["id"])):
            if checkpoint:
                command = parse_command(message, owner, channel, channels)
                if command:
                    try:
                        if (
                            channel in threads
                            and workflow.get(command["application_id"])["thread_id"] != channel
                        ):
                            raise PermissionError(
                                "This command belongs to another application's forum"
                            )
                        apply_command(command, message["id"])
                    except (ValueError, PermissionError) as error:
                        discord(
                            "POST",
                            f"/channels/{channel}/messages",
                            {"content": str(error), "allowed_mentions": {"parse": []}},
                        )
        if messages or not checkpoint:
            # An empty channel also needs a cursor, otherwise its first command
            # would be discarded on the next poll as bootstrap history.
            empty_cursor = str((int(datetime.now(UTC).timestamp() * 1000) - 1420070400000) << 22)
            maximum = max(messages, key=lambda m: int(m["id"]))["id"] if messages else empty_cursor
            with workflow.db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_checkpoints VALUES(?,?)", (channel, maximum)
                )


def recover_interrupted():
    """The worker holds the only lock, so an old PREPARING row is a crashed run."""
    cutoff = (datetime.now(UTC) - INTERRUPTED_AFTER).isoformat()
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT id FROM application_queue WHERE status='PREPARING' AND updated_at<?", (cutoff,)
        ).fetchall()
    for row in rows:
        workflow.set_state(row[0], "NEEDS_USER", error="preparation_interrupted")
        workflow.action_needed(
            row[0],
            "Preparation was interrupted before it finished. Nothing was submitted. Inspect the "
            f"visible browser, then reply `resume {row[0]}` to prepare again.",
        )


def run_approved_submissions() -> list[dict]:
    """Execute owner-approved submissions, one exact package each, never twice."""
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT c.message_id,c.application_id,c.payload FROM owner_commands c JOIN application_queue q "
            "ON q.id=c.application_id WHERE c.kind='submit' AND c.status='applied' "
            "AND q.status='READY_FOR_REVIEW' ORDER BY c.created_at"
        ).fetchall()
    results = []
    for row in rows:
        package_hash = json.loads(row["payload"])["package_hash"]
        application_id = row["application_id"]
        try:
            result = browser_call(
                "submit",
                run_id=application_id,
                package_hash=package_hash,
                owner_message_id=row["message_id"],
            )
            outcome = "executed"
        except Exception as error:  # noqa: BLE001 -- persist the failure before yielding
            result = {"application_id": application_id, "error": str(error)[:800]}
            outcome = "failed"
            if workflow.get(application_id)["status"] == "READY_FOR_REVIEW":
                workflow.action_needed(
                    application_id,
                    "Submission was not attempted: "
                    + str(error)[:600]
                    + f". Nothing was sent. Reply `resume {application_id}` to prepare and review "
                    "the form again, then approve the new package hash.",
                )
        with workflow.db() as conn:
            conn.execute(
                "UPDATE owner_commands SET status=? WHERE message_id=?",
                (outcome, row["message_id"]),
            )
        write_private(state_root() / f"applications/{application_id}/submit-result.json", result)
        results.append({**result, "outcome": outcome})
    return results


def next_queued(max_waiting: int):
    with workflow.db() as conn:
        # An explicit owner resume/proceed is processed even while other
        # applications wait; otherwise the queue holds until the owner answers.
        resumed = conn.execute(
            "SELECT q.id FROM application_queue q JOIN owner_commands c ON c.application_id=q.id "
            "WHERE q.status='QUEUED' AND c.kind IN ('resume','proceed') AND c.status='applied' "
            "ORDER BY c.created_at DESC LIMIT 1"
        ).fetchone()
        if resumed:
            return resumed[0]
        waiting = conn.execute(
            "SELECT COUNT(*) FROM application_queue WHERE status IN ('NEEDS_USER','READY_FOR_REVIEW')"
        ).fetchone()[0]
        if waiting >= max_waiting:
            return None
        queued = conn.execute(
            "SELECT id FROM application_queue WHERE status='QUEUED' ORDER BY CASE source WHEN 'owner_link' THEN 0 ELSE 1 END,created_at LIMIT 1"
        ).fetchone()
    return queued[0] if queued else None


def tick() -> dict:
    settings = workflow.config()
    if not settings.get("enabled"):
        return {"enabled": False}
    with open(state_root() / "workflow.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"already_running": True}
        poll_commands()
        recover_interrupted()
        submitted = run_approved_submissions()
        if submitted:
            write_private(state_root() / "workflow-status.json", {"submissions": submitted})
            return {"submissions": submitted}
        with workflow.db() as conn:
            # A live or uncertain submission, or an in-flight preparation, holds everything.
            active = conn.execute(
                "SELECT id,status FROM application_queue WHERE status IN ('PREPARING','SUBMITTING','UNKNOWN_SUBMISSION') ORDER BY created_at LIMIT 1"
            ).fetchone()
        if active:
            return {"waiting_on": dict(active)}
        queued = next_queued(int(settings.get("max_waiting_applications", 1)))
        if not queued:
            return {"idle": True}
        try:
            result = process(queued)
        except PhaseError as failure:
            workflow.set_state(queued, "NEEDS_USER", error=str(failure))
            write_private(
                state_root() / f"applications/{queued}/error.json",
                {
                    "phase": failure.phase,
                    "type": type(failure.error).__name__,
                    "detail": str(failure.error)[:1500],
                },
            )
            workflow.action_needed(
                queued,
                f"Preparation stopped during {failure.phase.replace('_', ' ')}: "
                + type(failure.error).__name__
                + f". Private details saved locally; no application was submitted. Reply `resume {queued}` to retry.",
            )
            result = {
                "application_id": queued,
                "status": "NEEDS_USER",
                "error": str(failure),
            }
        write_private(state_root() / "workflow-status.json", result)
        return result
