"""Local Qwen/Hermes proposals, never an authorization or candidate-fact writer."""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import workflow
from .evidence import career_evidence
from .onboarding import read_approved
from .runtime import state_root, write_private


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


def parse_review(raw: str, observed_keys: set[str]) -> dict:
    text = raw.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    result = Review.model_validate_json(text).model_dump()
    seen = set()
    for answer in result["answers"]:
        if answer["key"] not in observed_keys or answer["key"] in seen:
            raise ValueError("Qwen referenced an unknown or repeated question")
        seen.add(answer["key"])
        if answer["kind"] == "needs_user" and answer["value"]:
            raise ValueError("An unknown fact cannot carry an application answer")
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
        "questions": questions,
        "profile": {
            key: profile[key]
            for key in [
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
    write_private(directory / "reasoning-input.json", context)
    settings = workflow.config()
    python = settings.get("hermes_python")
    if not python or not Path(python).is_file():
        raise ValueError("Configure the installed Hermes Python path before running Qwen")
    repository = Path(__file__).resolve().parents[2]
    command = [
        python,
        str(repository / "scripts/recruiting_reasoning.py"),
        "--hermes-checkout",
        str(Path.home() / ".hermes/hermes-agent"),
        "--input",
        str(directory / "reasoning-input.json"),
        "--output",
        str(directory / "reasoning-result.json"),
    ]
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=900,
        env=dict(os.environ, PYTHONPATH=str(repository / "src"), HERMES_AUTOPILOT_16K="1"),
    )
    generated = json.loads((directory / "reasoning-result.json").read_text())
    raw = generated["result"].get("final_response", "")
    result = parse_review(raw, {q["key"] for q in questions})
    result.update(
        approved=False,
        model=generated["model"],
        harness="Hermes",
        profile_hash=page["profile_hash"],
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
