"""Preparation crosses real form pages after each newly generated draft is persisted."""

import pytest
import test_frames

from rove import reasoning, worker, workflow

sites = test_frames.sites

FORM = """<!doctype html><title>Apply - Northwind</title><main></main><script>
const questions = ['Which project interests you?', 'Which team interests you?',
                   'Which product interests you?'];
const answers = [];
document.documentElement.dataset.submissions = '0';
function show() {
  const n = answers.length;
  if (n === questions.length) {
    document.querySelector('main').innerHTML = '<h1>Review your application</h1>'
      + '<pre id="answers"></pre><button id="submit">Submit application</button>';
    document.querySelector('#answers').textContent = JSON.stringify(answers);
    document.querySelector('#submit').onclick = () => {
      document.documentElement.dataset.submissions++;
    };
    return;
  }
  document.querySelector('main').innerHTML = '<h1>Application questions</h1><form>'
    + '<label for="answer">' + questions[n] + '</label><select id="answer" name="q' + n
    + '" required><option value="">Select...</option><option>Platform</option>'
    + '<option>Frontend</option></select><button type="submit">Continue</button></form>';
  document.querySelector('form').onsubmit = event => {
    event.preventDefault();
    answers.push(document.querySelector('#answer').value);
    show();
  };
}
show();</script>"""


@pytest.mark.parametrize("automatic", [True, False])
def test_new_questions_are_filled_through_the_browser_only_with_draft_policy(
    sites, monkeypatch, automatic
):
    runtime, employer, _board, state = sites
    test_frames.Employer.pages["/drafts/jobs/12"] = FORM
    opened = runtime.open(f"{employer}/drafts/jobs/12")
    application = opened["run_id"]
    test_frames.freeze_resume(state, application)
    calls = []

    def review(app, page):
        assert app == application and len(page["pending"]) == 1
        question = page["pending"][0]
        calls.append(question["label"])
        return {
            "answers": [
                {
                    "key": question["key"],
                    "kind": "proposal",
                    "value": "Platform",
                    "proposal_hash": "a" * 64,
                }
            ]
        }

    def browser(action, **kwargs):
        assert action == "prepare"
        return runtime.prepare(**kwargs)

    monkeypatch.setattr(reasoning, "review_application", review)
    monkeypatch.setattr(worker, "browser_call", browser)
    page, _questions = worker.prepare_fields(application, {"auto_use_drafts": automatic})
    if automatic:
        assert calls == [
            "Which project interests you?",
            "Which team interests you?",
            "Which product interests you?",
        ]
        assert not page["pending"] and page["status"] == "READY_FOR_REVIEW"
        assert runtime.page.locator("#answers").inner_text() == '["Platform","Platform","Platform"]'
        assert len(workflow.automatic_answers(application)) == 3
    else:
        assert calls == ["Which project interests you?"] and len(page["pending"]) == 1
        assert runtime.page.locator("select").input_value() == ""
        assert not workflow.automatic_answers(application)
    assert runtime.page.locator("html").get_attribute("data-submissions") == "0"
    assert not workflow.approved_answers(application)


def test_existing_validation_errors_cannot_be_frozen_as_ready(sites):
    runtime, employer, _board, state = sites
    test_frames.Employer.pages["/validation/jobs/12"] = """<title>Apply</title>
      <div role="alert">You have 4 issues that need to be fixed before you can submit.</div>
      <label>First name<input name="first" required></label>
      <label>Resume<input type="file" name="resume" required></label>
      <div>Education: Example College. Fields to fix: 2</div>
      <button id="submit" onclick="document.body.dataset.sent='yes'">Submit application</button>"""
    opened = runtime.open(f"{employer}/validation/jobs/12")
    application = opened["run_id"]
    test_frames.freeze_resume(state, application)
    page = runtime.prepare(application)
    assert page["status"] == "NEEDS_USER"
    assert any(q.get("control_issue") and q["required"] for q in page["pending"])
    assert workflow.get(application)["status"] == "NEEDS_USER"
    assert runtime.page.locator("body").get_attribute("data-sent") is None
