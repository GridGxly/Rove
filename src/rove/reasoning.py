"""Local Qwen/Hermes proposals, never an authorization or candidate-fact writer.

Qwen interprets postings and drafts answers. Trusted code owns exact comparisons
(dates, approved booleans), output validation, caching, and the final decision.
"""

import asyncio
import copy
import functools
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import draft_guard, fastpath, model_client, postings, timing, unslop, vault, workflow
from .evidence import career_evidence
from .model_client import ModelUnavailable, PromptTooLong
from .onboarding import read_approved
from .research import company_context
from .research import quoted as quoted_research
from .runtime import MODEL, state_root, write_private

# The prompts in scripts/recruiting_reasoning.py are versioned apart, so work done under
# an older prompt is never reused silently and work done under an unchanged one is kept.
# Bump FIT_PROMPT_VERSION when JOB_FIT_PROMPT changes: stored job-fit reviews are keyed by
# it and every job is reviewed again. Bump ANSWERS_PROMPT_VERSION when ANSWER_PROMPT or
# the writing rules change: cached drafts are keyed by it, stored fit reviews are not.
FIT_PROMPT_VERSION = "2026-10-03.1"
ANSWERS_PROMPT_VERSION = "2026-10-03.1"
# The drafting version under its earlier name, for existing callers.
PROMPT_VERSION = ANSWERS_PROMPT_VERSION
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
REPOSITORY = Path(__file__).resolve().parents[2]
# The Hermes path puts its own system prompt in front of Rove's.
HERMES_PROMPT_TOKENS = 600


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-f0-9]{12}$")
    kind: Literal["proposal", "needs_user"]
    # A proposal carries a value and sources; a needs_user answer only its short reason.
    value: str = Field(default="", max_length=3000)
    sources: list[str] = Field(default_factory=list, max_length=10)
    explanation: str = Field(default="", max_length=1000)


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
    # In English; `original` keeps a non-English posting's own words beside it.
    requirement: str = Field(min_length=1, max_length=300)
    original: str = Field(default="", max_length=400)
    # Only on a conflict: the approved profile field it contradicts, as a path.
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


@functools.cache
def prompts():
    """The prompt texts, from the one file both transports read them from."""
    spec = importlib.util.spec_from_file_location(
        "recruiting_reasoning", REPOSITORY / "scripts/recruiting_reasoning.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def system_prompt(kind) -> str:
    return prompts().system_prompt(kind)


# Page text goes first when a prompt is over its budget, then evidence excerpts.
PAGE_TEXT = ("job_context", "job_text")


def fit_budget(
    context: dict, system: str = "", tokens: int = model_client.PROMPT_TOKEN_BUDGET
) -> dict:
    """Keep a request inside the model's window: page text is trimmed before evidence.

    Sizes are estimated in tokens (`model_client.estimate_tokens`, which errs high) and
    include the system prompt. What cannot be trimmed stays; the caller refuses to send a
    prompt that is still over.
    """
    context = json.loads(json.dumps(context))

    def over() -> bool:
        return model_client.prompt_tokens(system, context) > tokens

    while over():
        texts = [k for k in PAGE_TEXT if isinstance(context.get(k), str) and len(context[k]) > 500]
        research = context.get("company_research")
        quotes = research.get("quotes") if isinstance(research, dict) else None
        if texts:
            longest = max(texts, key=lambda k: len(context[k]))
            context[longest] = context[longest][: int(len(context[longest]) * 0.7)]
        elif isinstance(quotes, list) and quotes:
            research["quotes"] = quotes[: int(len(quotes) * 0.7)]
        else:
            excerpts = [
                item
                for item in (context.get("career_evidence") or {}).get("results", [])
                if isinstance(item, dict) and len(item.get("excerpt", "")) > 1500
            ]
            if not excerpts:
                break
            longest_excerpt = max(excerpts, key=lambda i: len(i["excerpt"]))
            longest_excerpt["excerpt"] = longest_excerpt["excerpt"][
                : int(len(longest_excerpt["excerpt"]) * 0.7)
            ]
    return context


def static_first(system: str, static: dict, dynamic: dict) -> dict:
    """A context whose leading part is the same for every job of a profile version.

    The server reuses a prompt prefix in whole 2,048-token blocks, so the system prompt,
    profile, evidence and voice note go first and the job's own text after them.
    `cache_padding` (digits, one token each) marks where the shared part ends. Here it
    holds what an estimate says is missing for one whole block; the request replaces it
    with the server's exact count (`pad_to_block`).
    """
    context = dict(static)
    estimate = model_client.at_least_tokens(system + json.dumps(context, ensure_ascii=False))
    context["cache_padding"] = "0" * model_client.padding_for(estimate, exact=False)
    context.update(dynamic)
    return context


def pad_to_block(system: str, context: dict) -> dict:
    """The context with each shared part padded on the server's own count of it, so the
    shared part fills whole cache blocks. A `cache_padding…` key ends each shared part:
    the one every job shares, then (for a form drafted in batches) the part every batch
    of one application shares. Unchanged where the server cannot count; an empty padding
    is dropped."""
    padded = dict(context)
    for marker in [k for k in context if k.startswith("cache_padding")]:
        keys = list(padded)
        before = {k: padded[k] for k in keys[: keys.index(marker)]}
        tokens = model_client.prefix_tokens(system, json.dumps(before, ensure_ascii=False))
        if tokens is not None:
            padded[marker] = "0" * model_client.padding_for(tokens, exact=True)
        if not padded[marker]:
            del padded[marker]
    return padded


# One read of Erga's approved evidence serves the fit review and the drafting of a pass,
# and the same excerpts make the same leading cache block for every job. Inside a pass the
# reads share the pass's one Erga process (erga_session); outside it each starts its own.
EVIDENCE_QUERY = "skills experience projects TransferTrack OBI PayPals"


def approved_evidence() -> dict:
    return asyncio.run(career_evidence(EVIDENCE_QUERY))


def model_alive() -> bool:
    from .runtime import client

    try:
        with client() as c:
            return c.get("/models", timeout=5).is_success
    except httpx.HTTPError:
        return False


def ensure_model():
    if model_alive():
        return
    launcher = Path.home() / ".omlx/bin/omlx"
    if launcher.is_file():
        subprocess.run([str(launcher), "start"], check=False, capture_output=True, timeout=60)
        for _ in range(60):
            time.sleep(1)
            if model_alive():
                return
    raise ModelUnavailable("Local model server is not running")


def transport() -> str:
    """`direct` (the default) or `hermes`, from `model_transport` in the workflow config."""
    return "hermes" if workflow.config().get("model_transport") == "hermes" else "direct"


def model_facts(generated) -> dict:
    facts = timing.tokens(generated)
    if isinstance(generated, dict) and generated.get("transport"):
        facts["transport"] = str(generated["transport"])
    return facts


def words_asked(question: dict) -> int:
    """How many words a written answer may run to: a number the question names, else what
    its character limit holds, else the drafting prompt's own ceiling."""
    label = str(question.get("label", ""))
    named = re.search(r"\b(\d{2,3})\s*(?:words|word limit)", label, re.IGNORECASE)
    if named:
        return min(int(named.group(1)), 400)
    if question.get("max_chars"):
        return max(10, min(int(question["max_chars"]) // 6, WORD_CEILING))
    return WORD_CEILING


def expected_output(question: dict) -> int:
    """Output tokens one answer may take, erring high.

    Measured on drafting replays: an answer's JSON (key, kind, sources or reason) runs
    about 40 to 60 tokens at about 2.8 characters a token; prose about 1.45 tokens a
    word, and a draft runs up to twice its word target before code shortens it.
    """
    control = {"kind": question["control"]} if question.get("control") else None
    kind = fastpath.question_kind(question, control)
    if kind == "writing":
        return 45 + int(2.9 * words_asked(question))
    return ANSWER_TOKENS[kind]


ANSWER_TOKENS = {"owner": 40, "short": 60, "choice": 60}
# Under this much expected output a form is one request; the server's ceiling is 2,048.
OUTPUT_SPLIT_TOKENS = 1600


def question_batches(questions: list) -> list[list]:
    """The fewest runs of questions, in form order, each expected to fit under
    OUTPUT_SPLIT_TOKENS; usually one."""
    batches: list[list] = [[]]
    size = 20  # the {"answers": [...]} around them
    for question in questions:
        need = expected_output(question)
        if batches[-1] and size + need > OUTPUT_SPLIT_TOKENS:
            batches.append([])
            size = 20
        batches[-1].append(question)
        size += need
    return batches


def generate(directory: Path, context: dict, basename: str, attempts: int = 2) -> dict:
    """The model's answer to one structured prompt, through the configured transport.

    A drafting context whose answers may not fit under the output ceiling goes as the
    fewest requests that do, and their answers come back as one.
    """
    questions = context.get("questions")
    if isinstance(questions, list) and questions:
        batches = question_batches(questions)
        if len(batches) > 1:
            return generate_batches(directory, context, basename, attempts, batches)
    return request(directory, context, basename, attempts)


def generate_batches(
    directory: Path, context: dict, basename: str, attempts: int, batches: list[list]
) -> dict:
    answers: list = []
    usage: dict = {}
    generated: dict = {}
    # Everything but the questions is the same in every batch: it is padded to a cache
    # block too, so the second batch on reads only its own questions.
    shared = {k: v for k, v in context.items() if k != "questions"}
    tail = {k: shared.pop(k) for k in list(shared) if k == "previous_output_problem"}
    for number, batch in enumerate(batches, start=1):
        part = {**shared, "cache_padding_application": "", "questions": batch, **tail}
        name = basename if number == 1 else f"{basename}-{number}"
        generated = request(directory, part, name, attempts)
        try:
            parsed = load_json(completed_response(generated))
        except (RuntimeError, ValueError):
            return generated  # the caller's own check names the defect
        if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), list):
            return generated
        answers += parsed["answers"]
        for name, value in ((generated.get("result") or {}).get("usage") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage[name] = usage.get(name, 0) + value
    merged = copy.deepcopy(generated)
    merged["batches"] = len(batches)
    merged["result"]["final_response"] = json.dumps({"answers": answers}, ensure_ascii=False)
    merged["result"]["usage"] = usage
    return merged


@timing.call("model", result=model_facts)
def request(directory: Path, context: dict, basename: str, attempts: int = 2) -> dict:
    """One model request. A prompt over the token budget is trimmed, and never sent when
    trimming cannot bring it under."""
    system = system_prompt(context.get("review_type"))
    hermes = transport() == "hermes"
    budget = model_client.PROMPT_TOKEN_BUDGET - (HERMES_PROMPT_TOKENS if hermes else 0)
    ensure_model()
    # The Hermes prompt comes first, so no padding of Rove's part lines up with a block.
    context = (
        {k: v for k, v in context.items() if not k.startswith("cache_padding")}
        if hermes
        else pad_to_block(system, context)
    )
    context = fit_budget(context, system, budget)
    write_private(directory / f"{basename}-input.json", context)
    if model_client.prompt_tokens(system, context) > budget:
        raise PromptTooLong(
            f"The {context.get('review_type') or 'answers'} prompt is over the model's "
            f"{model_client.PROMPT_TOKEN_BUDGET}-token budget even after trimming"
        )
    try:
        if hermes:
            return hermes_request(directory, basename, attempts)
        return direct_request(directory, system, context, basename)
    finally:
        model_client.mark_used()


def direct_request(directory: Path, system: str, context: dict, basename: str) -> dict:
    reply = model_client.chat(
        system,
        model_client.user_content(context),
        expects_json=context.get("review_type") != "cleanup",
    )
    finished = reply["finish_reason"] == "stop"
    generated = {
        "model": MODEL,
        "harness": "direct",
        "transport": "direct",
        "tool_count": 0,
        "streaming": True,
        "result": {
            "completed": finished,
            "turn_exit_reason": "text_response(finish_reason=stop)"
            if finished
            else f"output_stopped({reply['finish_reason']})",
            "final_response": reply["content"],
            "usage": reply["usage"],
            "api_calls": 1,
            "seconds": reply["seconds"],
        },
    }
    write_private(directory / f"{basename}-result.json", generated)
    return generated


def hermes_request(directory: Path, basename: str, attempts: int) -> dict:
    """The review inside the installed Hermes harness (`model_transport: "hermes"`)."""
    python = workflow.config().get("hermes_python")
    if not python or not Path(python).is_file():
        raise ValueError("Configure the installed Hermes Python path before running Qwen")
    failure = "not started"
    for _ in range(attempts):
        run = subprocess.run(
            [
                python,
                str(REPOSITORY / "scripts/recruiting_reasoning.py"),
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
            env=dict(os.environ, PYTHONPATH=str(REPOSITORY / "src"), HERMES_AUTOPILOT_16K="1"),
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
        return {**generated, "transport": "hermes"}
    if not model_alive():
        # The server went away under the harness: the queue waits, nothing failed.
        raise ModelUnavailable("The model server stopped answering: " + failure)
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


@timing.stage(None, "fit_review")
def review_job(application_id: str, page: dict, posting_text: str = "") -> dict:
    """Qwen extracts requirements before any applicant data enters the form.

    One model call per job. The review is stored under the posting's content, the approved
    profile snapshot and the fit prompt's version; a later tick, a resume after a hold or a
    reopen reads it back, and only a change to one of the three asks Qwen again. A change
    to the drafting prompt does not. Code's own comparisons run on every read, so an
    improved rule applies without a model call.
    """
    approved = read_approved()
    item = workflow.get(application_id)
    if approved["profile_hash"] != item["profile_hash"]:
        raise PermissionError("Queued profile version changed; rebuild before preparation")
    job_text = (posting_text or page.get("text", "").split("Apply for this job")[0])[:12000]
    cache_key = fastpath.review_key(job_text, approved["profile_hash"], FIT_PROMPT_VERSION)
    directory = state_root() / "applications" / application_id
    path = directory / "job-review.json"
    keep_posting(application_id, job_text, page.get("url") or item["url"])
    prior = fastpath.stored_review(application_id, cache_key)
    board = postings.kept(application_id)
    if not prior and postings.from_board(board):
        # A review made before the browser opened, from the board's own copy of the
        # posting, stands while the board still shows that posting word for word.
        board_key = fastpath.review_key(board["text"], *cache_key[1:])
        candidate = fastpath.stored_review(application_id, board_key)
        if candidate and postings.still_current(application_id, page.get("url")):
            prior, job_text, cache_key = candidate, board["text"], board_key
            timing.note(board=board["source"])
    timing.note(cached=bool(prior))
    if prior:
        timing.note(background=bool(prior.get("background")))
        current = {
            **prior,
            **evaluate_review(prior["qwen_output"], approved["profile"], job_text),
        }
        changed = current["decision"] != prior.get("decision")
        if changed:
            current["note"] = "Re-evaluated by updated code rules; Qwen output unchanged"
            write_private(path, current)
            fastpath.store_review(application_id, cache_key, current)
        if changed or not fit_card_posted(application_id):
            # A review made in the background reaches the thread with its first pass.
            workflow.record(application_id, "qwen_job_review", current)
            workflow.flush_events(application_id)
        return {**current, "cached": True}
    timing.note(changed=fastpath.review_change(application_id, cache_key))
    # Form labels help Qwen tell questions from requirements. They are not part of the
    # stored review's key: how a label is read must not ask Qwen again.
    labels = [f["label"][:120] for f in page.get("fields", []) if f.get("label")][:60]
    try:
        result = fit_review(application_id, approved, item, job_text, page["url"], labels)
    except ModelUnavailable:
        raise  # the queue waits for the server; nothing to tell the owner
    except Exception as error:
        workflow.record(
            application_id,
            "qwen_failure",
            {"phase": "job_fit_review", "reason": type(error).__name__ + ": " + str(error)[:300]},
        )
        workflow.flush_events(application_id)
        raise
    # Stored before anything is posted: a Discord failure must not cost a second review.
    fastpath.store_review(application_id, cache_key, result)
    write_private(path, result)
    workflow.record(application_id, "qwen_job_review", result)
    workflow.flush_events(application_id)
    return result


def fit_context(approved: dict, item: dict, job_text: str, url: str, labels: list) -> dict:
    """The fit review's input: what every job shares first, this job after it."""
    static = {
        "review_type": "job_fit",
        "prompt_version": FIT_PROMPT_VERSION,
        "profile": {
            key: approved["profile"][key]
            for key in ("identity", "education", "eligibility", "availability", "preferences")
        },
        "career_evidence": approved_evidence(),
    }
    dynamic = {
        "expected_job_title": item["title"],
        "job_url": url,
        "job_text": job_text,
        "form_questions": labels,
    }
    return static_first(system_prompt("job_fit"), static, dynamic)


def fit_review(
    application_id: str, approved: dict, item: dict, job_text: str, url: str, labels: list
) -> dict:
    """One model review of a posting, checked by code. Stores and posts nothing."""
    directory = state_root() / "applications" / application_id
    context = fit_context(approved, item, job_text, url, labels)
    context_hash = fingerprint(
        {k: v for k, v in context.items() if k not in ("form_questions", "cache_padding")}
    )
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
    qwen_output = parsed.model_dump()
    return {
        "qwen_output": qwen_output,
        **evaluate_review(qwen_output, approved["profile"], job_text),
        "context_hash": context_hash,
        "posting_hash": fastpath.posting_hash(job_text),
        "prompt_version": FIT_PROMPT_VERSION,
        "profile_hash": approved["profile_hash"],
        "model": generated["model"],
        "harness": generated.get("harness", "Hermes"),
    }


def fit_card_posted(application_id: str) -> bool:
    with workflow.db() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM application_events WHERE application_id=? "
                "AND kind='qwen_job_review' LIMIT 1",
                (application_id,),
            ).fetchone()
        )


# The posting as it was keyed for the fit review, kept so a later review (a new profile
# version or fit prompt) can run before the browser opens again. A copy read from the
# board's own API (postings.py) is kept in the same file and is not replaced by the page.
POSTING_FILE = postings.POSTING_FILE


def keep_posting(application_id: str, text: str, url: str):
    if not text.strip():
        return
    current = postings.kept(application_id)
    if postings.from_board(current) or current.get("text") == text:
        return
    postings.keep(
        application_id,
        {"text": text, "source": "browser", "url": url, "captured_at": workflow.now()},
    )


def stored_posting(application_id: str) -> str:
    """The posting text kept for this application, or "" when none is kept yet.

    `posting.json` holds the text a fit review was keyed on. An application reviewed
    before that file existed has the text in its last fit review input instead.
    """
    directory = state_root() / "applications" / application_id
    for name, key in ((POSTING_FILE, "text"), ("job-reasoning-input.json", "job_text")):
        try:
            text = json.loads((directory / name).read_text()).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(text, str) and text.strip():
            return text[:12000]
    return ""


@timing.stage(None, "background_review")
def prereview_job(application_id: str, job_text: str) -> dict | None:
    """A fit review made while the worker waits, stored under the same key a pass reads.

    Nothing is posted: the review reaches the thread with the application's first pass,
    which uses it only when the posting it reads has this text's hash. None when the
    application is no longer queued under the approved profile, or already reviewed.
    """
    approved = read_approved()
    item = workflow.get(application_id)
    if item["status"] != "QUEUED" or approved["profile_hash"] != item["profile_hash"]:
        return None
    cache_key = fastpath.review_key(job_text, approved["profile_hash"], FIT_PROMPT_VERSION)
    if fastpath.stored_review(application_id, cache_key):
        return None
    timing.note(changed=fastpath.review_change(application_id, cache_key))
    result = fit_review(application_id, approved, item, job_text, item["url"], [])
    result["background"] = True
    fastpath.store_review(application_id, cache_key, result)
    write_private(state_root() / "applications" / application_id / "job-review.json", result)
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


def parse_review(
    raw: str,
    observed_keys: set[str],
    options_by_key: dict | None = None,
    asked: list | None = None,
) -> dict:
    """Validate Qwen's answers against the questions that were asked.

    `asked` is the list of pending questions (key, label, options). With it, code decides
    which questions a model may draft at all: a legal or personal question, or a field
    the form gave no label, comes back as "needs the owner" whatever the model said, with
    the reason in `gate` for the system log.
    """
    from .questions import draft_gate

    result = Review.model_validate(load_json(raw)).model_dump()
    by_key = {q.get("key"): q for q in asked or []}
    seen = set()
    for answer in result["answers"]:
        options = (options_by_key or {}).get(answer["key"]) or []
        gate = draft_gate(by_key.get(answer["key"]))
        if gate:
            answer.update(
                kind="needs_user",
                value="",
                sources=[],
                explanation=gate["words"],
                gate=gate["code"],
            )
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


WORD_CEILING = 130  # a draft past 150 words is cut to whole sentences under this


def shorten_to_fit(result: dict, questions: list):
    """Cut each over-length draft to whole leading sentences that fit its field, and its
    card says so. Code does this instead of asking the model to write it again."""
    limits = {q["key"]: q.get("max_chars") for q in questions}
    for answer in result.get("answers", []):
        if answer.get("kind") != "proposal":
            continue
        value = str(answer.get("value", ""))
        limit = int(limits.get(answer["key"]) or 0) or 900
        if len(value.split()) > 150:
            words = value.split()
            # The character budget of the first WORD_CEILING words, cut at a sentence.
            budget = len(" ".join(words[:WORD_CEILING]))
            shortened = workflow.brief(value, min(limit, budget))
            why = f"Shortened to {len(shortened.split())} words to fit."
        elif len(value) > limit:
            shortened = workflow.brief(value, limit)
            why = f"Shortened to fit the field's {limit}-character limit."
        else:
            continue
        answer["value"] = shortened
        answer["explanation"] = (str(answer.get("explanation", ""))[:200] + " " + why).strip()


def posting_text_for(directory: Path, page: dict) -> str:
    """The posting as the job-fit review saw it; the form page's own text otherwise."""
    path = directory / "job-reasoning-input.json"
    if path.is_file():
        try:
            text = json.loads(path.read_text()).get("job_text")
        except (ValueError, OSError):
            text = ""
        if text:
            return str(text)
    return str(page.get("text") or "")


def review_application(application_id: str, page: dict) -> dict:
    directory = state_root() / "applications" / application_id
    approved = read_approved()
    if page["profile_hash"] != approved["profile_hash"]:
        raise PermissionError("Application and approved profile versions differ")
    # A question whose text could not be read has nothing to draft from; it stays with the owner.
    questions = [q for q in page.get("pending", []) if q.get("key") and not q.get("label_missing")]
    if not questions:
        return {"answers": []}
    profile = approved["profile"]
    item = workflow.get(application_id)
    # What no written answer may carry: contact details, an undisclosed GPA, pay floors.
    # They are kept out of the context below and checked for in every draft after it.
    private = draft_guard.private_facts(profile, application_id)
    # What every job of this profile version shares goes first, so the server reuses it.
    static = {
        "review_type": "answers",
        "prompt_version": PROMPT_VERSION,
        "source_meaning": "keryx means discovered automatically in the Keryx GitHub jobs feed; this is trusted intake metadata, not a claimed employee referral",
        "profile": draft_guard.drafting_profile(profile),
        "career_evidence": draft_guard.scrub(approved_evidence(), private),
    }
    voice = vault.voice_samples()
    if voice:
        # The owner's own writing, bounded by the vault reader; a style sample the
        # prompt must not copy or cite, never a source of facts.
        static["owner_voice"] = draft_guard.scrub(voice, private)
    # The questions go last: a form drafted in batches shares everything before them.
    dynamic = {
        "application_id": application_id,
        "intake_source": item["source"],
        "intake_url": draft_guard.scrub(item["source_url"], private),
        "owner_answers": {
            key: answer
            for key, answer in workflow.approved_answers(application_id).items()
            if not draft_guard.private_fact_in(answer["value"], private)
        },
        # A form page can show the applicant's own details back (a review step).
        "job_context": draft_guard.scrub(page.get("text", "")[:5500], private),
    }
    research = quoted_research(
        company_context(application_id, posting_text_for(directory, page), item["url"])
    )
    workflow.record_research(application_id)  # one quiet thread line: pages read, no text
    if research:
        # Public text from the employer's own site, bounded, stripped of anything that
        # reads as an instruction and handed over as quoted sentences. It steers a "why
        # this company" draft; it is never a fact about the applicant.
        dynamic["company_research"] = research
    directions = directory / "owner-context.json"
    if directions.exists():
        dynamic["owner_directions"] = draft_guard.scrub(json.loads(directions.read_text()), private)
    # Each question says what control it is (a one-line field, a text area, a list), so
    # the draft fits it and the request can be sized by the answers it may need.
    controls = {
        f.get("key"): f.get("kind") for f in page.get("fields") or [] if isinstance(f, dict)
    }
    dynamic["questions"] = [
        {**q, "control": controls[q["key"]]} if controls.get(q["key"]) else q for q in questions
    ]
    context = static_first(system_prompt("answers"), static, dynamic)
    context_hash = fingerprint({k: v for k, v in context.items() if k != "cache_padding"})
    cached_path = directory / "answer-proposals.json"
    if cached_path.exists():
        cached = json.loads(cached_path.read_text())
        if cached.get("context_hash") == context_hash:
            return cached
    try:
        from .questions import real_options  # a select's "Select..." is no option to propose

        generated, result = None, None
        for attempt in range(2):
            generated = generate(directory, context, "reasoning")
            try:
                result = parse_review(
                    completed_response(generated),
                    {q["key"] for q in questions},
                    {q["key"]: real_options(q.get("options")) for q in questions},
                    questions,
                )
                leaks = draft_guard.problems(result, private, voice)
                if leaks and not attempt:
                    # Only a leak is worth a second full call. A draft that is too long
                    # is cut by code below, sentence by sentence, never re-drafted.
                    problems = length_problems(result, questions)
                    note = "; ".join(filter(None, [draft_guard.retry_note(leaks), problems]))
                    context = {**context, "previous_output_problem": note[:600]}
                    continue
                # A second draft that still carries a private fact is dropped, not sent.
                draft_guard.withhold(result, leaks)
                shorten_to_fit(result, questions)
                break
            except (ValueError, ValidationError) as error:
                if attempt:
                    raise
                context = {**context, "previous_output_problem": str(error)[:300]}
    except ModelUnavailable:
        raise  # the queue waits for the server; nothing to tell the owner
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
        harness=generated.get("harness", "Hermes"),
        profile_hash=page["profile_hash"],
        context_hash=context_hash,
        prompt_version=PROMPT_VERSION,
    )
    labels = {q["key"]: q.get("label", "") for q in questions}
    for answer in result["answers"]:
        if answer["kind"] == "proposal" and len(answer["value"]) > 60:
            # Written answers get the Unslop pass before they are hashed for approval.
            answer.update(polish(directory, answer["key"], answer["value"]))
    # The cleanup pass rewrites a draft, so the finished text is checked once more.
    draft_guard.withhold(result, draft_guard.problems(result, private, voice))
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
