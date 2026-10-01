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
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import unslop, workflow
from .evidence import career_evidence
from .onboarding import read_approved
from .runtime import state_root, write_private

# Bump when the prompts in scripts/recruiting_reasoning.py change so cached
# reviews produced by an older prompt are never reused silently.
PROMPT_VERSION = "2026-09-30.4"
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
    requirement: str = Field(min_length=1, max_length=300)
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


def load_json(text: str):
    """Parse model JSON leniently: raw newlines inside strings are common in local output."""
    try:
        return json.loads(strip_fence(text), strict=False)
    except json.JSONDecodeError as error:
        raise ValueError(f"Qwen returned invalid JSON: {error.msg} at char {error.pos}") from error


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


# The local server holds 16K tokens; leave room for the system prompt and 2K of output.
INPUT_BUDGET_CHARS = 40_000


def fit_budget(context: dict, limit: int = INPUT_BUDGET_CHARS) -> dict:
    """Keep a request inside the model's window by trimming long text, largest first."""
    context = json.loads(json.dumps(context))
    trimmable = ("job_text", "job_context")

    def size() -> int:
        return len(json.dumps(context))

    while size() > limit:
        excerpts = [
            item
            for item in (context.get("career_evidence") or {}).get("results", [])
            if isinstance(item, dict) and len(item.get("excerpt", "")) > 1500
        ]
        texts = [k for k in trimmable if isinstance(context.get(k), str) and len(context[k]) > 1000]
        longest_excerpt = max(excerpts, key=lambda i: len(i["excerpt"]), default=None)
        longest_text = max(texts, key=lambda k: len(context[k]), default=None)
        if longest_excerpt is not None and (
            longest_text is None or len(longest_excerpt["excerpt"]) >= len(context[longest_text])
        ):
            longest_excerpt["excerpt"] = longest_excerpt["excerpt"][
                : int(len(longest_excerpt["excerpt"]) * 0.7)
            ]
        elif longest_text is not None:
            context[longest_text] = context[longest_text][: int(len(context[longest_text]) * 0.7)]
        else:
            break
    return context


class ModelUnavailable(RuntimeError):
    """The local model server is down; the queue waits instead of failing applications."""


def ensure_model():
    from .runtime import client

    def alive() -> bool:
        try:
            with client() as c:
                return c.get("/models", timeout=5).is_success
        except httpx.HTTPError:
            return False

    if alive():
        return
    launcher = Path.home() / ".omlx/bin/omlx"
    if launcher.is_file():
        subprocess.run([str(launcher), "start"], check=False, capture_output=True, timeout=60)
        for _ in range(60):
            time.sleep(1)
            if alive():
                return
    raise ModelUnavailable("Local model server is not running")


def generate(directory: Path, context: dict, basename: str, attempts: int = 2) -> dict:
    ensure_model()
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
            # Keep the last meaningful line only; owner cards never carry file paths.
            lines = [
                line.strip()
                for line in run.stderr.strip().splitlines()
                if line.strip() and not line.lstrip().startswith(("File ", "Traceback", "^"))
            ]
            failure = (
                f"harness exit {run.returncode}: " + (lines[-1] if lines else "no output")[:200]
            )
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


def month_evidenced(month: str, requirement: str, posting: str) -> bool:
    """A window month must appear in the posting; Qwen may not derive one from a phrase."""
    from datetime import UTC, datetime

    when = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    haystack = (requirement + "\n" + posting).lower()
    spelled = [when.strftime(f).lower() for f in ("%B %Y", "%b %Y", "%B, %Y", "%m/%Y", "%-m/%Y")]
    return any(text in haystack for text in spelled) or str(when.year) in requirement


# A class standing names when the applicant graduates relative to the internship summer:
# a rising senior in summer Y graduates between December Y and August Y+1.
CLASS_STANDING_MONTHS = {
    "rising senior": (5, 15),
    "rising junior": (17, 27),
    "rising sophomore": (29, 39),
}


def internship_year(text: str) -> int:
    """The internship's calendar year from the posting, else the next summer."""
    from datetime import UTC, datetime

    match = re.search(
        r"(?:summer|spring|fall|winter|intern[a-z]*)\D{0,20}(20\d\d)", text, re.IGNORECASE
    )
    if match:
        return int(match.group(1))
    today = datetime.now(UTC)
    return today.year + 1 if today.month >= 8 else today.year


def class_standing(
    requirement: str, graduation: str | None, posting: str
) -> tuple[str, str] | None:
    """Code's verdict on "rising senior" style requirements, or None when not applicable."""
    match = re.search(r"rising (senior|junior|sophomore)", requirement, re.IGNORECASE)
    if not match or not graduation:
        return None
    low, high = CLASS_STANDING_MONTHS["rising " + match.group(1).lower()]
    year = internship_year(requirement + "\n" + posting)
    grad_year, grad_month = (int(x) for x in graduation.split("-"))
    months_after_june = (grad_year - year) * 12 + (grad_month - 6)
    if low <= months_after_june <= high:
        return (
            "satisfied",
            f"Approved graduation {graduation} makes a {match.group(0).lower()} in summer {year}",
        )
    if months_after_june < 0:
        return (
            "conflict",
            f"Approved graduation {graduation} is before the summer {year} internship",
        )
    return (
        "unknown",
        f"Approved graduation {graduation} is outside the usual {match.group(0).lower()} range for summer {year}; owner review",
    )


def evaluate_requirements(requirements: list[dict], profile: dict, posting: str = "") -> list[dict]:
    """Deterministic checks for exact facts; Qwen's judgment stands only where code cannot."""
    education = profile["education"]["schools"]
    graduation = next((s["graduation_month"] for s in education if s["graduation_month"]), None)
    eligible = profile["eligibility"]
    checked = []
    for item in requirements:
        entry = {**item, "checked_by": "qwen"}
        standing = class_standing(item.get("requirement", ""), graduation, posting)
        if standing:
            entry["status"], entry["note"] = standing
            entry["checked_by"] = "code" if standing[0] != "unknown" else "qwen"
        elif item["kind"] == "graduation_window" and not re.search(
            GRADUATION_WORDS, item.get("requirement", ""), re.IGNORECASE
        ):
            # An internship term ("Winter/Spring 2027") is not a graduation requirement.
            entry["kind"] = "dates"
            entry["status"] = "unknown"
            entry["note"] = "Stated as a term, not as a graduation requirement"
        elif item["kind"] == "graduation_window":
            months = [m for m in (item.get("graduation_start"), item.get("graduation_end")) if m]
            evidenced = all(
                month_evidenced(m, item.get("requirement", ""), posting) for m in months
            )
            inside = (
                None
                if graduation is None or not months or not evidenced
                else months_inclusive(
                    graduation, item.get("graduation_start"), item.get("graduation_end")
                )
            )
            if months and not evidenced:
                entry["status"] = "unknown"
                entry["note"] = "Window months are not stated in the posting; owner review"
            elif inside is None:
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
        elif item["kind"] == "degree":
            majors = " ".join(
                (school.get("major") or "") + " " + (school.get("degree") or "")
                for school in education
            ).lower()
            computing = any(
                term in majors
                for term in ("comput", "software", "information technology", "data science")
            )
            broad = re.search(
                r"computer science|computing|software|\bstem\b|related (?:field|discipline"
                r"|major|area|degree)|technical (?:field|degree|discipline)"
                r"|information (?:systems|technology)",
                item.get("requirement", ""),
                re.IGNORECASE,
            )
            if computing and broad:
                entry["status"] = "satisfied"
                entry["checked_by"] = "code"
                entry["note"] = (
                    "Approved major is a computing degree; "
                    "the posting accepts computing or related fields"
                )
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
    "degree": r"degree|major|field of study|related field|computer science|qualif",
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


# Requirement kinds that decide eligibility. Skills and other wishes are for the resume
# and the written answers, never a gate: an unknown skill must not stop an application.
GATE_KINDS = {
    "program",
    "graduation_window",
    "work_authorization",
    "sponsorship",
    "location",
    "degree",
}
GRADUATION_WORDS = (
    r"graduat|class of|degree (?:by|in|expected|completion)|expected to graduate|completion of"
)


def decide(requirements: list[dict]) -> str:
    gates = [r for r in requirements if r.get("kind") in GATE_KINDS]
    if any(r["status"] == "conflict" and r["checked_by"] == "code" for r in gates):
        return "not_fit"
    if any(r["status"] == "conflict" for r in gates):
        return "needs_review"
    # Unknown skills are the resume's job. Eligibility the posting states but code cannot
    # verify is listed on the submit card, so the owner decides once, at submit time.
    return "fit"


def unverified(requirements: list[dict]) -> list[str]:
    return [
        r["requirement"][:160]
        for r in requirements
        if r.get("kind") in GATE_KINDS and r["status"] == "unknown"
    ]


def evaluate_review(qwen_output: dict, profile: dict, posting: str = "") -> dict:
    """Code's verdict over Qwen's extraction; rerunnable whenever the rules improve."""
    requirements = evaluate_requirements(
        [dict(item) for item in qwen_output["requirements"]], profile, posting
    )
    requirements, kept, resolved = merge_unknowns(requirements, list(qwen_output["unknowns"]))
    return {
        "decision": decide(requirements),
        "unverified": unverified(requirements),
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
    context = fit_budget(context)
    # Form labels help Qwen tell questions from requirements but must not force a new
    # model call whenever an observer improvement changes how a label is read.
    context_hash = fingerprint({k: v for k, v in context.items() if k != "form_questions"})
    directory = state_root() / "applications" / application_id
    path = directory / "job-review.json"
    if path.exists():
        prior = json.loads(path.read_text())
        if prior.get("context_hash") == context_hash and prior.get("qwen_output"):
            current = {
                **prior,
                **evaluate_review(prior["qwen_output"], approved["profile"], context["job_text"]),
            }
            if current["decision"] != prior.get("decision"):
                current["note"] = "Re-evaluated by updated code rules; Qwen output unchanged"
                write_private(path, current)
                workflow.record(application_id, "qwen_job_review", current)
                workflow.flush_events(application_id)
            return current
    try:
        generated, parsed = None, None
        for attempt in range(2):
            generated = generate(directory, context, "job-reasoning")
            try:
                parsed = JobReview.model_validate(load_json(completed_response(generated)))
                break
            except (ValueError, ValidationError) as error:
                if attempt:
                    raise
                # One retry with the defect named; a second bad answer is a failure.
                context = {**context, "previous_output_problem": str(error)[:300]}
    except Exception as error:
        workflow.record(
            application_id,
            "qwen_failure",
            {"phase": "job_fit_review", "reason": type(error).__name__ + ": " + str(error)[:300]},
        )
        workflow.flush_events(application_id)
        raise
    qwen_output = parsed.model_dump()
    result = {
        "qwen_output": qwen_output,
        **evaluate_review(qwen_output, approved["profile"], context["job_text"]),
    }
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


def polish(directory: Path, key: str, value: str) -> dict:
    """Unslop pass over one draft: scan, one bounded Qwen repair, re-scan, keep facts."""
    report = unslop.scan(value)
    if not unslop.needs_cleanup(report):
        return {"unslop": unslop.summary(report, None), "unslop_report": report}
    try:
        generated = generate(directory, unslop.cleanup_context(value, report), f"cleanup-{key}", 1)
        cleaned = strip_fence(completed_response(generated)).strip().strip('"')
    except (RuntimeError, ValueError) as error:
        return {"unslop": ("cleanup skipped: " + str(error))[:300], "unslop_report": report}
    if not cleaned or len(cleaned) > 3000 or not unslop.preserved_facts(value, cleaned):
        return {
            "unslop": "cleanup rejected (facts or length changed); original kept. "
            + unslop.summary(report, None),
            "unslop_report": report,
        }
    after = unslop.scan(cleaned)
    return {
        "value": cleaned,
        "original_value": value,
        "unslop": unslop.summary(report, after),
        "unslop_report": {"before": report, "after": after},
    }


def parse_review(raw: str, observed_keys: set[str], options_by_key: dict | None = None) -> dict:
    result = Review.model_validate(load_json(raw)).model_dump()
    seen = set()
    for answer in result["answers"]:
        options = (options_by_key or {}).get(answer["key"]) or []
        if answer["kind"] == "proposal" and options:
            wanted = " ".join(re.findall(r"[a-z0-9]+", answer["value"].lower()))
            if wanted not in {" ".join(re.findall(r"[a-z0-9]+", o.lower())) for o in options}:
                # A made-up option never reaches the form; the owner picks from the list.
                answer.update(
                    kind="needs_user",
                    value="",
                    explanation=(
                        f"Qwen proposed '{answer['value'][:80]}', which is not one of the options; "
                        "choose one of: " + ", ".join(str(o) for o in options[:10])
                    )[:1000],
                )
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


def length_problems(result: dict, questions: list) -> str:
    """A draft must fit the field it is for; the site truncates silently otherwise."""
    limits = {q["key"]: q.get("max_chars") for q in questions}
    problems = []
    for answer in result.get("answers", []):
        if answer.get("kind") != "proposal":
            continue
        limit, value = limits.get(answer["key"]), answer.get("value", "")
        if limit and len(value) > int(limit):
            problems.append(
                f"answer {answer['key']} is {len(value)} characters; the field allows {limit}"
            )
        elif len(value.split()) > 150:
            problems.append(
                f"answer {answer['key']} is {len(value.split())} words; keep it under 130"
            )
    return "; ".join(problems)[:300]


def shorten_to_fit(result: dict, questions: list):
    limits = {q["key"]: q.get("max_chars") for q in questions}
    for answer in result.get("answers", []):
        if answer.get("kind") != "proposal":
            continue
        limit = int(limits.get(answer["key"]) or 0) or 900
        if len(answer.get("value", "")) > limit:
            answer["value"] = workflow.brief(answer["value"], limit)
            answer["explanation"] = (
                str(answer.get("explanation", ""))[:200]
                + f" Shortened to fit the field's {limit}-character limit."
            ).strip()


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
    context = fit_budget(context)
    context_hash = fingerprint(context)
    cached_path = directory / "answer-proposals.json"
    if cached_path.exists():
        cached = json.loads(cached_path.read_text())
        if cached.get("context_hash") == context_hash:
            return cached
    try:
        generated, result = None, None
        for attempt in range(2):
            generated = generate(directory, context, "reasoning")
            try:
                result = parse_review(
                    completed_response(generated),
                    {q["key"] for q in questions},
                    {q["key"]: q.get("options") or [] for q in questions},
                )
                problems = length_problems(result, questions)
                if problems and not attempt:
                    context = {**context, "previous_output_problem": problems}
                    continue
                if problems:
                    shorten_to_fit(result, questions)
                break
            except (ValueError, ValidationError) as error:
                if attempt:
                    raise
                context = {**context, "previous_output_problem": str(error)[:300]}
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
    labels = {q["key"]: q.get("label", "") for q in questions}
    for answer in result["answers"]:
        if answer["kind"] == "proposal" and len(answer["value"]) > 60:
            # Written answers get the Unslop pass before they are hashed for approval.
            answer.update(polish(directory, answer["key"], answer["value"]))
    shorten_to_fit(result, questions)
    number = 0
    for answer in result["answers"]:
        answer["proposal_hash"] = fingerprint(
            {"application_id": application_id, "context_hash": context_hash, **answer}
        )
        answer["label"] = labels.get(answer["key"], "")
        if answer["kind"] == "proposal":
            # The draft's number is how the owner names it; it is not part of the hash.
            number += 1
            answer["number"] = number
            answer["approve_command"] = f"use draft {number}"
    write_private(directory / "answer-proposals.json", result)
    for answer in result["answers"]:
        workflow.record(
            application_id,
            "qwen_answer_proposal" if answer["kind"] == "proposal" else "qwen_question",
            answer,
        )
    workflow.flush_events(application_id)
    return result
