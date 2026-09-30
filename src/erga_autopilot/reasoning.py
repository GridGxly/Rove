"""Local Qwen/Hermes proposals, never an authorization or candidate-fact writer.

Qwen interprets postings and drafts answers. Trusted code owns exact comparisons
(dates, approved booleans), output validation, caching, and the final decision.
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import workflow
from .evidence import career_evidence
from .onboarding import read_approved
from .runtime import state_root, write_private

# Bump when the prompts in scripts/recruiting_reasoning.py change so cached
# reviews produced by an older prompt are never reused silently.
PROMPT_VERSION = "2026-09-30.3"
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-f0-9]{12}$")
    kind: Literal["proposal", "needs_user"]
    value: str = Field(max_length=3000)
    sources: list[str] = Field(max_length=10)
    explanation: str = Field(max_length=1000)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[Answer] = Field(max_length=40)


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "program",
        "graduation_window",
        "work_authorization",
        "sponsorship",
        "location",
        "dates",
        "degree",
        "skills",
        "other",
    ]
    requirement: str = Field(min_length=1, max_length=400)
    evidence: str = Field(default="", max_length=400)
    status: Literal["satisfied", "unknown", "conflict"]
    graduation_start: Month | None = None
    graduation_end: Month | None = None
    us_authorization_required: bool | None = None
    sponsorship_available: bool | None = None


class JobReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["fit", "needs_review", "not_fit"]
    rationale: str = Field(min_length=1, max_length=1500)
    requirements: list[Requirement] = Field(max_length=15)
    unknowns: list[Annotated[str, Field(max_length=300)]] = Field(max_length=10)


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def strip_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def completed_response(generated: dict) -> str:
    """Only a finished text turn is a usable model answer; a harness stop is not a draft."""
    result = generated.get("result") or {}
    reason = str(result.get("turn_exit_reason") or "")
    text = str(result.get("final_response") or "").strip()
    if result.get("completed") is not True or not reason.startswith("text_response") or not text:
        raise RuntimeError(
            "Qwen run did not complete: " + (reason or result.get("error") or "no response")
        )
    return text


def generate(directory: Path, context: dict, basename: str, attempts: int = 2) -> dict:
    write_private(directory / f"{basename}-input.json", context)
    python = workflow.config().get("hermes_python")
    if not python or not Path(python).is_file():
        raise ValueError("Configure the installed Hermes Python path before running Qwen")
    repository = Path(__file__).resolve().parents[2]
    failure = "not started"
    for _ in range(attempts):
        run = subprocess.run(
            [
                python,
                str(repository / "scripts/recruiting_reasoning.py"),
                "--hermes-checkout",
                str(Path.home() / ".hermes/hermes-agent"),
                "--input",
                str(directory / f"{basename}-input.json"),
                "--output",
                str(directory / f"{basename}-result.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
            env=dict(os.environ, PYTHONPATH=str(repository / "src"), HERMES_AUTOPILOT_16K="1"),
        )
        if run.returncode != 0:
            failure = f"harness exit {run.returncode}: " + run.stderr.strip()[-400:]
            continue
        generated = json.loads((directory / f"{basename}-result.json").read_text())
        try:
            completed_response(generated)
        except RuntimeError as error:
            failure = str(error)
            continue
        return generated
    raise RuntimeError(failure)


def months_inclusive(month: str, start: str | None, end: str | None) -> bool | None:
    if start is None and end is None:
        return None
    return (start is None or start <= month) and (end is None or month <= end)


def evaluate_requirements(requirements: list[dict], profile: dict) -> list[dict]:
    """Deterministic checks for exact facts; Qwen's judgment stands only where code cannot."""
    education = profile["education"]["schools"]
    graduation = next((s["graduation_month"] for s in education if s["graduation_month"]), None)
    eligible = profile["eligibility"]
    checked = []
    for item in requirements:
        entry = {**item, "checked_by": "qwen"}
        if item["kind"] == "graduation_window":
            inside = (
                None
                if graduation is None
                else months_inclusive(
                    graduation, item.get("graduation_start"), item.get("graduation_end")
                )
            )
            if inside is None:
                entry["status"] = "unknown"
                entry["note"] = "Graduation window or approved graduation month unavailable"
            else:
                entry["status"] = "satisfied" if inside else "conflict"
                entry["checked_by"] = "code"
                entry["note"] = (
                    f"Approved graduation {graduation} compared inclusively with "
                    f"{item.get('graduation_start') or '…'}–{item.get('graduation_end') or '…'}"
                )
        elif item["kind"] == "work_authorization":
            required = item.get("us_authorization_required")
            approved = eligible["us_work_authorized"]
            if required is True and approved is not None:
                entry["status"] = "satisfied" if approved else "conflict"
                entry["checked_by"] = "code"
            elif required is False:
                entry["status"] = "satisfied"
                entry["checked_by"] = "code"
            else:
                entry["status"] = "unknown"
        elif item["kind"] == "sponsorship":
            available = item.get("sponsorship_available")
            needs = eligible["sponsorship_now"] or eligible["sponsorship_future"]
            if eligible["sponsorship_now"] is None or eligible["sponsorship_future"] is None:
                entry["status"] = "unknown"
            elif available is False:
                entry["status"] = "conflict" if needs else "satisfied"
                entry["checked_by"] = "code"
            elif available is True:
                entry["status"] = "satisfied"
                entry["checked_by"] = "code"
            else:
                entry["status"] = "unknown"
        elif item["kind"] == "location" and profile["preferences"]["relocate"] is True:
            # Approved nationwide relocation plus accepted onsite work settles where the
            # applicant can be. Qwen may only flag an explicit posting rule that excludes
            # relocation, which stays a review item rather than a rejection.
            if item["status"] == "conflict":
                entry["status"] = "unknown"
                entry["note"] = "Relocation is approved; confirm the posting excludes it"
            elif "onsite" in profile["preferences"]["work_styles"]:
                entry["status"] = "satisfied"
                entry["checked_by"] = "code"
                entry["note"] = "Approved: relocate anywhere in the US and work onsite"
        checked.append(entry)
    return checked


# Qwen is told not to judge these, so an "unknown" that only restates a kind code
# already compared is noise, not a hold. Anything else becomes a review item.
CODE_KIND_TERMS = {
    "graduation_window": (
        r"graduat.*\b(window|range|within|falls|between|acceptable)\b"
        r"|\b(window|range|within|falls|between|acceptable)\b.*graduat"
    ),
    "work_authorization": r"authoriz|eligib",
    "sponsorship": r"sponsor",
    "location": r"relocat|on-?site|in[- ]person|physically present|local to|commut|office",
}


def merge_unknowns(requirements: list[dict], unknowns: list[str]) -> tuple[list, list, list]:
    resolved_kinds = {r["kind"] for r in requirements if r["checked_by"] == "code"}
    kept, resolved = [], []
    for text in unknowns:
        if any(
            re.search(pattern, text, re.IGNORECASE)
            for kind, pattern in CODE_KIND_TERMS.items()
            if kind in resolved_kinds
        ):
            resolved.append(text)
            continue
        kept.append(text)
        requirements.append(
            {
                "kind": "other",
                "requirement": text[:400],
                "evidence": "",
                "status": "unknown",
                "checked_by": "qwen",
                "note": "Listed by Qwen as unresolved",
            }
        )
    return requirements, kept, resolved


def decide(requirements: list[dict]) -> str:
    if any(r["status"] == "conflict" and r["checked_by"] == "code" for r in requirements):
        return "not_fit"
    if any(r["status"] != "satisfied" for r in requirements):
        return "needs_review"
    return "fit"


def evaluate_review(qwen_output: dict, profile: dict) -> dict:
    """Code's verdict over Qwen's extraction; rerunnable whenever the rules improve."""
    requirements = evaluate_requirements(
        [dict(item) for item in qwen_output["requirements"]], profile
    )
    requirements, kept, resolved = merge_unknowns(requirements, list(qwen_output["unknowns"]))
    return {
        "decision": decide(requirements),
        "qwen_decision": qwen_output["decision"],
        "rationale": qwen_output["rationale"],
        "requirements": requirements,
        "unknowns": kept,
        "resolved_unknowns": resolved,
    }


def review_job(application_id: str, page: dict, posting_text: str = "") -> dict:
    """Qwen extracts requirements before any applicant data enters the form."""
    approved = read_approved()
    item = workflow.get(application_id)
    if approved["profile_hash"] != item["profile_hash"]:
        raise PermissionError("Queued profile version changed; rebuild before preparation")
    context = {
        "review_type": "job_fit",
        "prompt_version": PROMPT_VERSION,
        "profile": {
            key: approved["profile"][key]
            for key in ("identity", "education", "eligibility", "availability", "preferences")
        },
        "career_evidence": asyncio.run(career_evidence("skills experience projects")),
        "expected_job_title": item["title"],
        "job_url": page["url"],
        "job_text": (posting_text or page.get("text", "").split("Apply for this job")[0])[:12000],
        "form_questions": [f["label"][:120] for f in page.get("fields", []) if f.get("label")][:60],
    }
    context_hash = fingerprint(context)
    directory = state_root() / "applications" / application_id
    path = directory / "job-review.json"
    if path.exists():
        prior = json.loads(path.read_text())
        if prior.get("context_hash") == context_hash and prior.get("qwen_output"):
            current = {**prior, **evaluate_review(prior["qwen_output"], approved["profile"])}
            if current["decision"] != prior.get("decision"):
                current["note"] = "Re-evaluated by updated code rules; Qwen output unchanged"
                write_private(path, current)
                workflow.record(application_id, "qwen_job_review", current)
                workflow.flush_events(application_id)
            return current
    try:
        generated = generate(directory, context, "job-reasoning")
        parsed = JobReview.model_validate_json(strip_fence(completed_response(generated)))
    except Exception as error:
        workflow.record(
            application_id,
            "qwen_failure",
            {"phase": "job_fit_review", "reason": type(error).__name__ + ": " + str(error)[:300]},
        )
        workflow.flush_events(application_id)
        raise
    qwen_output = parsed.model_dump()
    result = {"qwen_output": qwen_output, **evaluate_review(qwen_output, approved["profile"])}
    result.update(
        context_hash=context_hash,
        prompt_version=PROMPT_VERSION,
        profile_hash=approved["profile_hash"],
        model=generated["model"],
        harness="Hermes",
    )
    write_private(path, result)
    workflow.record(application_id, "qwen_job_review", result)
    workflow.flush_events(application_id)
    return result


def parse_review(raw: str, observed_keys: set[str]) -> dict:
    result = Review.model_validate_json(strip_fence(raw)).model_dump()
    seen = set()
    for answer in result["answers"]:
        if answer["key"] not in observed_keys or answer["key"] in seen:
            raise ValueError("Qwen referenced an unknown or repeated question")
        seen.add(answer["key"])
        if answer["kind"] == "needs_user" and answer["value"]:
            raise ValueError("An unknown fact cannot carry an application answer")
        if answer["kind"] == "proposal" and (not answer["value"] or not answer["sources"]):
            raise ValueError("A proposal needs a value and evidence references")
    if seen != observed_keys:
        raise ValueError("Qwen omitted an unresolved question")
    return result


def review_application(application_id: str, page: dict) -> dict:
    directory = state_root() / "applications" / application_id
    approved = read_approved()
    if page["profile_hash"] != approved["profile_hash"]:
        raise PermissionError("Application and approved profile versions differ")
    questions = [q for q in page.get("pending", []) if q.get("key")]
    if not questions:
        return {"answers": []}
    profile = approved["profile"]
    context = {
        "application_id": application_id,
        "prompt_version": PROMPT_VERSION,
        "questions": questions,
        "intake_source": workflow.get(application_id)["source"],
        "source_meaning": "keryx means discovered automatically in the Keryx GitHub jobs feed; this is trusted intake metadata, not a claimed employee referral",
        "intake_url": workflow.get(application_id)["source_url"],
        "profile": {
            key: profile[key]
            for key in [
                "identity",
                "education",
                "eligibility",
                "availability",
                "preferences",
                "stories",
                "application_policy",
            ]
        },
        "career_evidence": asyncio.run(career_evidence("TransferTrack OBI PayPals")),
        "owner_answers": workflow.approved_answers(application_id),
        "job_context": page.get("text", "")[:5500],
    }
    directions = directory / "owner-context.json"
    if directions.exists():
        context["owner_directions"] = json.loads(directions.read_text())
    context_hash = fingerprint(context)
    cached_path = directory / "answer-proposals.json"
    if cached_path.exists():
        cached = json.loads(cached_path.read_text())
        if cached.get("context_hash") == context_hash:
            return cached
    try:
        generated = generate(directory, context, "reasoning")
        result = parse_review(completed_response(generated), {q["key"] for q in questions})
    except Exception as error:
        workflow.record(
            application_id,
            "qwen_failure",
            {"phase": "answer_drafting", "reason": type(error).__name__ + ": " + str(error)[:300]},
        )
        workflow.flush_events(application_id)
        raise
    result.update(
        approved=False,
        model=generated["model"],
        harness="Hermes",
        profile_hash=page["profile_hash"],
        context_hash=context_hash,
        prompt_version=PROMPT_VERSION,
    )
    for answer in result["answers"]:
        answer["proposal_hash"] = fingerprint(
            {"application_id": application_id, "context_hash": context_hash, **answer}
        )
        if answer["kind"] == "proposal":
            answer["approve_command"] = (
                f"use {application_id} {answer['key']} {answer['proposal_hash']}"
            )
    write_private(directory / "answer-proposals.json", result)
    for answer in result["answers"]:
        workflow.record(
            application_id,
            "qwen_answer_proposal" if answer["kind"] == "proposal" else "qwen_question",
            answer,
        )
    workflow.flush_events(application_id)
    return result
