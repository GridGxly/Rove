"""A form reveals later questions only after the preceding answers were accepted."""

import pytest
import test_fit_cache

from rove import reasoning, worker, workflow

state = test_fit_cache.state


@pytest.fixture
def application(state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False})
    return workflow.enqueue("https://jobs.example.com/rounds")["application_id"]


def question(number, **extra):
    return {
        "label": "Which project interests you?",
        "key": f"{number:012x}",
        "required": True,
        "options": ["Platform", "Frontend"],
        **extra,
    }


def staged_browser(monkeypatch, application, stages):
    calls, drafts = [], []

    def browser(action, **kwargs):
        assert action == "prepare" and kwargs == {"run_id": application}
        if calls:
            # The page advances only after the previous answer reached durable storage.
            previous = stages[min(len(calls) - 1, len(stages) - 1)]
            answers = workflow.automatic_answers(application)
            for q in previous:
                assert q["key"] in answers
        pending = stages[min(len(calls), len(stages) - 1)]
        calls.append(action)
        return {
            "pending": pending,
            "filled": [],
            "fields": [],
            "final_controls": [] if pending else [{"label": "Submit"}],
            "package_hash": "a" * 64,
        }

    def review(app, page):
        assert app == application
        drafts.append([q["key"] for q in page["pending"]])
        return {
            "answers": [
                {
                    "key": q["key"],
                    "kind": "proposal",
                    "value": "Platform",
                    "proposal_hash": "a" * 64,
                }
                for q in page["pending"]
            ]
        }

    monkeypatch.setattr(worker, "browser_call", browser)
    monkeypatch.setattr(reasoning, "review_application", review)
    return calls, drafts


def test_new_form_steps_continue_without_another_owner_command(application, monkeypatch):
    calls, drafts = staged_browser(
        monkeypatch, application, [[question(n)] for n in range(3)] + [[]]
    )
    page, _ = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert not page["pending"] and page["final_controls"] == [{"label": "Submit"}]
    assert len(calls) == 4 and len(drafts) == 3
    assert len(workflow.automatic_answers(application)) == 3
    assert not workflow.approved_answers(application)
    with workflow.db() as conn:
        assert conn.execute("SELECT count(*) FROM owner_commands").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM live_submission_attempts").fetchone()[0] == 0


def test_repeated_unresolved_question_stops_without_redrafting(application, monkeypatch):
    calls, drafts = staged_browser(monkeypatch, application, [[question(1)]])
    page, _ = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert len(calls) == 2 and len(drafts) == 1
    assert page["pending"] and not page.get("preparation_limit")


def test_required_personal_fact_on_next_step_is_never_drafted(application, monkeypatch):
    unknown = question(2, label="Address1")
    calls, drafts = staged_browser(monkeypatch, application, [[question(1)], [unknown]])
    page, questions = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert len(calls) == 2 and len(drafts) == 1
    assert page["pending"] == [unknown]
    assert questions == [
        {
            "key": unknown["key"],
            "label": "Address1",
            "options": unknown["options"],
            "required": True,
            "state": "open",
        }
    ]
    assert unknown["key"] not in workflow.approved_answers(application)


def test_unapproved_draft_stays_held(application, monkeypatch):
    calls, drafts = staged_browser(monkeypatch, application, [[question(1)], []])
    page, questions = worker.prepare_fields(application, {"auto_use_drafts": False})
    assert page["pending"] and len(calls) == len(drafts) == 1
    assert questions[0]["state"] == "drafted" and not workflow.approved_answers(application)


def test_manual_step_never_enters_drafting(application, monkeypatch):
    manual = question(2, manual=True, reason="Verification required")
    calls, drafts = staged_browser(monkeypatch, application, [[question(1)], [manual]])
    page, _ = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert len(calls) == 2 and len(drafts) == 1 and page["pending"] == [manual]


def test_a_form_adding_steps_forever_has_a_bounded_truthful_hold(application, monkeypatch):
    calls, drafts = staged_browser(monkeypatch, application, [[question(n)] for n in range(20)])
    page, questions = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert page["preparation_limit"] and len(drafts) == worker.PREPARATION_ROUNDS
    assert len(calls) == worker.PREPARATION_ROUNDS + 1
    monkeypatch.setattr(worker, "leave_tab_to_owner", lambda app: None)
    worker.finish_preparation(workflow.get(application), page, questions, {}, {})
    hold = workflow.latest_hold(application)
    assert hold["headline"] == "More form steps remain" and hold["in_place"]
    assert "only you can answer" not in hold["reason"]


def test_later_optional_address_lines_are_left_blank_without_asking(application, monkeypatch):
    optional = question(2, label="Address2", required=False)
    calls, drafts = staged_browser(monkeypatch, application, [[question(1)], [optional], []])
    page, _ = worker.prepare_fields(application, {"auto_use_drafts": True})
    assert not page["pending"] and len(calls) == 3 and len(drafts) == 1
    answer = workflow.automatic_answers(application)[optional["key"]]
    assert answer["value"] == "skip" and answer["kind"] == "skip"
