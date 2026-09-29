"""Single-application local worker and authenticated Discord review commands."""

import fcntl
import json
import re

from . import workflow
from .discord_feed import discord, private_env
from .live_browser import browser_call
from .resumes import prepare_resume
from .runtime import state_root, write_private


def process(application_id: str) -> dict:
    item = workflow.get(application_id)
    workflow.ensure_forum(application_id)
    workflow.set_state(application_id, "PREPARING", error=None)
    page = browser_call("open", url=item["url"])
    for _ in range(6):
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
                    reason = resume["reason"] + ". Resume preparation needs review before upload."
                    break
                workflow.record(
                    application_id,
                    "resume_prepared",
                    {
                        "sha256": resume["resume_sha256"],
                        "review": "Review the generated PDF before submission.",
                    },
                )
            page = browser_call("prepare", run_id=application_id)
            pending = page.get("pending", [])
            if pending:
                from .reasoning import review_application

                proposals = review_application(application_id, page)
                page["qwen_review"] = proposals
                reason = "\n".join(f"• {f['label']} (`{f.get('key', '')}`)" for f in pending[:10])
                reason += f"\nReply `answer {application_id} FIELD_KEY = your answer` for each question; use `skip` only for optional fields. Then `resume {application_id}`."
            elif page.get("package_hash"):
                reason = f"Review the visible form and exact resume. Package `{page['package_hash']}`. Final submission is available only after the supported form's final control and all required answers are verified."
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
        page = browser_call(
            "follow",
            run_id=application_id,
            observation_id=page["observation_id"],
            ref=links[0]["ref"],
        )
    else:
        reason = "Application navigation reached its bounded step limit. Inspect the visible page before resuming."
    result = {"application_id": application_id, "page": page, "reason": reason, "submitted": False}
    workflow.save_result(application_id, result)
    workflow.set_state(application_id, "NEEDS_USER")
    workflow.action_needed(application_id, reason)
    return {"application_id": application_id, "status": "NEEDS_USER", "reason": reason}


def parse_command(message: dict, owner: str, channel: str, allowed: set[str]) -> dict | None:
    if (
        channel not in allowed
        or message.get("author", {}).get("id") != owner
        or message.get("author", {}).get("bot")
    ):
        return None
    text = message.get("content", "").strip()
    match = re.fullmatch(r"resume ([a-f0-9]{12})", text, re.IGNORECASE)
    if match:
        return {"kind": "resume", "application_id": match[1].lower()}
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
        if item["status"] in {"APPLIED", "SUBMITTING", "UNKNOWN_SUBMISSION"}:
            raise PermissionError("This application cannot be prepared again")
        if command["kind"] == "answer":
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
        "owner_answer" if command["kind"] == "answer" else "resume_requested",
        {k: v for k, v in command.items() if k != "application_id"},
    )
    workflow.flush_events(application_id)
    if command["kind"] == "resume":
        workflow.set_state(application_id, "QUEUED")


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
                "SELECT thread_id FROM application_queue WHERE thread_id IS NOT NULL AND status='NEEDS_USER'"
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
                        apply_command(command, message["id"])
                    except (ValueError, PermissionError) as error:
                        discord(
                            "POST",
                            f"/channels/{channel}/messages",
                            {"content": str(error), "allowed_mentions": {"parse": []}},
                        )
        if messages:
            maximum = max(messages, key=lambda m: int(m["id"]))["id"]
            with workflow.db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_checkpoints VALUES(?,?)", (channel, maximum)
                )


def tick() -> dict:
    if not workflow.config().get("enabled"):
        return {"enabled": False}
    with open(state_root() / "workflow.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"already_running": True}
        poll_commands()
        with workflow.db() as conn:
            # A paused application retains the visible browser for takeover.
            active = conn.execute(
                "SELECT id,status FROM application_queue WHERE status IN ('PREPARING','NEEDS_USER','SUBMITTING','UNKNOWN_SUBMISSION') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if active:
                return {"waiting_on": dict(active)}
            queued = conn.execute(
                "SELECT id FROM application_queue WHERE status='QUEUED' ORDER BY CASE source WHEN 'owner_link' THEN 0 ELSE 1 END,created_at LIMIT 1"
            ).fetchone()
        if not queued:
            return {"idle": True}
        try:
            result = process(queued[0])
        except Exception as error:  # noqa: BLE001 -- persist worker failure before yielding
            workflow.set_state(queued[0], "NEEDS_USER", error=type(error).__name__)
            write_private(
                state_root() / f"applications/{queued[0]}/error.json",
                {"type": type(error).__name__, "detail": str(error)[:1500]},
            )
            workflow.action_needed(
                queued[0],
                "Preparation stopped: "
                + type(error).__name__
                + ". Private details saved locally; no application was submitted.",
            )
            result = {
                "application_id": queued[0],
                "status": "NEEDS_USER",
                "error": type(error).__name__,
            }
        write_private(state_root() / "workflow-status.json", result)
        return result
