"""Single-application local worker and authenticated Discord review commands."""

import contextlib
import fcntl
import json
import re
from datetime import UTC, datetime, timedelta

from . import matching, workflow
from .discord_feed import discord, private_env
from .live_browser import browser_call
from .reasoning import GATE_KINDS
from .resumes import prepare_resume
from .runtime import state_root, write_private

INTERRUPTED_AFTER = timedelta(minutes=15)


class PhaseError(Exception):
    """Carries the workflow phase that failed so owner-facing text names it."""

    def __init__(self, phase: str, error: Exception):
        super().__init__(f"{phase}: {type(error).__name__}")
        self.phase = phase
        self.error = error


def fit_hold(application_id: str, fit: dict) -> dict:
    """What blocks the job, in the owner's terms: conflicts first, unchecked eligibility next."""
    conflicts = [
        r
        for r in fit.get("requirements", [])
        if r["status"] == "conflict" and r.get("kind") in GATE_KINDS
    ]
    unchecked = [
        r
        for r in fit.get("requirements", [])
        if r["status"] == "unknown" and r.get("kind") in GATE_KINDS
    ]
    items = [f"conflict · {r['requirement'][:110]}" for r in conflicts] + [
        f"unchecked · {r['requirement'][:110]}" for r in unchecked
    ]
    if conflicts:
        summary = "Conflicts with your approved facts: " + "; ".join(
            r["requirement"][:80] for r in conflicts[:2]
        )
    elif unchecked:
        summary = "Eligibility the posting states could not be checked against your profile: " + (
            "; ".join(r["requirement"][:80] for r in unchecked[:2])
        )
    else:
        summary = "Job-fit review asks for your call."
    return {
        "summary": summary + ".",
        "items": items,
        "commands": ["go", "park it"],
    }


def held(
    application_id: str, status: str, reason: str, headline: str, channel: str = "action", **extra
) -> dict:
    """End a run in a state the owner must act on, with one clear card in one channel."""
    workflow.set_state(application_id, status)
    workflow.action_needed(application_id, reason, headline=headline, channel=channel, **extra)
    return {
        "application_id": application_id,
        "status": status,
        "reason": reason,
        "submitted": False,
    }


def use_drafts(application_id: str, proposals: dict, asked: list) -> list[str]:
    """Owner policy auto_use_drafts: Qwen's drafts become answers without a per-draft reply.

    The draft cards stay in the thread for review after the fact; a later numbered
    reply before submission still overrides. Facts only the owner knows stay questions.
    `asked` is the owner's numbered question list, so the thread line can cite the number.
    """
    labels = {q["key"]: q.get("label", "") for q in asked}
    numbers = {q["key"]: number for number, q in enumerate(asked, start=1)}
    used = []
    with workflow.db() as conn:
        for answer in proposals.get("answers", []):
            if answer.get("kind") != "proposal" or answer["key"] not in labels:
                continue
            if conn.execute(
                "SELECT 1 FROM application_answers WHERE application_id=? AND field_key=?",
                (application_id, answer["key"]),
            ).fetchone():
                continue
            conn.execute(
                "INSERT INTO application_answers VALUES(?,?,?,?)",
                (
                    application_id,
                    answer["key"],
                    answer["value"],
                    f"auto-draft:{answer['proposal_hash'][:12]}",
                ),
            )
            used.append(answer["key"])
    for key in used:
        workflow.record(
            application_id,
            "auto_draft_used",
            {"key": key, "label": labels[key], "number": numbers[key]},
        )
    if used:
        workflow.flush_events(application_id)
    return used


def question_list(asked: list, pending: list, proposals: dict, used: set) -> list[dict]:
    """The owner's numbered questions, in form order, each with how it stands.

    A question is `open` (only the owner can answer it), `drafted` (Qwen's draft waits
    in its card), or `used` (the draft became the answer under the owner's policy).
    Numbers are positions in this list; the thread's cards and the `N: value` reply
    both count the same way.
    """
    drafts: dict = {}
    for answer in proposals.get("answers", []):
        if answer.get("kind") == "proposal" and answer.get("key"):
            drafts.setdefault(answer["key"], len(drafts) + 1)
    seen = {q["key"] for q in asked if q.get("key")}
    ordered = [*asked, *[q for q in pending if q.get("key") and q["key"] not in seen]]
    questions = []
    for question in ordered:
        key = question.get("key")
        if not key:
            continue
        entry = {
            "key": key,
            "label": question.get("label", ""),
            "options": list(question.get("options") or []),
            "required": question.get("required", True),
        }
        if key in used:
            entry["state"] = "used"
        elif key in drafts:
            entry["state"] = "drafted"
        else:
            entry["state"] = "open"
        if key in drafts:
            entry["draft"] = drafts[key]
        questions.append(entry)
    return questions


def queue_auto_submit(application_id: str, package_hash: str) -> str:
    """Owner policy auto_submit: one synthetic approval for this exact package, once."""
    message_id = f"auto-submit:{application_id}:{package_hash[:12]}"
    with workflow.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO owner_commands VALUES(?,?,?,?,?,?)",
            (
                message_id,
                application_id,
                "submit",
                json.dumps(
                    {
                        "kind": "submit",
                        "application_id": application_id,
                        "package_hash": package_hash,
                    }
                ),
                "applied",
                workflow.now(),
            ),
        )
    workflow.record(application_id, "auto_submit_queued", {"package_hash": package_hash})
    workflow.flush_events(application_id)
    return message_id


def skip_optional(application_id: str, questions: list):
    """Record a blank for optional fields with no approved fact or draft; never asks."""
    with workflow.db() as conn:
        for question in questions:
            conn.execute(
                "INSERT OR IGNORE INTO application_answers VALUES(?,?,?,?)",
                (application_id, question["key"], "skip", f"auto-skip:{question['key']}"),
            )
    workflow.record(
        application_id,
        "optional_skipped",
        {"labels": [str(q.get("label") or q.get("key"))[:80] for q in questions[:12]]},
    )
    workflow.flush_events(application_id)


def process(application_id: str) -> dict:
    item = workflow.get(application_id)
    settings = workflow.config()
    (state_root() / f"applications/{application_id}/error.json").unlink(missing_ok=True)
    workflow.ensure_forum(application_id)
    workflow.set_state(application_id, "PREPARING", error=None)
    phase = "open"
    final_state = "NEEDS_USER"
    posting_text = ""
    questions: list = []
    items: list = []
    channel = "action"
    commands: list = ["go", "park it"]
    headline = "Browser needs a look"
    try:
        page = browser_call("open", url=item["url"])
        for _ in range(6):
            if not page.get("fields"):
                # The posting itself; application-form labels are never requirements.
                posting_text = page.get("text", "") or posting_text
            if page.get("blocked"):
                phase = "blocked_retry"
                workflow.record(
                    application_id,
                    "browser_access_blocked",
                    {"url": page.get("url"), "marker": page.get("block_marker"), "retry": "once"},
                )
                page = browser_call("reopen", run_id=application_id)
                if page.get("blocked"):
                    return held(
                        application_id,
                        "MANUAL_TAKEOVER",
                        "The employer's site blocked the recruiting browser twice. Nothing was "
                        "submitted. If you want this one, apply in your own browser and tell me.",
                        "Blocked by the employer's site",
                        commands=["applied", "park it"],
                    )
            if page.get("ats_markers", {}).get("captcha_challenge"):
                return held(
                    application_id,
                    "MANUAL_TAKEOVER",
                    "The site shows a CAPTCHA. Solve it in the recruiting browser, then reply "
                    "`go`; nothing was sent.",
                    "CAPTCHA needs you",
                    commands=["go", "park it"],
                )
            if page.get("ats_markers", {}).get("already_applied"):
                return held(
                    application_id,
                    "MANUAL_TAKEOVER",
                    "The site says an application from you already exists for this job. "
                    "Nothing was sent. If that is right, mark it applied; otherwise apply in "
                    "your own browser.",
                    "The site says you already applied",
                    commands=["applied", "park it"],
                )
            if page.get("closed"):
                workflow.transition(
                    application_id,
                    "DEFERRED",
                    "posting says it no longer accepts applications",
                    page.get("closed_marker") or "",
                )
                return {"application_id": application_id, "status": "DEFERRED", "submitted": False}
            if page.get("auth_page") == "login":
                from . import credentials

                if credentials.lookup(credentials.account_host(page["url"])):
                    phase = "sign_in"
                    page = browser_call("login", run_id=application_id)
                    if page.get("auth_page") == "login":
                        reason = (
                            "Signing in with the stored account did not work. Finish the "
                            "sign-in in the recruiting browser, then reply `go`."
                        )
                        headline = "Sign-in needs you"
                        break
                    continue
                reason = (
                    "This board wants an existing account and I have none stored for it. "
                    "Sign in yourself in the recruiting browser, then reply `go`."
                )
                headline = "Sign-in needs you"
                break
            if page.get("auth_page") == "register":
                if workflow.owner_override(application_id, "account"):
                    phase = "account_creation"
                    page = browser_call("register", run_id=application_id)
                    if not page.get("fields") and re.search(
                        r"verif|confirm your email|check your (email|inbox)|activate",
                        page.get("text", ""),
                        re.IGNORECASE,
                    ):
                        reason = (
                            "The account was created with your application email; the site "
                            "wants the email verified. Open the link it sent in the recruiting "
                            "browser, then reply `go`."
                        )
                        headline = "Verify the account email"
                        break
                    continue
                reason = (
                    "This board needs an account before the application. I can create one "
                    "with your application email and a generated password stored encrypted "
                    "on this Mac. Your policy asks first."
                )
                commands = ["create account", "park it"]
                headline = "Account needed"
                break
            if page.get("manual_takeover_required"):
                reason = (
                    "An identity or verification step needs you in the recruiting browser. "
                    "Finish it there, then reply `go`."
                )
                headline = "Manual step in the browser"
                break
            if page.get("fields") and any(
                f["kind"] == "file"
                or "first name" in f["label"].lower()
                or "full name" in f["label"].lower()
                for f in page["fields"]
            ):
                from .reasoning import review_application, review_job

                phase = "job_fit_review"
                fit = review_job(application_id, page, posting_text)
                workflow.system_line(application_id, f"fit · {fit['decision']}")
                # A link the owner pasted is a decision already made: never ask again.
                if (
                    fit["decision"] != "fit"
                    and item["source"] != "owner_link"
                    and not workflow.owner_override(application_id, "proceed")
                ):
                    hold = fit_hold(application_id, fit)
                    reason, commands, headline, items = (
                        hold["summary"],
                        hold["commands"],
                        "Your call on fit",
                        hold["items"],
                    )
                    channel = "shortlist"
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
                        reason = resume["reason"] + ". The resume needs review before upload."
                        headline = "Resume needs review"
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
                proposals: dict = {"answers": []}
                asked: list = []
                used: set = set()
                if pending:
                    phase = "answer_drafting"
                    proposals = review_application(application_id, page)
                    page["qwen_review"] = proposals
                    drafted_keys = {
                        a["key"] for a in proposals.get("answers", []) if a["kind"] == "proposal"
                    }
                    optional = [
                        q
                        for q in pending
                        if not q.get("required", True) and q["key"] not in drafted_keys
                    ]
                    # The owner's numbered list: what the form asked beyond approved facts,
                    # in form order, without the optional fields that are left blank.
                    asked = [q for q in pending if q not in optional]
                    if settings.get("auto_use_drafts", settings.get("auto_submit")):
                        used = set(use_drafts(application_id, proposals, asked))
                    if optional:
                        # An optional field nobody can fill from approved facts stays blank.
                        skip_optional(application_id, optional)
                    if optional or used:
                        phase = "prepare"
                        page = browser_call("prepare", run_id=application_id)
                        page["qwen_review"] = proposals
                        pending = page.get("pending", [])
                questions = question_list(asked, pending, proposals, used)
                if pending:
                    open_numbers = [
                        n for n, q in enumerate(questions, start=1) if q["state"] == "open"
                    ]
                    drafted = len([q for q in questions if q["state"] == "drafted"])
                    parts = []
                    if drafted:
                        parts.append(
                            f"{drafted} draft{'s' if drafted != 1 else ''} to approve in the "
                            "thread (each card says which `use draft` reply approves it)"
                        )
                    if open_numbers:
                        count = len(open_numbers)
                        parts.append(
                            f"{count} question{'s' if count != 1 else ''} only you can answer"
                        )
                    reason = " · ".join(parts) + ". Then reply `go`."
                    commands = [f"{n}: " for n in open_numbers[:4]] + ["go", "park it"]
                    headline = "Answers needed"
                elif page.get("package_hash") and len(page.get("final_controls", [])) != 1:
                    reason = page.get(
                        "reason",
                        "The form's last step with its Submit control was not reached. Check "
                        "the recruiting browser, then reply `go`.",
                    )
                    headline = "Final step not reached"
                elif page.get("package_hash"):
                    from .submission import enabled_adapter

                    if fit.get("unverified"):
                        items = [f"not verified · {u}" for u in fit["unverified"][:4]]
                    caveat = (
                        " The posting states requirements I could not check against your "
                        "profile; see Why."
                        if fit.get("unverified")
                        else ""
                    )
                    try:
                        enabled_adapter(page.get("url") or item["url"])
                    except PermissionError as why:
                        # No adapter or submission disabled: the owner presses Submit.
                        final_state = "MANUAL_TAKEOVER"
                        cause = (
                            "this site has no submission adapter yet"
                            if "adapter" in str(why)
                            else "final submission is off in your local config"
                        )
                        reason = (
                            f"Every field is filled from approved facts, but {cause}. Review "
                            "the form in the recruiting browser, press its Submit button "
                            "yourself, then reply." + caveat
                        )
                        commands = ["applied", "park it"]
                        headline = "Ready · send it yourself"
                    else:
                        final_state = "READY_FOR_REVIEW"
                        if settings.get("auto_submit"):
                            # Owner policy: send it; the thread is the record to review after.
                            workflow.set_state(application_id, "READY_FOR_REVIEW")
                            queue_auto_submit(application_id, page["package_hash"])
                            workflow.refresh_status(application_id)
                            result = {
                                "application_id": application_id,
                                "page": page,
                                "status": "READY_FOR_REVIEW",
                                "auto_submit": True,
                                "submitted": False,
                            }
                            workflow.save_result(application_id, result)
                            return result
                        reason = (
                            "Every field is filled from approved facts. Check the form and "
                            "the resume in the recruiting browser, then reply `send it`." + caveat
                        )
                        commands = ["send it", "go"]
                        headline = "Ready to submit"
                else:
                    reason = page.get("reason", page.get("status", "Browser requires review"))
                break
            links = page.get("application_links", [])
            if not links:
                reason = (
                    "No Apply control was found on this page. Open the recruiting browser, "
                    "reach the form yourself, then reply `go`."
                )
                headline = "Apply control not found"
                break
            phase = "follow_application_link"
            page = browser_call(
                "follow",
                run_id=application_id,
                observation_id=page["observation_id"],
                ref=links[0]["ref"],
            )
        else:
            reason = (
                "Navigation hit its step limit before reaching a form. Inspect the recruiting "
                "browser, then reply `go`."
            )
            headline = "Navigation stopped"
    except Exception as error:
        raise PhaseError(phase, error) from error
    result = {"application_id": application_id, "page": page, "reason": reason, "submitted": False}
    workflow.save_result(application_id, result)
    return held(
        application_id,
        final_state,
        reason,
        headline,
        channel=channel,
        questions=questions,
        commands=commands,
        items=items,
    )


# Word replies inside an application's own thread, where the application is implied.
WORDS = {
    "go": "resume",
    "proceed": "resume",
    "continue": "resume",
    "resume": "resume",
    "park": "defer",
    "park it": "defer",
    "defer": "defer",
    "later": "defer",
    "skip": "defer",
    "send": "submit",
    "send it": "submit",
    "submit": "submit",
    "apply": "submit",
    "apply now": "submit",
    "applied": "applied",
    "i applied": "applied",
    "done": "applied",
    "sent it myself": "applied",
    "not sent": "not-submitted",
    "not submitted": "not-submitted",
    "nothing sent": "not-submitted",
    "create account": "account",
    "make an account": "account",
    "account": "account",
}


def draft_by_number(application_id: str, number: int) -> dict:
    """The Nth Qwen draft in the proposals file, bound to its exact stored hash."""
    path = state_root() / f"applications/{application_id}/answer-proposals.json"
    answers = json.loads(path.read_text()).get("answers", []) if path.exists() else []
    drafts = [a for a in answers if a.get("kind") == "proposal"]
    if not drafts:
        raise ValueError("There are no drafts here to use.")
    if not 1 <= number <= len(drafts):
        raise ValueError(f"There is no draft {number} here; the drafts go up to {len(drafts)}.")
    return {
        "field_key": drafts[number - 1]["key"],
        "proposal_hash": drafts[number - 1]["proposal_hash"],
    }


def question_by_number(application_id: str, number: int) -> dict:
    """The Nth question of the latest hold's numbered list, by its exact stored key."""
    questions = (workflow.latest_hold(application_id) or {}).get("questions") or []
    if not questions:
        raise ValueError("Nothing here is waiting for an answer.")
    if not 1 <= number <= len(questions):
        raise ValueError(
            f"There is no question {number} here; the list goes up to {len(questions)}."
        )
    return {"field_key": questions[number - 1]["key"]}


def held_on_fit(application_id: str) -> bool:
    return (
        workflow.get(application_id)["status"] == "NEEDS_USER"
        and (workflow.latest_hold(application_id) or {}).get("headline") == "Your call on fit"
    )


def thread_command(text: str, application_id: str) -> dict | None:
    """A reply in the application's own thread; unknown text is ignored, a reply that
    cannot apply raises a one-line plain-language reason."""
    raw = text.strip().strip("`").strip()
    match = re.fullmatch(
        r"(?:answer\s+)?(\d{1,2})\s*[:=]\s*(.{1,6000})", raw, re.IGNORECASE | re.DOTALL
    )
    if match:
        number = int(match[1])
        return {
            "kind": "answer",
            "application_id": application_id,
            **question_by_number(application_id, number),
            "value": match[2].strip(),
            "number": number,
        }
    word = " ".join(raw.rstrip(".!?").split()).lower()
    match = re.fullmatch(r"(?:use draft|draft|use) (\d{1,2})", word)
    if match:
        return {
            "kind": "use",
            "application_id": application_id,
            **draft_by_number(application_id, int(match[1])),
            "word": word,
        }
    kind = WORDS.get(word)
    if kind is None:
        return None
    if kind == "resume":
        kind = "proceed" if held_on_fit(application_id) else "resume"
    elif kind == "submit":
        item = workflow.get(application_id)
        if item["status"] == "APPLIED":
            raise ValueError("This one was already sent.")
        if item["status"] in {"SUBMITTING", "UNKNOWN_SUBMISSION"}:
            raise ValueError("A send is already in flight; nothing is sent twice.")
        if item["status"] != "READY_FOR_REVIEW" or not item["package_hash"]:
            raise ValueError("Nothing is ready to send here yet.")
        return {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": item["package_hash"],
            "word": word,
        }
    elif kind in {"applied", "not-submitted"}:
        return {
            "kind": "reconcile",
            "application_id": application_id,
            "outcome": kind,
            "word": word,
        }
    return {"kind": kind, "application_id": application_id, "word": word}


def parse_command(
    message: dict, owner: str, channel: str, allowed: set[str], threads: dict | None = None
) -> dict | None:
    """Owner replies only. Explicit id forms work in any allowed channel; word replies
    resolve only inside a thread listed in `threads` (thread id → application id)."""
    if (
        channel not in allowed
        or message.get("author", {}).get("id") != owner
        or message.get("author", {}).get("bot")
    ):
        return None
    text = message.get("content", "").strip()
    command = explicit_command(text)
    if command:
        return command
    application_id = (threads or {}).get(channel)
    if not application_id:
        return None
    return thread_command(text, application_id)


def explicit_command(text: str) -> dict | None:
    match = re.fullmatch(r"(resume|defer|proceed) ([a-f0-9]{12})", text, re.IGNORECASE)
    if match:
        return {"kind": match[1].lower(), "application_id": match[2].lower()}
    match = re.fullmatch(r"account ([a-f0-9]{12}) create", text, re.IGNORECASE)
    if match:
        return {"kind": "account", "application_id": match[1].lower()}
    match = re.fullmatch(r"submit ([a-f0-9]{12}) ([a-f0-9]{8,64})", text, re.IGNORECASE)
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
    match = re.fullmatch(r"use ([a-f0-9]{12}) ([a-f0-9]{12}) ([a-f0-9]{8,64})", text, re.IGNORECASE)
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
    label = None
    with workflow.db() as conn:
        if conn.execute(
            "SELECT 1 FROM owner_commands WHERE message_id=?", (message_id,)
        ).fetchone():
            return
        if command["kind"] == "reconcile":
            if item["status"] not in {"UNKNOWN_SUBMISSION", "MANUAL_TAKEOVER"}:
                raise PermissionError(
                    "Only an unknown submission or a blocked application can be reconciled"
                )
        elif item["status"] in {"APPLIED", "SUBMITTING", "UNKNOWN_SUBMISSION"}:
            raise PermissionError("This application cannot be prepared again")
        if command["kind"] == "submit":
            # A prefix of at least eight hex characters names the one current package.
            current = str(item["package_hash"] or "")
            if item["status"] != "READY_FOR_REVIEW" or not current.startswith(
                command["package_hash"]
            ):
                raise PermissionError(
                    "Submission needs the exact package hash of a reviewed, ready application"
                )
            command = {**command, "package_hash": current}
        if command["kind"] in {"proceed", "account"} and item["status"] != "NEEDS_USER":
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
                    and a["proposal_hash"].startswith(command["proposal_hash"])
                ),
                None,
            )
            if answer is None:
                raise PermissionError(
                    "Draft changed or is not an answer proposal; review the current draft"
                )
            command = {
                **command,
                "value": answer["value"],
                "proposal_hash": answer["proposal_hash"],
            }
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
            label = field["label"]
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
    data = {k: v for k, v in command.items() if k != "application_id"}
    if label is not None:
        data["label"] = label
    workflow.record(
        application_id,
        "owner_answer" if command["kind"] in {"answer", "use"} else command["kind"] + "_requested",
        data,
    )
    workflow.flush_events(application_id)
    if command["kind"] in {"resume", "proceed", "account"}:
        workflow.set_state(application_id, "QUEUED")
    elif command["kind"] == "defer":
        workflow.set_state(application_id, "DEFERRED")
        with contextlib.suppress(Exception):  # a tab that is already gone is fine
            browser_call("close", run_id=application_id)
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
            r["thread_id"]: r["id"]
            for r in conn.execute(
                "SELECT id,thread_id FROM application_queue WHERE thread_id IS NOT NULL "
                "AND status NOT IN ('APPLIED','SUBMITTING')"
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
            if not checkpoint:
                continue
            try:
                command = parse_command(message, owner, channel, channels, threads)
                if not command:
                    continue
                if channel in threads and threads[channel] != command["application_id"]:
                    raise PermissionError("This reply belongs to another application's thread")
                apply_command(command, message["id"])
            except (ValueError, PermissionError) as error:
                # One plain line in the same channel says why the reply did not apply.
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
            "recruiting browser, then reply `go` to prepare again.",
            commands=["go", "park it"],
            headline="Preparation interrupted",
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
            workflow.system_line(
                application_id,
                f"submission not attempted · package {package_hash} · {str(error)[:300]}",
            )
            if workflow.get(application_id)["status"] == "READY_FOR_REVIEW":
                workflow.action_needed(
                    application_id,
                    "Submission was not attempted: " + str(error)[:600] + ". Nothing was sent. "
                    "Reply `go` to prepare it again.",
                    commands=["go"],
                    headline="Submission not attempted",
                )
        with workflow.db() as conn:
            conn.execute(
                "UPDATE owner_commands SET status=? WHERE message_id=?",
                (outcome, row["message_id"]),
            )
        write_private(state_root() / f"applications/{application_id}/submit-result.json", result)
        results.append({**result, "outcome": outcome})
    return results


def prune_excluded() -> int:
    """Queued feed jobs are re-checked against the approved rules before the browser opens.

    A link the owner pasted is theirs to decide and is never pruned.
    """
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT id,title FROM application_queue WHERE status='QUEUED' AND source!='owner_link'"
        ).fetchall()
    if not rows:
        return 0
    prefs = matching.read_approved()["profile"]["preferences"]
    pruned = 0
    for row in rows:
        company, _, title = row["title"].partition(" — ")
        if not title:
            company, title = "", row["title"]
        reason = matching.excluded_by_rules(title, company, prefs)
        if reason:
            workflow.set_state(row["id"], "DEFERRED", error="excluded by your rules: " + reason)
            pruned += 1
    return pruned


def next_queued(max_waiting: int):
    with workflow.db() as conn:
        # An explicit owner resume/proceed is processed even while other
        # applications wait; otherwise the queue holds until the owner answers.
        resumed = conn.execute(
            "SELECT q.id FROM application_queue q JOIN owner_commands c ON c.application_id=q.id "
            "WHERE q.status='QUEUED' AND c.kind IN ('resume','proceed','account') AND c.status='applied' "
            "ORDER BY c.created_at DESC LIMIT 1"
        ).fetchone()
        if resumed:
            return resumed[0]
        waiting = conn.execute(
            "SELECT COUNT(*) FROM application_queue WHERE status IN ('NEEDS_USER','READY_FOR_REVIEW')"
        ).fetchone()[0]
        if waiting >= max_waiting:
            return None
        pasted = conn.execute(
            "SELECT id FROM application_queue WHERE status='QUEUED' AND source='owner_link' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if pasted:
            return pasted[0]
        settings = workflow.config()
        if settings.get("auto_submit"):
            # Unattended sending is paced: a daily cap the owner sets, counted from attempts.
            cap = int(settings.get("max_submissions_per_day", 10))
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            sent_today = conn.execute(
                "SELECT COUNT(*) FROM live_submission_attempts WHERE created_at LIKE ?",
                (today + "%",),
            ).fetchone()[0]
            if sent_today >= cap:
                return None
            # Sites score form duration and burst rate; unattended sends keep a human gap.
            gap = timedelta(minutes=int(settings.get("min_minutes_between_submissions", 8)))
            last = conn.execute("SELECT MAX(created_at) FROM live_submission_attempts").fetchone()[
                0
            ]
            if last and datetime.now(UTC) - datetime.fromisoformat(last) < gap:
                return None
        queued = conn.execute(
            "SELECT id FROM application_queue WHERE status='QUEUED' ORDER BY created_at DESC LIMIT 1"
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
        workflow.ensure_system_channel()
        poll_commands()
        workflow.flush_pending()
        prune_excluded()
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
            if result.get("auto_submit"):
                submitted = run_approved_submissions()
                if submitted:
                    result = {**result, "submissions": submitted}
        except PhaseError as failure:
            from .reasoning import ModelUnavailable

            if isinstance(failure.error, ModelUnavailable):
                # An outage of the local model is not the application's problem: wait.
                workflow.set_state(queued, "QUEUED", error="model_unavailable")
                workflow.record(queued, "model_unavailable", {"phase": failure.phase})
                result = {"application_id": queued, "status": "QUEUED", "waiting": "model"}
                write_private(state_root() / "workflow-status.json", result)
                return result
            workflow.set_state(queued, "NEEDS_USER", error=str(failure))
            write_private(
                state_root() / f"applications/{queued}/error.json",
                {
                    "phase": failure.phase,
                    "type": type(failure.error).__name__,
                    "detail": str(failure.error)[:1500],
                },
            )
            workflow.system_line(
                queued, f"preparation stopped · {failure.phase} · {type(failure.error).__name__}"
            )
            detail = str(failure.error)
            if detail.startswith("Field verification failed"):
                label = detail.partition(":")[2].strip() or "a field"
                reason = (
                    f"The site changed the value I typed for “{label}”. Check it in the "
                    "recruiting browser, then reply `go`."
                )
            else:
                reason = (
                    f"Preparation stopped during {failure.phase.replace('_', ' ')}: "
                    + type(failure.error).__name__
                    + ". Details are saved locally; nothing was submitted."
                )
            workflow.action_needed(
                queued,
                reason,
                commands=["go", "park it"],
                headline="Preparation stopped",
            )
            result = {
                "application_id": queued,
                "status": "NEEDS_USER",
                "error": str(failure),
            }
        write_private(state_root() / "workflow-status.json", result)
        return result
