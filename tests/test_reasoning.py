import json
import os
import subprocess
from pathlib import Path

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
        requirement="Students graduating from a CS or STEM degree program",
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
    doubt = {**requirement("location", "conflict"), "checked_by": "qwen"}
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
    # An unresolved unknown is shown to the owner as unverified; it never holds the job.
    assert reasoning.decide(requirements) == "fit"
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


def test_context_budget_trims_long_text_and_evidence_first():
    context = {
        "profile": {"identity": {"legal_first_name": "Alex"}},
        "job_text": "x" * 30000,
        "career_evidence": {"results": [{"evidence_id": "e", "excerpt": "y" * 30000}]},
    }
    fitted = reasoning.fit_budget(context, limit=20000)
    assert len(json.dumps(fitted)) <= 20000
    assert fitted["profile"] == context["profile"]
    assert (
        len(fitted["job_text"]) < 30000
        and len(fitted["career_evidence"]["results"][0]["excerpt"]) < 30000
    )


def test_proposed_value_outside_the_options_becomes_a_question():
    key = "abcdef012345"
    raw = json.dumps(
        {
            "answers": [
                {
                    "key": key,
                    "kind": "proposal",
                    "value": "Keryx feed",
                    "sources": ["s"],
                    "explanation": "e",
                }
            ]
        }
    )
    parsed = reasoning.parse_review(raw, {key}, {key: ["LinkedIn", "Other"]})
    assert (
        parsed["answers"][0]["kind"] == "needs_user"
        and "not one of the options" in parsed["answers"][0]["explanation"]
    )
    ok = json.dumps(
        {
            "answers": [
                {
                    "key": key,
                    "kind": "proposal",
                    "value": "other",
                    "sources": ["s"],
                    "explanation": "e",
                }
            ]
        }
    )
    assert (
        reasoning.parse_review(ok, {key}, {key: ["LinkedIn", "Other"]})["answers"][0]["kind"]
        == "proposal"
    )


def test_computing_degree_satisfies_a_related_field_requirement_by_code():
    profile = {
        **PROFILE,
        "education": {
            "schools": [
                {
                    "graduation_month": "2027-12",
                    "major": "Computing Technology and Software Development",
                    "degree": "B.A.S.",
                }
            ]
        },
    }
    related = requirement(
        "degree",
        "unknown",
        requirement="Bachelor's in Computer Science, Engineering or a related field",
    )
    checked = reasoning.evaluate_requirements([related], profile)[0]
    assert (checked["status"], checked["checked_by"]) == ("satisfied", "code")
    strict = requirement(
        "degree", "conflict", requirement="Must be enrolled in Electrical Engineering"
    )
    assert reasoning.evaluate_requirements([strict], profile)[0]["status"] == "conflict"
    vague = requirement("degree", "unknown", requirement="Enrolled in Electrical Engineering")
    assert reasoning.evaluate_requirements([vague], profile)[0]["checked_by"] == "qwen"
    _, kept, resolved = reasoning.merge_unknowns(
        [checked], ["Whether Computing Technology counts as a related field to CS"]
    )
    assert kept == [] and len(resolved) == 1


def test_only_conflicts_on_eligibility_requirements_change_the_decision():
    ok = {**requirement("program", "satisfied"), "checked_by": "qwen"}
    hard = {**requirement("graduation_window", "conflict"), "checked_by": "code"}
    assert reasoning.decide([ok, hard]) == "not_fit"
    claimed = {**requirement("work_authorization", "conflict"), "checked_by": "qwen"}
    assert reasoning.decide([ok, claimed]) == "needs_review"
    unchecked = {**requirement("graduation_window", "unknown"), "checked_by": "qwen"}
    assert reasoning.decide([ok, unchecked]) == "fit"


def test_skills_and_other_wishes_never_change_the_decision():
    wishes = [
        {**requirement("skills", "unknown"), "checked_by": "qwen"},
        {**requirement("other", "unknown"), "checked_by": "qwen"},
        {**requirement("skills", "conflict"), "checked_by": "qwen"},
    ]
    assert reasoning.decide(wishes) == "fit"


def test_evaluate_review_lists_unverified_eligibility_instead_of_holding():
    output = {
        "decision": "needs_review",
        "rationale": "unsure",
        "requirements": [
            {"kind": "program", "requirement": "Internship", "evidence": "", "status": "satisfied"},
            {
                "kind": "location",
                "requirement": "Based in Example City",
                "evidence": "",
                "status": "unknown",
            },
            {"kind": "skills", "requirement": "Rust", "evidence": "", "status": "unknown"},
        ],
        "unknowns": [],
    }
    result = reasoning.evaluate_review(output, PROFILE)
    assert result["decision"] == "fit"
    assert result["unverified"] == ["Based in Example City"]


def test_an_internship_term_is_not_a_graduation_window():
    term = requirement(
        "graduation_window",
        "conflict",
        graduation_start="2027-01",
        graduation_end="2027-05",
        requirement="Winter/Spring 2027",
    )
    checked = reasoning.evaluate_requirements([term], PROFILE, "Winter/Spring 2027 internship")[0]
    assert (checked["kind"], checked["status"], checked["checked_by"]) == (
        "dates",
        "unknown",
        "qwen",
    )
    assert "term" in checked["note"]
    assert reasoning.decide([checked]) == "fit"
    window = requirement(
        "graduation_window",
        "unknown",
        graduation_start="2027-05",
        graduation_end="2028-05",
        requirement="Graduating between May 2027 and May 2028",
    )
    posting = "Eligibility: graduating between May 2027 and May 2028"
    checked = reasoning.evaluate_requirements([window], PROFILE, posting)[0]
    assert (checked["status"], checked["checked_by"]) == ("satisfied", "code")


def test_class_standing_is_decided_by_code_from_the_graduation_month():
    from erga_autopilot.reasoning import class_standing, evaluate_requirements

    assert class_standing("Rising seniors in a CS program", "2027-12", "Summer 2027 internship")[
        0
    ] == ("satisfied")
    assert class_standing("Rising seniors in a CS program", "2026-12", "Summer 2027 internship")[
        0
    ] == ("conflict")
    assert class_standing("Rising juniors welcome", "2028-12", "Summer 2027 internship")[0] == (
        "satisfied"
    )
    assert class_standing("Bachelor's degree", "2027-12", "") is None
    profile = {
        "education": {"schools": [{"graduation_month": "2027-12"}]},
        "eligibility": {
            "us_work_authorized": True,
            "sponsorship_now": False,
            "sponsorship_future": False,
        },
        "preferences": {"relocate": True, "work_styles": ["onsite"]},
    }
    checked = evaluate_requirements(
        [
            {
                "kind": "program",
                "requirement": "Rising seniors enrolled in a CS or STEM degree program",
                "evidence": "",
                "status": "conflict",
                "checked_by": "qwen",
            }
        ],
        profile,
        "10-week Summer 2027 internship in NYC",
    )
    assert checked[0]["status"] == "satisfied" and checked[0]["checked_by"] == "code"


def test_drafts_are_shortened_to_the_fields_limit_at_a_sentence_boundary():
    from erga_autopilot.reasoning import length_problems, shorten_to_fit

    questions = [{"key": "k1", "max_chars": 120}, {"key": "k2", "max_chars": None}]
    long = "First sentence here. " * 7 + "Second one follows. " + "Third closes it."
    result = {"answers": [{"key": "k1", "kind": "proposal", "value": long, "explanation": "x"}]}
    assert "120" in length_problems(result, questions)
    shorten_to_fit(result, questions)
    assert len(result["answers"][0]["value"]) <= 120
    assert result["answers"][0]["value"].endswith(".")
    assert "Shortened" in result["answers"][0]["explanation"]
    assert length_problems(result, questions) == ""


def test_voice_samples_read_the_story_note_only_when_it_exists(tmp_path, monkeypatch):
    from erga_autopilot import vault

    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(root))
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path / "state"))
    assert vault.voice_samples() == ""
    note = root / "Erga Autopilot/Story/Voice.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: story\n---\nI fixed the import by hand. It took a week.\n")
    assert vault.voice_samples() == "I fixed the import by hand. It took a week."
    long_note = "I wrote this sentence myself. " * 400
    note.write_text(long_note)
    sample = vault.voice_samples()
    assert 1250 < len(sample) <= 2500 and sample.endswith(".")
    assert note.read_text() == long_note
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH")
    assert vault.voice_samples() == ""


def test_drafting_context_carries_the_owner_voice_note(state, monkeypatch):
    from erga_autopilot.onboarding import read_approved

    note = Path(os.environ["OBSIDIAN_VAULT_PATH"]) / "Erga Autopilot/Story/Voice.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("I like small tools. I wrote the first one in a weekend.\n")

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False})
    captured = []
    key = "abcdef012345"

    def fake_generate(directory, context, basename, attempts=2):
        captured.append(context)
        answers = [
            {
                "key": key,
                "kind": "proposal",
                "value": "Short.",
                "sources": ["ev_1"],
                "explanation": "e",
            }
        ]
        return {
            "model": "m",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": json.dumps({"answers": answers}),
            },
        }

    monkeypatch.setattr(reasoning, "generate", fake_generate)
    page = {
        "profile_hash": read_approved()["profile_hash"],
        "pending": [{"key": key, "label": "Why us?"}],
        "text": "",
    }
    with_note = workflow.enqueue("https://jobs.example.com/voice")["application_id"]
    (state / "applications" / with_note).mkdir(parents=True)
    reasoning.review_application(with_note, page)
    assert captured[0]["owner_voice"] == "I like small tools. I wrote the first one in a weekend."
    note.unlink()
    without = workflow.enqueue("https://jobs.example.com/plain")["application_id"]
    (state / "applications" / without).mkdir(parents=True)
    reasoning.review_application(without, page)
    assert "owner_voice" not in captured[1]


def test_drafting_context_carries_company_research(state, monkeypatch):
    from erga_autopilot.onboarding import read_approved

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False})
    key = "abcdef012345"
    captured, asked = [], []

    def fake_generate(directory, context, basename, attempts=2):
        captured.append(context)
        answers = [
            {
                "key": key,
                "kind": "proposal",
                "value": "Short.",
                "sources": ["ev_1"],
                "explanation": "e",
            }
        ]
        return {
            "model": "m",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": json.dumps({"answers": answers}),
            },
        }

    monkeypatch.setattr(reasoning, "generate", fake_generate)
    research_text = "Acme Robotics builds warehouse robots for mid-size grocers."

    def fake_research(application_id, posting_text, posting_url):
        asked.append((application_id, posting_text, posting_url))
        return research_text

    monkeypatch.setattr(reasoning, "company_context", fake_research)
    page = {
        "profile_hash": read_approved()["profile_hash"],
        "pending": [{"key": key, "label": "Why Acme?"}],
        "text": "Apply form",
    }
    posting_url = "https://careers.acme.example/jobs/1"
    application_id = workflow.enqueue(posting_url)["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)
    (directory / "job-reasoning-input.json").write_text(json.dumps({"job_text": "Acme posting"}))
    reasoning.review_application(application_id, page)
    # The posting goes as the job-fit review saw it, with the queue's posting URL.
    assert asked == [(application_id, "Acme posting", posting_url)]
    assert captured[0]["company_research"] == research_text
    assert captured[0]["prompt_version"] == reasoning.PROMPT_VERSION
    without = workflow.enqueue("https://jobs.example.com/plain")["application_id"]
    (state / "applications" / without).mkdir(parents=True)
    monkeypatch.setattr(reasoning, "company_context", lambda *_: "")
    reasoning.review_application(without, page)
    assert "company_research" not in captured[1]
