import json
import subprocess

import pytest

from erga_autopilot import reasoning, workflow
from erga_autopilot.onboarding import approve, digest, draft, propose

PROFILE = {
    "education": {"schools": [{"graduation_month": "2027-12"}]},
    "eligibility": {
        "us_work_authorized": True,
        "sponsorship_now": False,
        "sponsorship_future": False,
    },
    "preferences": {"relocate": True, "work_styles": []},
}


def requirement(kind, status="unknown", **extra):
    return {"kind": kind, "requirement": kind, "evidence": "", "status": status, **extra}


POSTING = "Acceptable Graduation Dates: December 2027 - June 2028"


def test_inclusive_graduation_window_is_decided_by_code_not_qwen():
    window = requirement(
        "graduation_window",
        "conflict",
        graduation_start="2027-12",
        graduation_end="2028-06",
        requirement=POSTING,
    )
    checked = reasoning.evaluate_requirements([window], PROFILE, POSTING)[0]
    assert (checked["status"], checked["checked_by"]) == ("satisfied", "code")
    early = requirement("graduation_window", "satisfied", graduation_end="2027-11")
    posting = "Must graduate by November 2027"
    assert reasoning.evaluate_requirements([early], PROFILE, posting)[0]["status"] == "conflict"
    invented = requirement(
        "graduation_window",
        "conflict",
        graduation_start="2027-05",
        graduation_end="2027-08",
        requirement="Rising seniors enrolled in a CS or STEM degree program",
    )
    guessed = reasoning.evaluate_requirements([invented], PROFILE, "Rising seniors welcome")[0]
    assert guessed["status"] == "unknown" and "not stated" in guessed["note"]
    vague = requirement("graduation_window", "conflict")
    assert reasoning.evaluate_requirements([vague], PROFILE)[0]["status"] == "unknown"
    unknown_profile = {**PROFILE, "education": {"schools": [{"graduation_month": None}]}}
    assert reasoning.evaluate_requirements([window], unknown_profile)[0]["status"] == "unknown"


def test_authorization_sponsorship_and_relocation_use_approved_facts():
    checked = reasoning.evaluate_requirements(
        [
            requirement("work_authorization", "conflict", us_authorization_required=True),
            requirement("sponsorship", "conflict", sponsorship_available=False),
            requirement("location", "conflict"),
            requirement("skills", "conflict"),
        ],
        PROFILE,
    )
    assert [c["status"] for c in checked] == ["satisfied", "satisfied", "unknown", "conflict"]
    assert [c["checked_by"] for c in checked] == ["code", "code", "qwen", "qwen"]
    onsite = {**PROFILE, "preferences": {"relocate": True, "work_styles": ["onsite", "hybrid"]}}
    settled = reasoning.evaluate_requirements([requirement("location", "unknown")], onsite)[0]
    assert (settled["status"], settled["checked_by"]) == ("satisfied", "code")
    _, kept, resolved = reasoning.merge_unknowns(
        [settled], ["Whether the applicant can be physically present in SoHo for 10 weeks"]
    )
    assert kept == [] and len(resolved) == 1
    needs_visa = {**PROFILE, "eligibility": {**PROFILE["eligibility"], "sponsorship_future": True}}
    sponsorship = requirement("sponsorship", "satisfied", sponsorship_available=False)
    assert reasoning.evaluate_requirements([sponsorship], needs_visa)[0]["status"] == "conflict"


def test_decision_escalates_qwen_doubt_but_only_code_conflicts_reject():
    ok = {**requirement("program", "satisfied"), "checked_by": "qwen"}
    assert reasoning.decide([ok]) == "fit"
    doubt = {**requirement("skills", "conflict"), "checked_by": "qwen"}
    assert reasoning.decide([ok, doubt]) == "needs_review"
    hard = {**requirement("graduation_window", "conflict"), "checked_by": "code"}
    assert reasoning.decide([ok, hard]) == "not_fit"


def test_unknowns_already_compared_by_code_do_not_hold_the_application():
    window = {**requirement("graduation_window", "satisfied"), "checked_by": "code"}
    ok = {**requirement("program", "satisfied"), "checked_by": "qwen"}
    requirements, kept, resolved = reasoning.merge_unknowns(
        [window, ok], ["Whether the December 2027 graduation falls within the window"]
    )
    assert kept == [] and len(resolved) == 1 and len(requirements) == 2
    assert reasoning.decide(requirements) == "fit"
    requirements, kept, _ = reasoning.merge_unknowns(
        [window, ok], ["Must hold a security clearance"]
    )
    assert kept and requirements[-1]["status"] == "unknown"
    assert reasoning.decide(requirements) == "needs_review"
    schedule = ["Can the applicant work ten weeks full time before the December 2027 graduation"]
    _, kept, resolved = reasoning.merge_unknowns([window, ok], schedule)
    assert kept == schedule and resolved == []


def test_harness_stop_is_not_a_model_answer():
    stopped = {
        "result": {
            "completed": False,
            "turn_exit_reason": "max_iterations_reached(1/1)",
            "final_response": "I ran out of steps",
        }
    }
    with pytest.raises(RuntimeError, match="max_iterations"):
        reasoning.completed_response(stopped)
    done = {
        "result": {
            "completed": True,
            "turn_exit_reason": "text_response(finish_reason=stop)",
            "final_response": '```json\n{"a":1}\n```',
        }
    }
    assert reasoning.strip_fence(reasoning.completed_response(done)) == '{"a":1}'


def test_generate_retries_once_then_reports_the_harness_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"hermes_python": __import__("sys").executable})
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = command[command.index("--output") + 1]
        with open(output, "w") as f:
            json.dump(
                {
                    "model": "m",
                    "result": {
                        "completed": False,
                        "turn_exit_reason": "max_iterations_reached(2/2)",
                    },
                },
                f,
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reasoning.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="max_iterations_reached"):
        reasoning.generate(tmp_path, {"x": 1}, "reasoning")
    assert len(calls) == 2


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    propose(
        "education",
        {"schools": [{"school": "Example University", "graduation_month": "2027-12"}]},
        digest(draft()),
    )
    propose(
        "eligibility",
        {"us_work_authorized": True, "sponsorship_now": False, "sponsorship_future": False},
        digest(draft()),
    )
    approve(digest(draft()))
    return tmp_path / "state"


def test_job_review_ignores_stale_prompt_cache_and_overrides_qwen_arithmetic(state, monkeypatch):
    application_id = workflow.enqueue("https://jobs.example.com/1")["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)
    (directory / "job-review.json").write_text(
        json.dumps({"context_hash": "old", "decision": "fit", "model": "m"})
    )

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    generated = {
        "model": "synthetic-model",
        "result": {
            "completed": True,
            "turn_exit_reason": "text_response(finish_reason=stop)",
            "final_response": json.dumps(
                {
                    "decision": "not_fit",
                    "rationale": "December 2027 is outside December 2027 to June 2028.",
                    "requirements": [
                        {
                            "kind": "graduation_window",
                            "requirement": "Graduate between December 2027 and June 2028",
                            "status": "conflict",
                            "graduation_start": "2027-12",
                            "graduation_end": "2028-06",
                        },
                        {"kind": "program", "requirement": "Internship", "status": "satisfied"},
                    ],
                    "unknowns": [],
                }
            ),
        },
    }
    monkeypatch.setattr(reasoning, "generate", lambda *a, **k: generated)
    result = reasoning.review_job(
        application_id, {"url": "https://jobs.example.com/1", "text": "posting"}
    )
    assert result["qwen_decision"] == "not_fit"
    assert result["decision"] == "fit"
    assert result["requirements"][0]["checked_by"] == "code"
    assert result["prompt_version"] == reasoning.PROMPT_VERSION
    monkeypatch.setattr(reasoning, "generate", lambda *a, **k: pytest.fail("cache must be reused"))
    assert (
        reasoning.review_job(
            application_id, {"url": "https://jobs.example.com/1", "text": "posting"}
        )["decision"]
        == "fit"
    )


def test_cached_review_is_re_evaluated_by_current_code_rules(state, monkeypatch):
    application_id = workflow.enqueue("https://jobs.example.com/2")["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(reasoning, "generate", lambda *a, **k: pytest.fail("Qwen must not run"))
    page = {"url": "https://jobs.example.com/2", "text": "posting", "fields": []}
    from erga_autopilot.onboarding import read_approved

    context_hash = reasoning.fingerprint(
        {
            "review_type": "job_fit",
            "prompt_version": reasoning.PROMPT_VERSION,
            "profile": {
                key: read_approved()["profile"][key]
                for key in ("identity", "education", "eligibility", "availability", "preferences")
            },
            "career_evidence": {"results": []},
            "expected_job_title": "",
            "job_url": page["url"],
            "job_text": "posting",
            "form_questions": [],
        }
    )
    stale = {
        "context_hash": context_hash,
        "decision": "needs_review",
        "model": "m",
        "qwen_output": {
            "decision": "needs_review",
            "rationale": "unsure",
            "requirements": [
                {"kind": "program", "requirement": "Intern", "evidence": "", "status": "satisfied"}
            ],
            "unknowns": [],
        },
    }
    (directory / "job-review.json").write_text(json.dumps(stale))
    result = reasoning.review_job(application_id, page)
    assert result["decision"] == "fit" and "Re-evaluated" in result["note"]
    with workflow.db() as conn:
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM application_events WHERE application_id=?", (application_id,)
            )
        ]
    assert kinds.count("qwen_job_review") == 1
