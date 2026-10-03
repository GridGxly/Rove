"""Single-application local worker and authenticated Discord review commands."""

import contextlib
import fcntl
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import (
    delivery,
    fastpath,
    inbound,
    intake,
    matching,
    memory_channel,
    overlays,
    timing,
    workflow,
)
from .discord_feed import discord, private_env
from .live_browser import browser_call, owner_words
from .reasoning import GATE_KINDS
from .resumes import one_erga_pass, prepare_resume, start_preparation
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
    excluded = [r for r in conflicts if "excluded_title_keywords" in str(r.get("evidence", ""))]
    if excluded:
        # Qwen matched the role against the owner's excluded kinds (AI/ML and the like).
        summary = "This looks like a role your rules exclude. " + workflow.brief(
            fit.get("rationale", ""), 200
        )
    elif conflicts:
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


def attach_stop_screenshot(application_id: str):
    """Every stop shows what the browser showed: the freshest screenshot of this run."""
    directory = state_root() / f"applications/{application_id}"
    candidates = [p for p in (directory / "failure.png", directory / "browser.png") if p.is_file()]
    if not candidates:
        return
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if datetime.now(UTC).timestamp() - newest.stat().st_mtime > 30 * 60:
        return
    workflow.attach_file(application_id, newest, "→ What the browser showed when it stopped")


# Erga drafts to render for the thread once the tick's send is done: (application, tex,
# page fill). Rendering takes seconds and is for review only, so it never delays a send.
_deferred_drafts: list[tuple] = []


def attach_resume(application_id: str, manifest: dict):
    """The resume as sent, and Erga's rejected tailored draft when there is one, for review.

    Both go into the thread record: while an application is being prepared they are
    posted after the send, and the draft is rendered then too."""
    directory = state_root() / f"applications/{application_id}"
    kind = (
        "tailored by Erga from your evidence"
        if manifest.get("tailored")
        else "your approved base PDF"
    )
    workflow.attach_file(application_id, directory / "resume.pdf", f"→ Resume as sent · {kind}")
    tex = manifest.get("rejected_proposal_tex")
    if not tex or not Path(tex).is_file() or not manifest.get("warning_is_new", True):
        return  # a draft rejected for the reason the owner already saw stays in Erga's folder
    if workflow.delivery_deferred():
        _deferred_drafts.append((application_id, tex, manifest.get("page_fill_ratio")))
        return
    attach_rejected_draft(application_id, tex, manifest.get("page_fill_ratio"))


def attach_deferred_drafts():
    """Render and attach the drafts held back during the tick's pass."""
    while _deferred_drafts:
        application_id, tex, fill = _deferred_drafts.pop(0)
        with contextlib.suppress(Exception):  # review material; the record never waits on it
            attach_rejected_draft(application_id, tex, fill)


def attach_rejected_draft(application_id: str, tex, fill):
    """Erga's tailored draft that failed its layout check, rendered and marked not sent."""
    directory = state_root() / f"applications/{application_id}"
    if not Path(tex).is_file():
        return
    draft = directory / "tailored-draft.pdf"
    try:
        import shutil
        import subprocess

        workdir = directory / "tailored-draft"
        workdir.mkdir(exist_ok=True, mode=0o700)
        shutil.copyfile(tex, workdir / "draft.tex")
        subprocess.run(
            [str(Path.home() / ".local/bin/tectonic"), "--keep-logs", "draft.tex"],
            cwd=workdir,
            check=True,
            capture_output=True,
            timeout=120,
        )
        shutil.move(workdir / "draft.pdf", draft)
        draft.chmod(0o600)
    except Exception as error:  # noqa: BLE001 -- a draft that cannot be rendered is reported, not fatal
        workflow.system_line(
            application_id, f"tailored draft render failed: {type(error).__name__}"
        )
        return
    why = (
        f"it fills {round(float(fill) * 100)}% of the page and Erga requires 90%"
        if fill
        else "it failed Erga's layout check"
    )
    workflow.attach_file(
        application_id,
        draft,
        f"→ Tailored draft Erga rejected ({why}) · not sent · for your review",
    )


def ready_resume(application_id: str, url: str, preparing) -> dict:
    """The application's resume before anything is uploaded, announced in the thread once.

    `preparing` is the background intake started beside the job-fit review, if any. A
    resume an earlier pass prepared but never announced (its pass was held on fit) is
    announced now. The fallback's reason shows only when it is news to the owner; the
    technical detail goes to the system log.
    """
    manifest_path = state_root() / f"applications/{application_id}/resume-manifest.json"
    if preparing is not None:
        resume = preparing.result()
    elif manifest_path.exists():
        resume = json.loads(manifest_path.read_text())
        if resume.get("announced", True):
            return resume
    else:
        workflow.record(
            application_id,
            "resume_preparation_started",
            {"source": "approved Erga evidence", "job_url": url},
        )
        workflow.flush_events(application_id)
        resume = prepare_resume(application_id, url)
    if not resume["ready"] or resume.get("announced") is True:
        return resume
    workflow.record(
        application_id,
        "resume_prepared",
        {
            "sha256": resume["resume_sha256"],
            "tailored": resume.get("tailored", False),
            "warning": resume.get("warning", "") if resume.get("warning_is_new", True) else "",
            "review": "Review the exact PDF before approving submission.",
        },
    )
    workflow.flush_events(application_id)
    if resume.get("system_note"):
        workflow.system_line(application_id, resume["system_note"])
    attach_resume(application_id, resume)
    if resume.get("announced") is False and manifest_path.exists():
        write_private(manifest_path, {**json.loads(manifest_path.read_text()), "announced": True})
    return resume


@timing.stage(None, "hold")
def held(
    application_id: str, status: str, reason: str, headline: str, channel: str = "action", **extra
) -> dict:
    """End a run in a state the owner must act on, with one clear card in one channel."""
    workflow.set_state(application_id, status)
    workflow.action_needed(application_id, reason, headline=headline, channel=channel, **extra)
    attach_stop_screenshot(application_id)
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

    A draft is never used for a legal or personal question, or for a field the form gave
    no label: code decides that from the question itself, whatever the model returned.
    The row is stored as a draft Rove used (`auto-draft:`), never as the owner's reply.
    """
    from . import questions

    labels = {q["key"]: q.get("label", "") for q in asked}
    numbers = {q["key"]: number for number, q in enumerate(asked, start=1)}
    by_key = {q["key"]: q for q in asked}
    used = []
    refused = []
    with workflow.db() as conn:
        for answer in proposals.get("answers", []):
            if answer.get("gate"):
                # The review already kept this one for the owner; the log says why.
                refused.append((answer.get("key"), answer["gate"]))
            if answer.get("kind") != "proposal" or answer["key"] not in labels:
                continue
            gate = questions.draft_gate(by_key[answer["key"]])
            if gate:
                refused.append((answer["key"], gate["code"]))
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
    for key, why in refused:
        workflow.system_line(application_id, f"draft not used · question {key} · {why}")
    return used


def question_list(asked: list, pending: list, proposals: dict, used: set) -> list[dict]:
    """The owner's numbered questions, in form order, each with how it stands.

    A question is `open` (only the owner can answer it), `drafted` (Qwen's draft waits
    in its card), or `used` (the draft became the answer under the owner's policy).
    Numbers are positions in this list; the thread's cards and the `N: value` reply
    both count the same way.
    """
    from .questions import real_options

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
            # A select's placeholder ("Select...") is no choice: it is never listed.
            "options": real_options(question.get("options")),
            "required": question.get("required", True),
        }
        if question.get("label_missing"):
            entry["label_missing"] = True  # the card says the question could not be read
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
            "INSERT INTO owner_commands VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET status='applied', created_at=excluded.created_at",
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
    """Record a blank for optional fields with no approved fact or draft; never asks.

    The blank is Rove's own record (`auto-skip:`), not an owner answer, and it is never
    remembered: the same question on a later form is resolved afresh. The thread line
    names each field by its question, never by a key.
    """
    from . import form_reading

    with workflow.db() as conn:
        for question in questions:
            conn.execute(
                "INSERT OR IGNORE INTO application_answers VALUES(?,?,?,?)",
                (application_id, question["key"], "skip", f"auto-skip:{question['key']}"),
            )

    def named(question: dict) -> str:
        label = form_reading.squash(question.get("label"))
        if not label or form_reading.looks_like_key(label):
            return form_reading.unreadable_label()
        return label[:80]

    workflow.record(
        application_id, "optional_skipped", {"labels": [named(q) for q in questions[:12]]}
    )
    workflow.flush_events(application_id)


@timing.stage(None, "pass")
@one_erga_pass
def process(application_id: str) -> dict:
    item = workflow.get(application_id)
    timing.queue_wait(item)
    settings = workflow.config()
    (state_root() / f"applications/{application_id}/error.json").unlink(missing_ok=True)
    try:
        workflow.ensure_forum(application_id)
    except Exception as error:
        raise PhaseError("forum", error) from error
    workflow.set_state(application_id, "PREPARING", error=None)
    waits = workflow.intake_hold(item)
    if waits:
        # A link the agent queued opens no page until the owner has looked at it.
        return held(
            application_id,
            "NEEDS_USER",
            waits,
            workflow.INTAKE_HEADLINE,
            commands=["go", "park it"],
        )
    phase = "open"
    final_state = "NEEDS_USER"
    posting_text = ""
    questions: list = []
    items: list = []
    channel = "action"
    commands: list = ["go", "park it"]
    headline = "Browser needs a look"
    try:
        timing.lap("open")
        page = browser_call("open", url=item["url"])
        for _ in range(6):
            if not page.get("fields") and not page.get("blocked"):
                # The posting itself; application-form labels are never requirements, and
                # a block page is not a posting.
                posting_text = page.get("text", "") or posting_text
            if page.get("blocked"):
                phase = "blocked_retry"
                timing.lap("reopen")
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
                # The retry reached a page: read it from the top, posting text included.
                continue
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
                    timing.lap("sign_in")
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
                    timing.lap("sign_in")
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

                # Erga's intake runs beside the job-fit review; the resume step joins it.
                preparing = start_preparation(
                    prepare_resume,
                    application_id,
                    item["url"],
                    posting_text or page.get("text", "").split("Apply for this job")[0],
                )
                phase = "job_fit_review"
                # One model call per job: a later pass reads the stored review back.
                fit = review_job(application_id, page, posting_text)
                workflow.system_line(
                    application_id,
                    f"fit · {fit['decision']}"
                    + (" · stored review" if fit.get("cached") else "")
                    + (" · reviewed in the background" if fit.get("background") else ""),
                )
                # A link the owner pasted is a decision already made: never ask again.
                if (
                    fit["decision"] != "fit"
                    and intake.fit_may_hold(item["source"], fit)
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
                # The form's host must be a board or one the owner let a form be filled
                # on: decided here, before anything is typed or the thread says "sending".
                from .submission import fill_hold

                site = fill_hold(application_id, page.get("url") or item["url"])
                if site:
                    return held(
                        application_id,
                        site["status"],
                        site["reason"],
                        site["headline"],
                        commands=site["commands"],
                    )
                phase = "resume"
                timing.lap("resume")
                # The first fill uploads the resume, so the background intake joins here.
                resume = ready_resume(application_id, item["url"], preparing)
                if not resume["ready"]:
                    reason = resume["reason"] + ". The resume needs review before upload."
                    headline = "Resume needs review"
                    break
                phase = "prepare"
                timing.lap("fill")
                page = browser_call("prepare", run_id=application_id)
                timing.note(**fastpath.fill_counts(application_id, page))
                pending = page.get("pending", [])
                proposals: dict = {"answers": []}
                asked: list = []
                used: set = set()
                if pending:
                    phase = "answer_drafting"
                    kinds = fastpath.drafting_counts(pending, page.get("fields"))
                    if fastpath.needs_model(kinds):
                        with timing.stage(application_id, "drafting", **kinds):
                            proposals = review_application(application_id, page)
                            timing.note(
                                proposals=sum(
                                    a.get("kind") == "proposal"
                                    for a in proposals.get("answers", [])
                                )
                            )
                    else:
                        # Nothing here is Qwen's to write or choose: no call. The questions
                        # go to the owner exactly as they would after a needs-owner reply.
                        timing.record(application_id, "drafting", 0.0, skipped=True, **kinds)
                        workflow.system_line(
                            application_id,
                            f"drafting skipped · {kinds['questions']} pending, none needs "
                            "writing or a model choice",
                        )
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
                        timing.lap("fill")
                        page = browser_call("prepare", run_id=application_id)
                        timing.note(**fastpath.fill_counts(application_id, page))
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
                    headline = page.get("headline") or "Final step not reached"
                    if page.get("owner_step"):  # a video interview or assessment he takes
                        final_state, commands = "MANUAL_TAKEOVER", ["applied", "park it"]
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
                        if settings.get("auto_submit") and workflow.sends_unattended(item):
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
            from .submission import link_hold

            site = link_hold(application_id, page.get("url") or "", links[0].get("url"))
            if site:
                # The Apply control leads to another site: one the owner has to let in.
                return held(
                    application_id,
                    site["status"],
                    site["reason"],
                    site["headline"],
                    commands=site["commands"],
                )
            phase = "follow_application_link"
            timing.lap("follow")
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


# A reply made only of these is a nod or a command, never an answer to a question.
CHATTER = frozenset(
    {
        *("ok", "okay", "k", "kk", "thanks", "thank", "thx", "ty", "cool", "nice", "great"),
        *("got", "it", "sure", "alright", "awesome", "perfect", "sounds", "good", "lol"),
        *("haha", "hmm", "hm", "yeah", "yep", "please", "pls", "ahead", "now", "you"),
        *(word for phrase in WORDS for word in phrase.split()),
        *("use", "draft", "answer", "later", "not", "yet"),
    }
)
EXPLICIT_FORM = "For this one, reply `1: your answer` so I know it is your answer."


def single_question_answer(raw: str, application_id: str) -> dict | None:
    """When one question is open, the owner's plain reply is its answer. With options,
    the reply must name one of them; a link or a long message is never taken as one.

    A reply with no letters or digits ("👍"), or one made only of nods and command words
    ("ok go", "thanks"), is no answer: it is ignored, never stored or remembered. Any other
    reply to the one open question is the answer, however short ("40", "May 2027"): the
    owner should not have to retype it. A legal or personal question without options
    takes only the explicit `1: value` form.
    """
    from . import questions

    hold = workflow.latest_hold(application_id) or {}
    open_questions = [q for q in hold.get("questions") or [] if q.get("state", "open") == "open"]
    if len(open_questions) != 1 or len(raw) > 200 or re.search(r"https?://", raw):
        return None
    if workflow.get(application_id)["status"] != "NEEDS_USER":
        return None
    question = open_questions[0]
    value = raw.strip()
    tokens = re.findall(r"[^\W_]+", value.lower())
    options = questions.real_options(question.get("options"))
    wanted = " ".join(re.findall(r"[a-z0-9]+", value.lower()))
    match = next(
        (o for o in options if " ".join(re.findall(r"[a-z0-9]+", o.lower())) == wanted), None
    )
    if match is None and (not tokens or set(tokens) <= CHATTER):
        return None  # a nod, an emoji or a command word: not an answer to anything
    if options:
        if match is None:
            raise ValueError(
                "That is not one of the options for the open question: "
                + " / ".join(o for o in options[:8] if not o.strip().startswith("-"))
            )
        value = match
    elif questions.is_sensitive(question.get("label"), question.get("kind") or ""):
        raise ValueError(EXPLICIT_FORM)
    return {
        "kind": "answer",
        "application_id": application_id,
        "field_key": question["key"],
        "value": value,
        "number": 1,
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
        return single_question_answer(raw, application_id)
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
    application_id = (threads or {}).get(channel) or card_application(message, channel)
    if not application_id:
        return None
    return thread_command(text, application_id)


HELP_LINE = (
    "I read replies in each application's thread, or as a Discord reply to its card here: "
    "`go`, `park it`, `send it`, `use draft 2`, `2: your answer`, `applied`, `not sent`."
)


def card_application(message: dict, channel: str) -> str | None:
    """Which application a message in action-needed or shortlist is about.

    A Discord reply names the application only when it points at one of Rove's own live
    cards in that channel; a reply on anything else (a look-alike card, a card already
    withdrawn) names none, however few cards are live. A message that is not a reply is
    about the one live card when there is exactly one. With several live, a word or a
    plain answer gets a "Which one?" line that lists them by company and role; the owner
    then names the company or replies on the card.
    """
    settings = workflow.config()
    names = {settings.get(key): name for name, key in workflow.NOTICE_CHANNELS.items()}
    if channel not in names:
        return None
    content = str(message.get("content") or "")
    word = " ".join(content.strip().strip("`").rstrip(".!?").split()).lower()
    is_command = bool(
        word in WORDS
        or re.fullmatch(r"(?:use draft|draft|use) \d{1,2}", word)
        or re.match(r"(?:answer\s+)?\d{1,2}\s*[:=]", word)
    )
    referenced = str((message.get("message_reference") or {}).get("message_id") or "")
    if referenced:
        with workflow.db() as conn:
            row = conn.execute(
                "SELECT application_id FROM owner_notices WHERE message_id=? AND channel=? "
                "AND delivery='sent'",
                (referenced, names[channel]),
            ).fetchone()
        if row:
            return row["application_id"]
        if is_command:
            raise ValueError(
                "That is not one of my live cards, so nothing was done. Reply on a live "
                "card, or answer in the application's thread."
            )
        return None
    live = inbound.live_cards(names[channel])
    if len(live) == 1:
        return live[0]["id"]
    if len(live) > 1 and (is_command or inbound.could_answer(content, live)):
        raise ValueError(inbound.which_one(channel, message, live))
    if is_command:
        raise ValueError("No card is waiting here. Answer in the application's thread.")
    return None


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
        remember_later, remember_how = None, {}
        if command["kind"] in {"answer", "use"}:
            from . import questions

            observation_path = state_root() / f"applications/{application_id}/observation.json"
            observation = json.loads(observation_path.read_text())
            field = next(
                (f for f in observation["fields"] if f["key"] == command["field_key"]), None
            )
            if (
                field is None
                or field["kind"] in {"password", "file", "hidden"}
                or questions.manual_only(field["label"])
            ):
                raise PermissionError("Unknown or manual-only question")
            if command["value"].lower() == "skip" and field["required"]:
                raise ValueError("Required questions cannot be skipped")
            label = field["label"]
            conn.execute(
                "INSERT OR REPLACE INTO application_answers VALUES(?,?,?,?)",
                (application_id, field["key"], command["value"], message_id),
            )
            remembered = [
                o.get("label", "") if isinstance(o, dict) else str(o)
                for o in field.get("options") or []
            ]
            # The owner's own answer becomes a fact for the same question anywhere; an
            # answer to a question Rove could not read is for this form only. A draft he
            # approves counts the same way for a plain question, never for a legal or
            # personal one: those are remembered only in his own words.
            approved_draft = command["kind"] == "use" and not questions.is_sensitive(
                label, field.get("kind") or "", remembered
            )
            if (command["kind"] == "answer" or approved_draft) and not field.get("label_missing"):
                remember_later = (label, remembered, command["value"])
                remember_how = {
                    "kind": field.get("kind") or "",
                    "employer": questions.employer_key(item["url"]),
                    "origin": "approved_draft" if approved_draft else "owner",
                }
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
    if remember_later:
        workflow.remember_answer(*remember_later, message_id, **remember_how)
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


# How often every candidate channel is read whatever Discord's channel list says, in case
# a channel's newest-message mark lags behind.
FULL_READ_EVERY = timedelta(minutes=10)


def say(channel: str, text: str):
    """One plain line in a channel the owner wrote in; a failure is logged, not raised."""
    try:
        discord(
            "POST",
            f"/channels/{channel}/messages",
            {"content": text, "allowed_mentions": {"parse": []}},
        )
    except (httpx.HTTPError, OSError) as error:
        workflow.delivery_failed("reply", channel, error, announce=False)


def full_read_due() -> bool:
    """True every FULL_READ_EVERY: then every candidate channel is read."""
    stamp = datetime.now(UTC)
    with workflow.db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poll_marks(name TEXT PRIMARY KEY, at TEXT NOT NULL)"
        )
        row = conn.execute("SELECT at FROM poll_marks WHERE name='full_read'").fetchone()
        due = not row or stamp - datetime.fromisoformat(row[0]) >= FULL_READ_EVERY
        if due:
            conn.execute(
                "INSERT OR REPLACE INTO poll_marks VALUES('full_read',?)", (stamp.isoformat(),)
            )
    return due


def channel_news(settings: dict, threads: dict) -> dict | None:
    """The newest message id Discord reports for each channel and active thread, from two
    guild-wide reads, or None when that is unknown (no guild, or time for a full read).

    A thread missing from the active list is archived and has nothing new: a message
    posted in an archived thread unarchives it. Raises when Discord cannot be reached.
    """
    guild = settings.get("guild_id")
    if not guild or full_read_due():
        return None
    listed = discord("GET", f"/guilds/{guild}/channels")
    active = discord("GET", f"/guilds/{guild}/threads/active")
    news: dict = {}
    if isinstance(listed, list) and listed:
        for channel in listed:
            if isinstance(channel, dict) and channel.get("id"):
                news[str(channel["id"])] = channel.get("last_message_id")
    known_threads = isinstance(active, dict) and isinstance(active.get("threads"), list)
    if known_threads:
        for thread in active["threads"]:
            if isinstance(thread, dict) and thread.get("id"):
                news[str(thread["id"])] = thread.get("last_message_id")
        for thread in threads:
            news.setdefault(str(thread), "archived")
    return news


def has_news(channel: str, checkpoint, news: dict | None) -> bool:
    """Whether a channel can hold a message after its cursor."""
    if news is None or not checkpoint or channel not in news:
        return True
    newest = news[channel]
    if newest == "archived" or not newest:
        return False  # archived, or no message was ever posted there
    try:
        return int(newest) > int(checkpoint[0])
    except (TypeError, ValueError):
        return True


def poll_commands() -> bool:
    """Read the owner's new messages in the owner channels and every thread that can take
    a reply, and act on each one once. Returns False when Discord could not be reached.

    Cheap when nothing happened: two guild-wide reads say which channels and threads have
    a message past their cursor, and only those are read (every channel is read every
    FULL_READ_EVERY regardless). A transport error, a 5xx or a rate limit ends the
    reading for this tick without raising; the next tick reads from the same cursors.

    Cursors: the first read of a channel takes the newest message's id; a channel with no
    message gets a cursor from Discord's own clock, never this Mac's, so a clock running
    ahead cannot skip the owner's first reply.

    Edits are not read, by design: Discord returns new messages only, and an edited
    message keeps its id, so an edit can neither repeat nor take back a reply that was
    already applied. The owner sends a new message instead.
    """
    from . import discord_feed

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
    channels = {
        settings.get("control_channel_id"),
        settings.get("action_channel_id"),
        settings.get("shortlist_channel_id"),
        settings.get("recruiting_channel_id"),
        settings.get("system_channel_id"),
        *threads,
    } - {None}
    quiet = {settings.get("control_channel_id")}
    try:
        news = channel_news(settings, threads)
    except (httpx.HTTPError, OSError) as error:
        workflow.delivery_failed("read", "guild", error, announce=False)
        if isinstance(error, httpx.HTTPStatusError) and not delivery.transient(error):
            news = None  # the guild reads were refused: read the channels one by one
        else:
            return False  # Discord is unreachable; right after waking from sleep, routine
    for channel in channels:
        with workflow.db() as conn:
            checkpoint = conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id=?", (channel,)
            ).fetchone()
        if not has_news(channel, checkpoint, news):
            continue
        # Read-only bootstrap establishes a cursor. Do not replay old commands.
        route = f"/channels/{channel}/messages?limit=100"
        if checkpoint:
            route += "&after=" + checkpoint[0]
        try:
            messages = discord("GET", route) or []
        except (httpx.HTTPError, OSError) as error:
            workflow.delivery_failed("read", channel, error, announce=False)
            if isinstance(error, httpx.HTTPStatusError) and not delivery.transient(error):
                continue  # this channel is refused or gone; the others are still read
            return False
        for message in sorted(messages, key=lambda m: int(m["id"])):
            if not checkpoint:
                continue
            if not inbound.from_owner(message, owner):
                # Nobody else's message is parsed, answered or acted on, in any channel.
                continue
            try:
                if intake.digest_reply(message, owner, channel):
                    # A numbered reply to the daily digest, read before any card's words.
                    continue
                handled = inbound.owner_message(message, channel, settings, threads)
                if handled is not None:
                    # A pasted link, a reply on a mail card, or taking back a mail's step.
                    if handled:
                        say(channel, handled)
                    continue
                command = parse_command(message, owner, channel, channels, threads)
                if not command:
                    if channel not in quiet and str(message.get("content") or "").strip():
                        # The owner typed where nothing applies: say once how replies work.
                        say(channel, HELP_LINE)
                    continue
                if channel in threads and threads[channel] != command["application_id"]:
                    raise PermissionError("This reply belongs to another application's thread")
                apply_command(command, message["id"])
            except (ValueError, PermissionError) as error:
                # One plain line in the same channel says why the reply did not apply.
                say(channel, str(error))
        maximum = None
        if messages:
            maximum = max(messages, key=lambda m: int(m["id"]))["id"]
        elif not checkpoint:
            # An empty channel also needs a cursor, otherwise its first command would be
            # discarded on the next poll as bootstrap history.
            maximum = discord_feed.empty_cursor()
        elif news and str(news.get(channel) or "").isdigit():
            # Discord's newest id for the channel was past the cursor and nothing is
            # there: that message was deleted (a withdrawn card). Its id is real, so
            # moving the cursor to it skips nothing and saves the read next tick.
            maximum = news[channel]
        if maximum is not None:
            with workflow.db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_checkpoints VALUES(?,?)", (channel, maximum)
                )
    memory_channel.poll(owner)  # `#memory` keeps its own cursor and plain-word replies
    return True


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
                    # The browser's own plain words (an unsafe redirect, a first send to
                    # a new employer) are the reason as they stand.
                    owner_words(str(error))
                    or "Submission was not attempted: "
                    + str(error)[:600]
                    + ". Nothing was sent. Reply `go` to prepare it again.",
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
    """Queued feed jobs are re-scored against the approved rules before the browser opens.

    A job a hard rule now excludes, one whose listing scores below the digest bar, and a
    second copy of a role already queued for another place are parked with the reason; the
    rest keep a score for the queue order. A link the owner pasted, a job they picked
    from the digest, and a job they told to go again are theirs to decide and never pruned;
    a link the agent queued has no such standing and is judged like a feed job.

    The first run also sorts a queue filled under the old rules: feed jobs whose listing
    scores in the digest tier leave the queue and wait for the owner's yes on the daily
    list. A mark keeps that from happening twice.

    Feed jobs whose posting closed, that waited past `feed_max_age_days`, or whose role is
    already in progress or sent through another board are parked too. Nothing is judged
    while the approved profile does not validate.
    """
    with workflow.db() as conn:
        queued = conn.execute(
            "SELECT id,title,url,source_url,source FROM application_queue WHERE status='QUEUED' "
            "AND id NOT IN (SELECT application_id FROM owner_commands "
            "WHERE kind IN ('resume','proceed','account'))"
        ).fetchall()
    rows = [row for row in queued if not workflow.source_policy(row["source"])["owner_decided"]]
    # Once, a queue filled under the old rules is sorted by the new tiers.
    first_sort = intake.mark(intake.QUEUE_SORTED) is None
    if not rows:
        if first_sort:
            intake.set_mark(intake.QUEUE_SORTED)
        return 0
    approved = intake.profile_gate(matching.read_approved)
    if approved is None:
        return 0
    stale = intake.stale_feed_jobs(rows, workflow.config(), datetime.now(UTC))
    for application_id, reason in stale:
        workflow.set_state(application_id, "DEFERRED", error=reason)
    gone = {application_id for application_id, _ in stale}
    rows = [row for row in rows if row["id"] not in gone]
    parked = intake.rescore_queue(rows, approved)
    moved = intake.move_borderline(rows, {a for a, _ in parked}) if first_sort else []
    for application_id, reason in [*parked, *moved]:
        workflow.set_state(application_id, "DEFERRED", error=reason)
    if first_sort:
        intake.set_mark(intake.QUEUE_SORTED)
        workflow.system_line(
            "intake",
            f"sorted the existing queue · {len(rows) - len(parked) - len(moved)} stay queued · "
            f"{len(moved)} moved to the daily list · {len(parked)} parked",
        )
    if stale:
        workflow.system_line(
            "intake",
            f"parked {len(stale)} queued feed job{'s' if len(stale) != 1 else ''} · "
            + workflow.clip(" · ".join(sorted({reason for _, reason in stale})), 300),
        )
    return len(stale) + len(parked) + len(moved)


def next_queued(max_waiting: int):
    """The next application to prepare: the owner's resumes, then pasted links, then
    digest picks, then everything else by score (the newest first within a score band).

    What the owner chose (see `workflow.SOURCES`) is never stopped by waiting holds, the
    daily cap or the hold brake. With `auto_submit` on, holds waiting on the owner do not
    stop feed jobs either; the daily cap, the per-platform gap and the hold brake are the
    brakes. The gap applies to every application, so a job on a platform that just took
    a submission gives way to the best job on another platform. With `auto_submit` off,
    `max_waiting` holds stop the rest of the queue until the owner answers, and they
    always stop a link the agent queued, which is never sent unattended.

    `feed_paused` holds every feed job, attended or not. While the approved profile does
    not validate nothing starts at all: every preparation would stop on the same check.
    """
    settings = workflow.config()
    unattended = bool(settings.get("auto_submit"))
    now = datetime.now(UTC)
    if intake.profile_gate() is None:
        return None
    with workflow.db() as conn:
        intake.prepare_pacing(conn)
        resting = intake.resting_platforms(conn, settings, now) if unattended else set()

        def first_free(rows):
            return next(
                (
                    row["id"]
                    for row in rows
                    if not resting or intake.platform_of(row["url"]) not in resting
                ),
                None,
            )

        queued = conn.execute(
            "SELECT q.id,q.url,q.source,q.created_at,COALESCE(s.score,0) AS score "
            "FROM application_queue q LEFT JOIN queue_scores s ON s.application_id=q.id "
            "WHERE q.status='QUEUED' ORDER BY q.created_at"
        ).fetchall()
        policies = {row["id"]: workflow.source_policy(row["source"]) for row in queued}
        # An explicit owner resume/proceed is processed even while other applications
        # wait; then the owner's own links and picks, however many holds there are.
        chosen = first_free(
            conn.execute(
                "SELECT q.id,q.url FROM application_queue q JOIN owner_commands c "
                "ON c.application_id=q.id WHERE q.status='QUEUED' "
                "AND c.kind IN ('resume','proceed','account') AND c.status='applied' "
                "ORDER BY c.created_at DESC"
            ).fetchall()
        ) or first_free(
            sorted(
                (row for row in queued if policies[row["id"]]["owner_decided"]),
                key=lambda row: (-policies[row["id"]]["rank"], *inbound.jump_key(conn, row["id"])),
            )
        )
        if chosen:
            return chosen
        # Unattended sending is paced from the attempts on record.
        if unattended and (
            intake.daily_cap_reached(conn, settings, now)
            or intake.inside_global_gap(conn, settings, now)
        ):
            return None
        stalled = (
            conn.execute(
                "SELECT COUNT(*) FROM application_queue "
                "WHERE status IN ('NEEDS_USER','READY_FOR_REVIEW')"
            ).fetchone()[0]
            >= max_waiting
        )
        braked = paused = None
        rest = []
        for row in reversed(queued):  # newest first, then by score band
            policy = policies[row["id"]]
            if policy["owner_decided"]:
                continue
            if intake.is_feed(row["source"]):
                if paused is None:
                    paused = intake.feed_paused(conn, settings, now)
                if paused:
                    continue
            if not (unattended and policy["unattended"]):
                # Not covered by unattended sending: waiting holds stop it as before.
                if not stalled:
                    rest.append(row)
                continue
            if braked is None:
                braked = intake.hold_brake(conn, settings, now)
            if not braked:
                rest.append(row)
        rest.sort(key=lambda row: -(row["score"] // intake.SCORE_BAND))
        return first_free(rest)


def tick() -> dict:
    settings = workflow.config()
    if not settings.get("enabled"):
        return {"enabled": False}
    with open(state_root() / "workflow.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"already_running": True}
        # This worker holds the lock: a delivery window still open was left by a crash.
        delivery.close_windows()
        workflow.ensure_system_channel()
        reachable = poll_commands() is not False
        if reachable:
            workflow.flush_pending()
        try:
            # The pass and its send come first; the thread record, files and system-log
            # lines are delivered when the window closes, at the end of this tick.
            with workflow.delivery_window():
                return work(settings, reachable)
        finally:
            attach_deferred_drafts()


def work(settings: dict, reachable: bool = True) -> dict:
    """The tick's work after the owner's messages are read: sends the owner approved,
    then the next application. A new application waits while Discord is unreachable,
    because its thread cannot be opened; that is not the application's problem."""
    prune_excluded()
    recover_interrupted()
    from .submission import settle_stale_sends

    settle_stale_sends()  # a send whose record never came becomes unknown, not stuck
    submitted = run_approved_submissions()
    if submitted:
        write_private(state_root() / "workflow-status.json", {"submissions": submitted})
        return {"submissions": submitted}
    with workflow.db() as conn:
        # A send in flight or an in-flight preparation holds everything. An unclear send
        # waits for the owner on its own card; the rest of the queue goes on.
        active = conn.execute(
            "SELECT id,status FROM application_queue WHERE status IN ('PREPARING','SUBMITTING') ORDER BY created_at LIMIT 1"
        ).fetchone()
    if active:
        return {"waiting_on": dict(active)}
    if not reachable:
        return {"idle": True, "discord": "unreachable"}
    queued = next_queued(intake.number(settings, "max_waiting_applications", 1))
    if not queued:
        from .prereview import idle  # background fit reviews and the model keepalive

        idle(settings)
        return {"idle": True}
    try:
        result = process(queued)
        if result.get("auto_submit"):
            submitted = run_approved_submissions()
            if submitted:
                result = {**result, "submissions": submitted}
    except PhaseError as failure:
        from .reasoning import ModelUnavailable

        if failure.phase == "forum" and isinstance(failure.error, httpx.TransportError):
            # Discord went away before the thread existed: try again next tick, no card.
            workflow.set_state(queued, "QUEUED", error="discord_unreachable")
            workflow.delivery_failed("forum", queued, failure.error, announce=False)
            result = {"application_id": queued, "status": "QUEUED", "waiting": "discord"}
            write_private(state_root() / "workflow-status.json", result)
            return result
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
        elif detail.startswith(overlays.HOLD_WORDS):
            reason = overlays.HOLD_WORDS  # a pop-up code would not guess on
        elif owner_words(detail):
            reason = owner_words(detail)  # the browser already said it in plain words
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
        attach_stop_screenshot(queued)
        result = {
            "application_id": queued,
            "status": "NEEDS_USER",
            "error": str(failure),
        }
    write_private(state_root() / "workflow-status.json", result)
    return result
