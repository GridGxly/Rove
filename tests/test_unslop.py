import json

import pytest

from rove import reasoning, unslop, workflow
from rove.onboarding import approve, digest, draft, propose


def test_builtin_scan_finds_hard_tells_and_leaves_plain_prose_alone(monkeypatch):
    monkeypatch.setattr(workflow, "config", dict)
    report = unslop.scan(
        "I am passionate about leveraging robust tools. It is not just fast but reliable."
    )
    phrases = {v["phrase"] for v in report["violations"]}
    assert {"passionate about", "leveraging", "robust", "not just"} <= phrases
    assert report["source"] == "builtin" and unslop.needs_cleanup(report)
    clean = unslop.scan("I built 56 migrations for student records and a streaming advisor.")
    assert clean["violations"] == [] and not unslop.needs_cleanup(clean)


def test_humanizer_scan_flags_shape_tells_and_passes_plain_prose(monkeypatch):
    monkeypatch.setattr(workflow, "config", dict)
    staged = (
        "Let's dive in. My work serves as a foundation for meticulous engineering — careful, "
        "fast, and vibrant — and it could potentially matter. Why does that matter?"
    )
    report = unslop.scan(staged)
    phrases = {v["phrase"] for v in report["violations"]}
    assert {"let's dive in", "serves as a", "meticulous", "vibrant", "could potentially"} <= phrases
    humanizer = [v for v in report["violations"] if v["category"].startswith("humanizer")]
    assert len(humanizer) >= 3 and unslop.needs_cleanup(report)
    # Two connector dashes in one paragraph are a hard tell; one question is only soft.
    severity = {v["phrase"]: v["severity"] for v in report["violations"]}
    assert severity["—"] == "hard" and severity["Why does that matter?"] == "soft"
    assert [v["column"] for v in report["violations"]] == sorted(
        v["column"] for v in report["violations"]
    )
    plain = unslop.scan(
        "I wrote the import script in Python and ran it against 56 student records. "
        "The first run failed on dates. I fixed the parser and reran it the same night."
    )
    assert plain["violations"] == [] and not unslop.needs_cleanup(plain)
    # A soft tell alone is advisory; a second finding makes it a repair.
    soft = unslop.scan("I am ensuring the import is correct. The parser is done.")
    assert not unslop.needs_cleanup(soft) and len(soft["violations"]) == 1
    assert not unslop.preserved_facts(
        "Cut p95 from 900 ms to 120 ms.", "Cut p95 latency to 120 ms."
    )


def test_fact_preservation_rejects_dropped_numbers_or_names():
    original = "I built 56 Supabase migrations and a Next.js site for TransferTrack."
    assert unslop.preserved_facts(
        original,
        "I built 56 Supabase migrations and a Next.js site for TransferTrack, then shipped it.",
    )
    assert not unslop.preserved_facts(
        original, "I built many Supabase migrations and a Next.js site for TransferTrack."
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False})
    return tmp_path / "state"


def test_drafts_get_one_bounded_cleanup_and_are_hashed_after_it(state, monkeypatch):
    from rove.onboarding import read_approved

    application_id = workflow.enqueue("https://jobs.example.com/1")["application_id"]
    (state / "applications" / application_id).mkdir(parents=True)

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    key = "abcdef012345"
    slop = "I am passionate about leveraging robust tools. I built 56 migrations for TransferTrack."
    fixed = "I like building tools people use. I built 56 migrations for TransferTrack."
    calls = []

    def fake_generate(directory, context, basename, attempts=2):
        calls.append(basename)
        if context.get("review_type") == "cleanup":
            assert context["text"] == slop and context["findings"]
            text = fixed
        else:
            text = json.dumps(
                {
                    "answers": [
                        {
                            "key": key,
                            "kind": "proposal",
                            "value": slop,
                            "sources": ["ev_1"],
                            "explanation": "e",
                        }
                    ]
                }
            )
        return {
            "model": "m",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": text,
            },
        }

    monkeypatch.setattr(reasoning, "generate", fake_generate)
    page = {
        "profile_hash": read_approved()["profile_hash"],
        "pending": [{"key": key, "label": "Favorite project?"}],
        "text": "",
    }
    result = reasoning.review_application(application_id, page)
    answer = result["answers"][0]
    assert answer["value"] == fixed and answer["original_value"] == slop
    assert "repaired" in answer["unslop"] and "leveraging" in answer["unslop"]
    assert calls == ["reasoning", f"cleanup-{key}"]
    expected = reasoning.fingerprint(
        {
            "application_id": application_id,
            "context_hash": result["context_hash"],
            **{
                k: answer[k]
                for k in (
                    "key",
                    "kind",
                    "value",
                    "sources",
                    "explanation",
                    "unslop",
                    "unslop_report",
                    "original_value",
                )
            },
        }
    )
    assert answer["proposal_hash"] == expected


def test_cadence_scores_alone_never_trigger_a_rewrite_and_summary_is_one_line():
    report = {
        "source": "unslop",
        "violations": [],
        "structure": [{"metric": "sentence_burstiness"}],
    }
    assert not unslop.needs_cleanup(report)
    assert unslop.summary(report, None) == "clean (unslop scan) · advisory: sentence_burstiness"
    assert not unslop.preserved_facts("I built 56 things.", "I built 56 things and 3 more.")
