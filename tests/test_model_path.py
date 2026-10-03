"""The worker's model path: one direct request per structured prompt, a context whose
leading part the server can reuse, a token budget, compact answers, and the model work
done while the worker waits.

Every test serves the model from an in-process stand-in for the local server; nothing
reaches the network or the real model.
"""

import json
import sys

import httpx
import pytest

from rove import model_client, prereview, reasoning, runtime, timing, workflow
from rove.onboarding import approve, digest, draft, propose, read_approved

POSTING = """Software Engineering Intern, Summer 2027
Students in a computer science or related degree. Graduating between December 2027 and
June 2028. US work authorization required."""
FIT = {
    "decision": "fit",
    "rationale": "A CS internship the approved facts meet.",
    "requirements": [
        {"kind": "program", "requirement": "Summer 2027 internship", "status": "satisfied"},
        {
            "kind": "graduation_window",
            "requirement": "Graduating between December 2027 and June 2028",
            "status": "unknown",
            "graduation_start": "2027-12",
            "graduation_end": "2028-06",
        },
    ],
    "unknowns": [],
}


class FakeServer:
    """The local server's OpenAI-compatible surface, scripted per test."""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list = []
        self.status = {"active_requests": 0, "waiting_requests": 0}
        self.consumed = 0
        self.count = None  # the tokenizer's count of a shared prefix; None: cannot count
        self.counted: list[dict] = []

    def reply(self, *parts, finish="stop", usage=None, reasoning_parts=(), error=None):
        chunks = [{"choices": [{"delta": {"reasoning_content": r}}]} for r in reasoning_parts]
        chunks += [{"choices": [{"delta": {"content": p}, "finish_reason": None}]} for p in parts]
        if error:
            chunks.append({"error": {"message": error, "type": "server_error"}})
        if finish:
            chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        chunks.append({"choices": [], "usage": usage or {"prompt_tokens": 10}})
        self.replies.append(chunks)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": runtime.MODEL}]})
        if path == "/api/status":
            if self.status is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self.status)
        body = json.loads(request.content)
        if path == "/v1/messages/count_tokens":
            self.counted.append(body)
            if self.count is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"input_tokens": self.count})
        self.requests.append(body)
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, Exception):
            raise reply
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "o"}}]})

        def stream():
            for chunk in reply or []:
                self.consumed += 1
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()

    def client():
        return httpx.Client(
            base_url=runtime.BASE_URL,
            transport=httpx.MockTransport(fake.handle),
            headers={"Authorization": "Bearer test"},
        )

    monkeypatch.setattr(runtime, "client", client)
    return fake


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


@pytest.fixture
def evidence(monkeypatch):
    calls = []

    async def career_evidence(query):
        calls.append(query)
        return {"results": [{"evidence_id": "ev_1", "excerpt": "Built a small tool. " * 20}]}

    monkeypatch.setattr(reasoning, "career_evidence", career_evidence)
    return calls


def queued(state, path: str, **kwargs) -> str:
    application_id = workflow.enqueue(f"https://jobs.example.com/{path}", **kwargs)[
        "application_id"
    ]
    (state / "applications" / application_id).mkdir(parents=True, exist_ok=True)
    return application_id


def user_turn(body: dict) -> str:
    return next(m["content"] for m in body["messages"] if m["role"] == "user")


def sent_context(body: dict) -> dict:
    return json.loads(user_turn(body).rsplit("\n\n", 1)[0])


def events(application_id: str) -> list[str]:
    with workflow.db() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM application_events WHERE application_id=? ORDER BY id",
                (application_id,),
            )
        ]


# --- the direct request -----------------------------------------------------------


def test_a_structured_prompt_is_one_direct_request_with_thinking_off_twice(state, server, tmp_path):
    server.reply(
        '{"label":',
        ' "other"}',
        usage={"prompt_tokens": 3059, "completion_tokens": 9, "prompt_tokens_details": {}},
    )
    context = {"review_type": "overlay", "overlay_text": "Join our talent network"}
    generated = reasoning.generate(tmp_path, context, "popup-1", attempts=1)
    assert reasoning.completed_response(generated) == '{"label": "other"}'
    assert generated["transport"] == "direct" and generated["harness"] == "direct"
    [body] = server.requests
    assert body["temperature"] == 0 and body["stream"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == runtime.MODEL
    system = body["messages"][0]
    assert system["role"] == "system" and system["content"] == reasoning.system_prompt("overlay")
    assert "Hermes" not in system["content"]
    assert user_turn(body).endswith("\n\n/no_think")
    assert sent_context(body) == context
    # The private files keep the exact input and the server's own counts.
    assert json.loads((tmp_path / "popup-1-input.json").read_text()) == context
    stored = json.loads((tmp_path / "popup-1-result.json").read_text())
    assert stored["result"]["usage"]["prompt_tokens"] == 3059
    assert reasoning.model_facts(generated) == {
        "tokens_in": 3059,
        "tokens_out": 9,
        "transport": "direct",
    }
    assert model_client.idle_seconds() < 60  # the keepalive knows the model was used


def test_the_hermes_harness_is_a_config_switch(state, server, tmp_path, monkeypatch):
    monkeypatch.setattr(
        workflow, "config", lambda: {"hermes_python": sys.executable, "model_transport": "hermes"}
    )
    ran = []

    def harness(command, **_kwargs):
        ran.append(command)
        output = command[command.index("--output") + 1]
        result = {
            "completed": True,
            "turn_exit_reason": "text_response(finish_reason=stop)",
            "final_response": "{}",
        }
        with open(output, "w") as f:
            json.dump({"model": "m", "harness": "Hermes", "result": result}, f)
        return __import__("subprocess").CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reasoning.subprocess, "run", harness)
    generated = reasoning.generate(tmp_path, {"review_type": "job_fit"}, "job-reasoning")
    assert len(ran) == 1 and generated["transport"] == "hermes"
    assert server.requests == []  # nothing went to the server directly
    monkeypatch.setattr(workflow, "config", dict)
    assert reasoning.transport() == "direct"


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ({"reasoning_parts": ["The user wants", " me to"]}, "job_fit"),
        ({"parts": ["Okay, let me look at the posting."]}, "job_fit"),
        ({"parts": ["<think>\nFirst I will"]}, "answers"),
        (
            {"parts": ["Okay, let me fix the listed spans in the draft so it reads well."]},
            "cleanup",
        ),
        (
            {
                "parts": ['{"a":1}'],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 40}},
            },
            "job_fit",
        ),
    ],
)
def test_a_model_that_starts_reasoning_is_stopped_at_once(state, server, tmp_path, script, kind):
    filler = [" more"] * 200
    server.reply(
        *script.get("parts", ()),
        *filler,
        reasoning_parts=script.get("reasoning_parts", ()),
        usage=script.get("usage"),
    )
    with pytest.raises(model_client.ThinkingLeak):
        reasoning.generate(tmp_path, {"review_type": kind}, "probe", attempts=1)
    if "usage" not in script:
        # The stream was dropped at the first words, not read to the end.
        assert server.consumed < 5


def test_plain_prose_is_a_fine_cleanup_answer(state, server, tmp_path):
    server.reply("I built the import tool by hand. It took a week.")
    generated = reasoning.generate(tmp_path, {"review_type": "cleanup"}, "cleanup-k")
    assert reasoning.completed_response(generated).startswith("I built")


def test_server_failures_mean_the_model_is_unavailable(state, server, tmp_path):
    cases = [
        httpx.Response(500, json={"error": {"message": "engine crashed"}}),
        httpx.Response(503, json={"detail": "busy with quantization"}),
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ReadTimeout("no answer"),
    ]
    for case in cases:
        server.replies.append(case)
        with pytest.raises(model_client.ModelUnavailable):
            reasoning.generate(tmp_path, {"review_type": "job_fit"}, "probe")
    server.reply('{"a":', error="memory guard aborted this request")
    with pytest.raises(model_client.ModelUnavailable, match="mid-answer"):
        reasoning.generate(tmp_path, {"review_type": "job_fit"}, "probe")
    server.reply('{"a":', finish=None)  # the stream ends without a finish
    with pytest.raises(model_client.ModelUnavailable):
        reasoning.generate(tmp_path, {"review_type": "job_fit"}, "probe")
    server.replies.append(httpx.Response(400, json={"error": {"message": "Prompt too long"}}))
    with pytest.raises(model_client.PromptTooLong):
        reasoning.generate(tmp_path, {"review_type": "job_fit"}, "probe")
    # An output that hit the ceiling is not an answer.
    server.reply('{"answers": [', finish="length")
    with pytest.raises(RuntimeError, match="did not complete"):
        reasoning.completed_response(
            reasoning.generate(tmp_path, {"review_type": "job_fit"}, "probe")
        )


def test_an_outage_during_review_requeues_without_a_card(state, server, evidence):
    app = queued(state, "outage")
    server.replies.append(httpx.Response(502, text="bad gateway"))
    with pytest.raises(reasoning.ModelUnavailable):
        reasoning.review_job(app, {"url": "https://jobs.example.com/outage", "text": POSTING})
    assert "qwen_failure" not in events(app)
    # Any other failure is still the owner's to see.
    server.reply("Sure! Here is the review you asked for.")
    with pytest.raises(model_client.ThinkingLeak):
        reasoning.review_job(app, {"url": "https://jobs.example.com/outage", "text": POSTING})
    assert events(app) == ["qwen_failure"]
    assert reasoning.ModelUnavailable is model_client.ModelUnavailable


def test_a_prompt_over_the_budget_is_never_sent(state, server, tmp_path):
    # Nothing in this context can be trimmed: the questions are the work itself.
    questions = [{"key": f"{n:012x}", "label": "Why? " * 900} for n in range(8)]
    with pytest.raises(model_client.PromptTooLong):
        reasoning.generate(tmp_path, {"questions": questions}, "reasoning")
    assert server.requests == []
    # A long posting is trimmed to fit and sent.
    server.reply(json.dumps(FIT))
    reasoning.generate(tmp_path, {"review_type": "job_fit", "job_text": "x" * 60000}, "job")
    [body] = server.requests
    system = body["messages"][0]["content"]
    assert model_client.prompt_tokens(system, sent_context(body)) <= 12_500
    assert len(sent_context(body)["job_text"]) < 40000


# --- what the model reads ---------------------------------------------------------


def test_the_shared_part_of_each_prompt_comes_first_and_fills_a_cache_block(
    state, server, evidence
):
    first, second = queued(state, "a", title="Example — Intern"), queued(state, "b")
    for app, text in ((first, POSTING), (second, POSTING.replace("2027", "2026"))):
        server.reply(json.dumps(FIT))
        reasoning.review_job(app, {"url": f"https://jobs.example.com/{app}", "text": text})
    one, two = (user_turn(body) for body in server.requests)
    context = sent_context(server.requests[0])
    assert list(context)[:4] == ["review_type", "prompt_version", "profile", "career_evidence"]
    assert list(context)[-4:] == ["expected_job_title", "job_url", "job_text", "form_questions"]
    # Everything before the job's own fields is the same text for both jobs, and it is
    # long enough to fill at least one 2,048-token block, padding included.
    shared = next(i for i, (a, b) in enumerate(zip(one, two, strict=False)) if a != b)
    static = one[: one.index('"expected_job_title"')]
    assert shared >= len(static)
    system = server.requests[0]["messages"][0]["content"]
    # The server could not count here: the padding is the estimate's, digits (one token
    # each) after a shared part counted at its lowest.
    padding = context["cache_padding"]
    assert set(padding) == {"0"}
    unpadded = static.replace(padding, "")
    assert (
        model_client.at_least_tokens(system + unpadded) + len(padding)
        >= model_client.CACHE_BLOCK_TOKENS
    )
    assert len(server.counted) == 2  # asked each time, since no count came back
    # Padding is only what is missing: a long static part gets none from the estimate.
    long = {"profile": {"note": "word " * 4000}}
    assert reasoning.static_first("", long, {"job_text": "x"})["cache_padding"] == ""


@pytest.mark.parametrize(
    ("counted", "padding"),
    [
        (1500, 2048 - 1500 + 48),  # short of one block: fill it
        (3309, 4096 - 3309 + 48),  # a partial second block within reach: fill it too
        (2335, 0),  # the next boundary is far: no padding, the first block is reused
    ],
)
def test_the_shared_part_is_padded_on_the_servers_own_count(
    state, server, evidence, counted, padding
):
    server.count = counted
    for path in ("a", "b"):
        app = queued(state, path, title="Example — Intern")
        server.reply(json.dumps(FIT))
        reasoning.review_job(app, {"url": f"https://jobs.example.com/{path}", "text": path})
    sent = [sent_context(body) for body in server.requests]
    assert [len(c.get("cache_padding", "")) for c in sent] == [padding, padding]
    assert ("cache_padding" in sent[0]) == bool(padding)
    # The count is asked once for a shared part, then kept.
    assert len(server.counted) == 1
    [asked] = server.counted
    assert asked["system"] == reasoning.system_prompt("job_fit")
    shared = json.loads(asked["messages"][0]["content"])
    assert list(shared) == ["review_type", "prompt_version", "profile", "career_evidence"]


def test_one_evidence_read_serves_the_fit_review_and_the_drafting(state, server, evidence):
    app = queued(state, "evidence", title="Example — Intern")
    server.reply(json.dumps(FIT))
    reasoning.review_job(app, {"url": "https://jobs.example.com/evidence", "text": POSTING})
    key = "abcdef012345"
    answer = {"key": key, "kind": "proposal", "value": "Short.", "sources": ["ev_1"]}
    server.reply(json.dumps({"answers": [answer]}))
    page = {
        "profile_hash": read_approved()["profile_hash"],
        "pending": [{"key": key, "label": "Preferred name"}],
        "text": "Apply form",
    }
    reasoning.review_application(app, page)
    assert len(evidence) == 1
    drafting = sent_context(server.requests[1])
    assert list(drafting)[:5] == [
        "review_type",
        "prompt_version",
        "source_meaning",
        "profile",
        "career_evidence",
    ]
    assert drafting["career_evidence"] == sent_context(server.requests[0])["career_evidence"]
    assert list(drafting).index("questions") > list(drafting).index("career_evidence")


# --- what the model writes --------------------------------------------------------


def test_compact_answers_validate_and_the_cards_keep_what_the_owner_reads(state):
    keys = ["a" * 12, "b" * 12]
    raw = json.dumps(
        {
            "answers": [
                {
                    "key": keys[0],
                    "kind": "proposal",
                    "value": "Example University",
                    "sources": ["p"],
                },
                {"key": keys[1], "kind": "needs_user", "explanation": "Desired start date"},
            ]
        }
    )
    parsed = reasoning.parse_review(raw, set(keys))
    assert parsed["answers"][0]["explanation"] == ""
    assert parsed["answers"][1] == {
        "key": keys[1],
        "kind": "needs_user",
        "value": "",
        "sources": [],
        "explanation": "Desired start date",
    }
    answers = reasoning.system_prompt("answers")
    assert "at most 10 words" in answers and "A proposal carries no explanation" in answers
    fit = reasoning.system_prompt("job_fit")
    assert "at most 25 words" in fit and "Leave out every field" in fit
    # A review without evidence strings, nulls or originals is a complete review.
    review = reasoning.JobReview.model_validate(FIT)
    assert review.requirements[0].evidence == "" and review.requirements[0].original == ""


def test_a_non_english_posting_is_read_through_its_english_requirements(state):
    output = {
        "decision": "needs_review",
        "rationale": "Praktikum; Abschlussfenster passt.",
        "requirements": [
            {
                "kind": "graduation_window",
                "requirement": "Graduating between December 2027 and June 2028",
                "original": "Abschluss zwischen Dezember 2027 und Juni 2028",
                "status": "unknown",
                "graduation_start": "2027-12",
                "graduation_end": "2028-06",
            },
            {
                "kind": "work_authorization",
                "requirement": "Authorized to work in the US",
                "original": "Arbeitserlaubnis für die USA",
                "status": "unknown",
                "us_authorization_required": True,
            },
        ],
        "unknowns": [],
    }
    reasoning.JobReview.model_validate(output)
    profile = read_approved()["profile"]
    posting = "Praktikum Softwareentwicklung. Abschluss zwischen Dezember 2027 und Juni 2028."
    result = reasoning.evaluate_review(output, profile, posting)
    assert [r["checked_by"] for r in result["requirements"]] == ["code", "code"]
    assert result["decision"] == "fit"
    assert result["requirements"][0]["original"].startswith("Abschluss")
    assert "not in English" in reasoning.system_prompt("job_fit")


def test_a_long_form_is_drafted_in_batches_of_eight(state, server, evidence):
    app = queued(state, "long", title="Example — Intern")
    questions = [{"key": f"{n:012x}", "label": f"Question {n}"} for n in range(19)]
    for start in (0, 8, 16):
        answers = [
            {"key": q["key"], "kind": "needs_user", "explanation": "Only you know"}
            for q in questions[start : start + 8]
        ]
        server.reply(json.dumps({"answers": answers}), usage={"prompt_tokens": 100})
    page = {
        "profile_hash": read_approved()["profile_hash"],
        "pending": questions,
        "text": "",
    }
    with timing.stage(app, "drafting"):
        result = reasoning.review_application(app, page)
    assert [len(sent_context(body)["questions"]) for body in server.requests] == [8, 8, 3]
    assert [a["key"] for a in result["answers"]] == [q["key"] for q in questions]
    calls = [r for r in timing.rows() if r["stage"] == "model"]
    assert len(calls) == 3


# --- while the worker waits -------------------------------------------------------


def keep(state, application_id: str, text: str):
    path = state / "applications" / application_id / "posting.json"
    path.write_text(json.dumps({"text": text}))


def test_a_queued_posting_is_reviewed_in_the_background_and_the_pass_reads_it(
    state, server, evidence
):
    app = queued(state, "bg", title="Example — Intern")
    keep(state, app, POSTING)
    model_client.mark_used()
    server.reply(json.dumps(FIT))
    assert prereview.idle({}) == {"reviewed": 1, "pinged": False}
    assert len(server.requests) == 1
    assert events(app) == []  # nothing reaches the thread from the background
    stored = json.loads((state / "applications" / app / "job-review.json").read_text())
    assert stored["background"] is True and stored["decision"] == "fit"
    # Nothing left to review: the next idle tick asks nobody.
    assert prereview.idle({})["reviewed"] == 0 and len(server.requests) == 1
    # The pass reads a posting with the same text: no model call, one fit card.
    result = reasoning.review_job(app, {"url": "https://jobs.example.com/bg", "text": ""}, POSTING)
    assert result["cached"] is True and len(server.requests) == 1
    assert events(app) == ["qwen_job_review"]
    reasoning.review_job(app, {"url": "https://jobs.example.com/bg", "text": ""}, POSTING)
    assert events(app) == ["qwen_job_review"]
    background = [r for r in timing.rows() if r["stage"] == "fit_review"]
    assert [r["facts"].get("background") for r in background] == [True, True]
    # A page that reads differently is reviewed live, as before.
    server.reply(json.dumps(FIT))
    changed = reasoning.review_job(app, {"url": "u", "text": ""}, POSTING + "\nApply by May.")
    assert "cached" not in changed and len(server.requests) == 2


def test_background_reviews_follow_the_queue_and_wait_for_a_free_server(state, server, evidence):
    model_client.mark_used()
    low, high = queued(state, "low", source="keryx"), queued(state, "high", source="keryx")
    with workflow.db() as conn:
        from rove import intake

        intake.ensure_tables(conn)
        for app, score in ((low, 10), (high, 90)):
            intake.record_queue_score(conn, app, score, "", "")
    for app in (low, high):
        keep(state, app, POSTING + app)
    assert [row["id"] for row in prereview.queue_order()] == [high, low]
    # Someone else's request is in flight (a mail label, the Discord agent): wait.
    server.status = {"active_requests": 1, "waiting_requests": 0}
    assert prereview.review_queued() == 0 and server.requests == []
    server.status = None  # unreadable status: wait as well
    assert prereview.review_queued() == 0
    server.status = {"active_requests": 0, "waiting_requests": 0}
    server.reply(json.dumps(FIT))
    assert prereview.review_queued() == 1
    assert sent_context(server.requests[0])["job_text"] == POSTING + high
    # A review that fails is not retried in the background for the same text.
    server.reply('{"decision": "maybe"}')
    server.reply('{"decision": "perhaps"}')  # the one retry with the defect named
    assert prereview.review_queued() == 1
    assert prereview.review_queued() == 0 and len(server.requests) == 3
    assert (state / "applications" / low / prereview.FAILED_FILE).exists()


def test_an_application_no_longer_queued_or_on_an_old_profile_is_not_reviewed(
    state, server, evidence
):
    model_client.mark_used()
    app = queued(state, "parked")
    keep(state, app, POSTING)
    workflow.set_state(app, "DEFERRED")
    assert prereview.review_queued() == 0
    assert reasoning.prereview_job(app, POSTING) is None and server.requests == []


def test_the_model_is_kept_warm_only_while_jobs_wait(state, server, monkeypatch):
    model_client.mark_used()
    monkeypatch.setattr(model_client, "idle_seconds", lambda: 9 * 60)
    assert prereview.keep_warm({}) is False  # nothing queued
    queued(state, "warm")
    assert prereview.keep_warm({"model_keepalive": False}) is False
    assert prereview.keep_warm({}) is True
    [ping] = server.requests
    assert ping["max_tokens"] == 1 and ping["chat_template_kwargs"] == {"enable_thinking": False}
    server.status = {"active_requests": 1, "waiting_requests": 0}
    assert prereview.keep_warm({}) is False  # in use already
    server.status = None
    assert prereview.keep_warm({}) is False  # down: never started just to keep warm
    monkeypatch.setattr(model_client, "idle_seconds", lambda: 60)
    server.status = {"active_requests": 0, "waiting_requests": 0}
    assert prereview.keep_warm({}) is False  # used a minute ago
    assert len(server.requests) == 1


def test_idle_work_waits_for_the_first_model_request(state, server):
    app = queued(state, "fresh")
    keep(state, app, POSTING)
    assert prereview.idle({}) == {}
    assert server.requests == []
