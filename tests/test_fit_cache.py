"""The model is asked once per job for the fit review, and for drafts only when a pending
question needs writing or a choice.

A stored review is keyed by the posting's content, the approved profile snapshot and the
prompt version. A later tick, a resume after a hold or a reopen reads it back; a change to
any of the three asks Qwen again. A drafting call with nothing to write or choose is not
made, and its questions go to the owner as they would have anyway.
"""

import json
import sqlite3

import pytest

from rove import fastpath, questions, reasoning, submission, timing, worker, workflow
from rove.onboarding import approve, digest, draft, propose, read_approved

POSTING = """Software Engineering Intern, Summer 2027
Students enrolled in Electrical Engineering. Based in Example City.
We do not sponsor visas for this role."""
QWEN_OUTPUT = {
    "decision": "needs_review",
    "rationale": "An internship; the degree field and the location need a look.",
    "requirements": [
        {"kind": "program", "requirement": "Summer 2027 internship", "status": "satisfied"},
        {
            "kind": "degree",
            "requirement": "Students enrolled in Electrical Engineering",
            "status": "conflict",
        },
        {"kind": "location", "requirement": "Based in Example City", "status": "unknown"},
        {
            "kind": "sponsorship",
            "requirement": "We do not sponsor visas for this role",
            "status": "unknown",
            "sponsorship_available": False,
        },
    ],
    "unknowns": [],
}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
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
    monkeypatch.setattr(timing, "_state", {"quiet_until": 0.0, "prune_after": 0.0})
    return tmp_path / "state"


def counting_model(monkeypatch) -> tuple[list, list]:
    """A stand-in for Qwen and for Erga's evidence search that count their calls."""
    model_calls, evidence_calls = [], []

    async def evidence(query):
        evidence_calls.append(query)
        return {"results": [{"evidence_id": f"ev_{len(evidence_calls)}", "excerpt": "x"}]}

    def generate(directory, context, basename, attempts=2):
        model_calls.append(context)
        return {
            "model": "synthetic-model",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": json.dumps(QWEN_OUTPUT),
            },
        }

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(reasoning, "generate", generate)
    return model_calls, evidence_calls


def queued(state, path: str) -> str:
    application_id = workflow.enqueue(f"https://jobs.example.com/{path}")["application_id"]
    (state / "applications" / application_id).mkdir(parents=True)
    return application_id


def form(url: str, *labels: str) -> dict:
    return {"url": url, "text": "Apply form", "fields": [{"label": label} for label in labels]}


def fit_rows() -> list[tuple]:
    """Each recorded fit review: whether it was read back, and why Qwen was asked if not."""
    return [
        (r["facts"]["cached"], r["facts"].get("changed"))
        for r in timing.rows()
        if r["stage"] == "fit_review" and "cached" in r["facts"]
    ]


def review_events(application_id: str) -> int:
    with workflow.db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM application_events WHERE application_id=? "
            "AND kind='qwen_job_review'",
            (application_id,),
        ).fetchone()[0]


def test_a_later_pass_reads_the_stored_review_instead_of_asking_qwen_again(state, monkeypatch):
    model_calls, evidence_calls = counting_model(monkeypatch)
    app = queued(state, "1")
    first = reasoning.review_job(app, form("https://jobs.example.com/1/apply", "Name"), POSTING)
    assert len(model_calls) == 1 and "cached" not in first
    assert first["decision"] == "needs_review" and first["qwen_decision"] == "needs_review"
    # The next tick reaches the form another way: a tracking link, relabelled fields, new
    # Erga evidence, the posting re-wrapped by the page. It is the same posting.
    rewrapped = "  ".join(POSTING.upper().split())
    again = reasoning.review_job(
        app, form("https://jobs.example.com/1/apply?gh_src=x", "Full name", "Email"), rewrapped
    )
    resumed = reasoning.review_job(app, form("https://jobs.example.com/1/apply", "Name"), POSTING)
    assert len(model_calls) == 1 and len(evidence_calls) == 1
    # The whole stored result comes back, so the fit card and the hold read the same.
    for later in (again, resumed):
        assert later.pop("cached") is True
        assert later == first
        assert worker.fit_hold(app, later) == worker.fit_hold(app, first)
    assert [r["status"] for r in first["requirements"]] == [
        "satisfied",
        "conflict",
        "unknown",
        "satisfied",
    ]
    assert first["unverified"] == ["Based in Example City"]
    assert worker.fit_hold(app, first)["items"] == [
        "conflict · Students enrolled in Electrical Engineering",
        "unchecked · Based in Example City",
    ]
    assert review_events(app) == 1  # the thread gets one fit card, not one per pass
    stored = json.loads((state / "applications" / app / "job-review.json").read_text())
    assert stored == first and stored["posting_hash"] == fastpath.posting_hash(POSTING)
    assert fit_rows() == [(False, "first"), (True, None), (True, None)]


def test_a_changed_posting_is_reviewed_again_and_the_old_review_is_kept(state, monkeypatch):
    model_calls, _ = counting_model(monkeypatch)
    app = queued(state, "2")
    page = form("https://jobs.example.com/2", "Name")
    reasoning.review_job(app, page, POSTING)
    reasoning.review_job(app, page, POSTING + "\nApplications close October 31.")
    assert len(model_calls) == 2
    assert model_calls[1]["job_text"].endswith("October 31.")
    # Each version of the posting keeps its own review: flipping back asks nobody.
    reasoning.review_job(app, page, POSTING)
    reasoning.review_job(app, page, POSTING + "\nApplications close October 31.")
    assert len(model_calls) == 2
    # Without a captured posting the form page's own text is the content.
    inline = {**page, "text": POSTING + "\nApply for this job\nFirst name"}
    assert reasoning.review_job(app, inline)["cached"] is True
    assert len(model_calls) == 2
    assert fit_rows() == [(False, "first"), (False, "posting"), *[(True, None)] * 3]


def test_a_new_fit_prompt_is_reviewed_again_and_a_new_drafting_prompt_is_not(state, monkeypatch):
    model_calls, _ = counting_model(monkeypatch)
    app = queued(state, "3")
    page = form("https://jobs.example.com/3", "Name")
    first = reasoning.review_job(app, page, POSTING)
    # The drafting prompt changed, under both of its names: the fit review stands.
    monkeypatch.setattr(reasoning, "ANSWERS_PROMPT_VERSION", "2027-01-01.1")
    monkeypatch.setattr(reasoning, "PROMPT_VERSION", "2027-01-01.1")
    kept = reasoning.review_job(app, page, POSTING)
    assert len(model_calls) == 1 and kept["cached"] is True
    assert kept["prompt_version"] == first["prompt_version"] == reasoning.FIT_PROMPT_VERSION
    # The fit prompt changed: Qwen is asked again, under the new version.
    monkeypatch.setattr(reasoning, "FIT_PROMPT_VERSION", reasoning.FIT_PROMPT_VERSION + ".next")
    newer = reasoning.review_job(app, page, POSTING)
    assert len(model_calls) == 2 and "cached" not in newer
    assert newer["prompt_version"] == reasoning.FIT_PROMPT_VERSION
    assert model_calls[1]["prompt_version"] == reasoning.FIT_PROMPT_VERSION
    assert reasoning.review_job(app, page, POSTING)["cached"] is True
    assert len(model_calls) == 2
    assert fit_rows() == [(False, "first"), (True, None), (False, "prompt"), (True, None)]


def test_the_earlier_name_still_means_the_drafting_prompt():
    assert reasoning.PROMPT_VERSION == reasoning.ANSWERS_PROMPT_VERSION
    from rove.reasoning import PROMPT_VERSION  # existing callers import it by this name

    assert PROMPT_VERSION


def test_a_new_approved_profile_is_reviewed_again(state, monkeypatch):
    model_calls, _ = counting_model(monkeypatch)
    app = queued(state, "4")
    page = form("https://jobs.example.com/4", "Name")
    before = reasoning.review_job(app, page, POSTING)
    propose("preferences", {"relocate": True, "work_styles": ["onsite"]}, digest(draft()))
    approve(digest(draft()))
    # The queued application still names the old snapshot: nothing is reviewed or reused.
    with pytest.raises(PermissionError, match="profile version changed"):
        reasoning.review_job(app, page, POSTING)
    assert len(model_calls) == 1
    with workflow.db() as conn:
        conn.execute(
            "UPDATE application_queue SET profile_hash=? WHERE id=?",
            (read_approved()["profile_hash"], app),
        )
    after = reasoning.review_job(app, page, POSTING)
    assert len(model_calls) == 2 and "cached" not in after
    assert after["profile_hash"] != before["profile_hash"]
    # Code checked the location against the newly approved relocation.
    assert after["unverified"] == [] and before["unverified"] == ["Based in Example City"]
    assert fit_rows() == [(False, "first"), (False, "profile")]


def test_each_job_keeps_its_own_review(state, monkeypatch):
    model_calls, _ = counting_model(monkeypatch)
    first, second = queued(state, "5"), queued(state, "6")
    reasoning.review_job(first, form("https://jobs.example.com/5", "Name"), POSTING)
    reasoning.review_job(second, form("https://jobs.example.com/6", "Name"), POSTING)
    assert len(model_calls) == 2
    version = reasoning.FIT_PROMPT_VERSION
    key = fastpath.review_key(POSTING, read_approved()["profile_hash"], version)
    assert fastpath.stored_review(first, key)["decision"] == "needs_review"
    assert fastpath.stored_review("0" * 12, key) is None
    for changed in (
        fastpath.review_key(POSTING + " more", key[1], key[2]),
        (key[0], "f" * 64, key[2]),
        (key[0], key[1], "older"),
    ):
        assert fastpath.stored_review(first, changed) is None


def test_a_review_is_kept_even_when_posting_its_card_fails(state, monkeypatch):
    model_calls, _ = counting_model(monkeypatch)
    app = queued(state, "7")
    page = form("https://jobs.example.com/7", "Name")

    def discord_down(_application_id):
        raise RuntimeError("Forum creation result is uncertain; reconcile before retrying")

    monkeypatch.setattr(workflow, "flush_events", discord_down)
    with pytest.raises(RuntimeError, match="uncertain"):
        reasoning.review_job(app, page, POSTING)
    monkeypatch.setattr(workflow, "flush_events", lambda _application_id: None)
    assert reasoning.review_job(app, page, POSTING)["cached"] is True
    assert len(model_calls) == 1


def test_a_failed_review_is_not_stored(state, monkeypatch):
    app = queued(state, "8")
    page = form("https://jobs.example.com/8", "Name")

    async def evidence(_query):
        return {"results": []}

    def broken(*_args, **_kwargs):
        raise RuntimeError("Qwen run did not complete: max_iterations_reached(2/2)")

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(reasoning, "generate", broken)
    with pytest.raises(RuntimeError, match="did not complete"):
        reasoning.review_job(app, page, POSTING)
    model_calls, _ = counting_model(monkeypatch)
    assert "cached" not in reasoning.review_job(app, page, POSTING)
    assert len(model_calls) == 1


GENERIC = "Needs reviewed answer or supported control adapter"


def question(label: str, key: str, options=(), reason: str = GENERIC, **extra) -> dict:
    return {
        "label": label,
        "key": key,
        "required": True,
        "options": list(options),
        "reason": reason,
        **extra,
    }


def test_questions_are_sorted_into_writing_choice_short_answer_and_owner_only():
    kind = fastpath.question_kind
    assert kind(question("Why do you want to work here?", "k1"), {"kind": "textarea"}) == "writing"
    assert kind(question("Preferred name", "k2"), {"kind": "text"}) == "short"
    assert kind(question("How did you hear about us?", "k3", ["LinkedIn", "Other"])) == "choice"
    assert kind(question("Open to relocation", "k4"), {"kind": "checkbox"}) == "choice"
    # A control the page did not describe is the model's to look at, never skipped.
    assert kind(question("Anything else?", "k5")) == "writing"
    assert kind({"label": "Team", "key": "k6", "reason": "New field appeared after filling"}) == (
        "writing"
    )
    # Legal and personal questions are the shared draft gate's call: the resolver answers
    # them from the profile when it holds the fact, and one still pending is the owner's,
    # since the review would refuse a draft for it anyway.
    owner_only = [
        "Do you hold an active security clearance?",
        "Have you ever been convicted of a felony?",
        "Do you consent to a background check?",
        "Gender",
        "Race / Ethnicity",
        "Veteran status",
        "Disability status",
        "Date of birth",
        "I certify that the information above is true",
        "Signature",
        "Are you eligible to work in the United States?",
        "Do you need visa sponsorship?",
        "Are you a U.S. citizen?",
        "Are you at least 18 years of age?",
        "Expected salary",
    ]
    for label in owner_only:
        pending = question(label, "k7", ["Yes", "No"])
        assert questions.draft_gate({**pending, "kind": "select-one"})  # the same verdict
        assert kind(pending, {"kind": "select-one"}) == "owner", label
    # A question the form gave no text for is never drafted; the call leaves it out.
    unread = {"label": "", "key": "k8", "required": True, "options": [], "label_missing": True}
    assert kind(unread, {"kind": "text"}) == "owner"
    assert kind(question("Transcript", "k9", reason=fastpath.UNAPPROVED_FILE)) == "owner"
    assert kind(question("Transcript", "k10"), {"kind": "file"}) == "owner"
    # A value code knew and the control refused stays with the model: a draft has settled
    # those before.
    refused = question("Location", "k11", reason="No unique matching dropdown option")
    assert kind(refused, {"kind": "text"}) == "short"
    # Ordinary questions that only sound close stay with the model.
    for label in (
        "Are you able to work in our office five days a week?",
        "Describe a time you embraced a change",
        "What stage of your degree are you in?",
        "List any certifications you hold",
    ):
        assert questions.draft_gate(question(label, "k12")) is None
        assert kind(question(label, "k12"), {"kind": "textarea"}) == "writing", label


def test_a_drafting_call_is_needed_only_for_writing_a_choice_or_a_short_answer():
    pending = [
        question("Do you hold an active security clearance?", "a1", ["Yes", "No"]),
        question("Cover letter", "a2", reason="Unapproved required file requested"),
    ]
    counts = fastpath.drafting_counts(pending, [{"key": "a1", "kind": "select-one"}])
    assert counts == {"questions": 2, "writing": 0, "choices": 0, "short": 0, "owner_only": 2}
    assert not fastpath.needs_model(counts)
    for extra, field, name in (
        (question("Why us?", "b1"), {"key": "b1", "kind": "textarea"}, "writing"),
        (question("How did you hear about us?", "b2", ["LinkedIn", "Other"]), None, "choices"),
        (question("Preferred name", "b3"), {"key": "b3", "kind": "text"}, "short"),
    ):
        counts = fastpath.drafting_counts([*pending, extra], [field] if field else None)
        assert counts[name] == 1 and counts["owner_only"] == 2 and fastpath.needs_model(counts)
    assert not fastpath.needs_model(fastpath.drafting_counts([]))


def test_filled_fields_are_counted_by_who_supplied_the_value(state, monkeypatch):
    app = queued(state, "9")
    answers = [
        (app, "k-draft", "Because of the mission.", "auto-draft:" + "c" * 12),
        (app, "k-used", "Other", "m-use"),
        (app, "k-owner", "No", "m-answer"),
        (app, "k-blank", "skip", "auto-skip:k-blank"),
    ]
    with workflow.db() as conn:
        conn.executemany("INSERT INTO application_answers VALUES(?,?,?,?)", answers)
        for message, kind in (("m-use", "use"), ("m-answer", "answer")):
            conn.execute(
                "INSERT INTO owner_commands VALUES(?,?,?,?,?,?)",
                (message, app, kind, "{}", "applied", workflow.now()),
            )
    page = {
        "filled": [
            {"key": "k-name", "source": "identity.legal_first_name"},
            {"label": "Resume", "sha256": "d" * 64},
            {"key": "k-draft"},
            {"key": "k-used"},
            {"key": "k-owner"},
        ],
        "pending": [{"key": "k-open"}],
    }
    counted = {"fields_code": 2, "fields_model": 2, "fields_owner": 1, "fields_pending": 1}
    assert fastpath.fill_counts(app, page) == counted

    # A count is measurement: a database it cannot read leaves it out and raises nothing.
    # (In WAL mode a writer no longer blocks this read, so the failure is simulated.)
    def unreadable():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(timing, "connect", unreadable)
    assert fastpath.fill_counts(app, page) == {}


FIELD = {"label": "First name", "name": "first", "kind": "text", "options": [], "required": True}
FORM = {
    "url": "https://jobs.example.com/form",
    "observation_id": "obs-1",
    "fields": [FIELD],
    "text": "",
    "ats_markers": {},
    "application_links": [],
}
FIT = {"decision": "fit", "rationale": "", "unverified": [], "requirements": []}


def prepare_worker(monkeypatch, pending: list, fields: list) -> tuple[list, list]:
    """A worker with a scripted browser whose form leaves `pending` unanswered. Returns
    the browser actions and the pages handed to the drafting call."""
    actions, drafted = [], []

    def browser(action, **_kwargs):
        actions.append(action)
        if action != "prepare":
            return json.loads(json.dumps(FORM))
        return json.loads(json.dumps({"pending": pending, "filled": [], "fields": fields}))

    def review_application(application_id, page):
        drafted.append(page)
        return {"answers": []}

    monkeypatch.setattr(worker, "browser_call", browser)
    # The scripted form lives on no board: no first-time-on-this-site hold here.
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False, "first_send_hold": "off"})
    monkeypatch.setattr(reasoning, "review_job", lambda *a: dict(FIT))
    monkeypatch.setattr(reasoning, "review_application", review_application)
    monkeypatch.setattr(
        worker, "prepare_resume", lambda *a: {"ready": True, "resume_sha256": "b" * 64}
    )
    monkeypatch.setattr(submission, "enabled_adapter", lambda url: object())
    return actions, drafted


def test_a_block_page_is_never_reviewed_as_the_posting(state, monkeypatch):
    blocked = {**FORM, "fields": [], "blocked": True, "text": "Access Denied"}
    posting = {
        **FORM,
        "fields": [],
        "text": POSTING,
        "application_links": [{"ref": "0", "label": "Apply"}],
    }
    pages = {
        "open": blocked,
        "reopen": posting,
        "follow": FORM,
        "prepare": {"pending": [], "filled": [], "package_hash": "a" * 64, "final_controls": []},
    }
    actions, _ = prepare_worker(monkeypatch, [], [])
    monkeypatch.setattr(
        worker,
        "browser_call",
        lambda action, **_kw: actions.append(action) or json.loads(json.dumps(pages[action])),
    )
    reviewed = []
    monkeypatch.setattr(
        reasoning, "review_job", lambda app, page, text: reviewed.append(text) or dict(FIT)
    )
    app = workflow.enqueue(
        "https://jobs.example.com/blocked-once", source="keryx", title="Example — Intern"
    )["application_id"]
    worker.process(app)
    assert actions == ["open", "reopen", "follow", "prepare"]
    assert reviewed == [POSTING]  # what the retry reached, not the block page's text
    in_order = sorted(timing.rows(), key=lambda r: r["started_at"])
    assert [r["stage"] for r in in_order if r["parent"] == "pass"] == [
        "open",
        "reopen",
        "follow",
        "resume",
        "fill",
        "hold",
    ]


def drafting_rows() -> list[dict]:
    return [r["facts"] for r in timing.rows() if r["stage"] == "drafting"]


def test_no_drafting_call_is_made_when_nothing_needs_writing_or_a_choice(state, monkeypatch):
    pending = [
        question("Have you ever been convicted of a felony?", "a" * 12, ["Yes", "No"]),
        {"label": "Transcript", "key": "b" * 12, "reason": "Unapproved required file requested"},
    ]
    fields = [{"key": "a" * 12, "kind": "select-one"}, {"key": "b" * 12, "kind": "file"}]
    actions, drafted = prepare_worker(monkeypatch, pending, fields)
    app = workflow.enqueue(
        "https://jobs.example.com/skip", source="keryx", title="Example — Intern"
    )["application_id"]
    result = worker.process(app)
    assert drafted == []  # Qwen was not asked
    assert actions == ["open", "prepare"]  # and nothing was filled a second time
    # The owner is asked exactly what a needs-owner reply would have asked.
    assert result["status"] == "NEEDS_USER" and not result["submitted"]
    hold = workflow.latest_hold(app)
    assert hold["headline"] == "Answers needed"
    assert [(q["key"], q["state"]) for q in hold["questions"]] == [
        ("a" * 12, "open"),
        ("b" * 12, "open"),
    ]
    assert "2 questions only you can answer" in hold["reason"]
    assert drafting_rows() == [
        {"skipped": True, "questions": 2, "writing": 0, "choices": 0, "short": 0, "owner_only": 2}
    ]
    with workflow.db() as conn:
        assert not conn.execute("SELECT 1 FROM application_answers").fetchone()


@pytest.mark.parametrize(
    ("extra", "field", "kind"),
    [
        (question("Why do you want to work here?", "c" * 12), {"kind": "textarea"}, "writing"),
        (
            question("How did you hear about this role?", "c" * 12, ["LinkedIn", "Other"]),
            {"kind": "select-one"},
            "choices",
        ),
        (question("Preferred name", "c" * 12), {"kind": "text"}, "short"),
    ],
)
def test_the_drafting_call_is_still_made_when_a_question_needs_the_model(
    state, monkeypatch, extra, field, kind
):
    owner_only = question("Do you hold an active security clearance?", "a" * 12, ["Yes", "No"])
    pending = [owner_only, extra]
    fields = [{"key": "a" * 12, "kind": "select-one"}, {"key": "c" * 12, **field}]
    actions, drafted = prepare_worker(monkeypatch, pending, fields)
    app = workflow.enqueue(
        "https://jobs.example.com/" + kind, source="keryx", title="Example — Intern"
    )["application_id"]
    assert worker.process(app)["status"] == "NEEDS_USER"
    assert actions == ["open", "prepare"]
    # One call, with every pending question: the model still sees the whole batch.
    assert len(drafted) == 1 and [q["key"] for q in drafted[0]["pending"]] == ["a" * 12, "c" * 12]
    expected = {"questions": 2, "writing": 0, "choices": 0, "short": 0, "owner_only": 1}
    assert drafting_rows() == [{**expected, kind: 1, "proposals": 0}]
