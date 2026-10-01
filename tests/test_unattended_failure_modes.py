"""Unattended runs stop safely and say so in plain words.

With the owner policy `auto_submit`, nobody watches the recruiting browser. Whatever the
site, the model or Discord does, the application has to land in a state the owner can
act on from a phone: one card that names the next word to reply, no identifiers, and
nothing sent twice. These tests drive the worker with a scripted browser and a fake
Discord; the synthetic board in test_live_submission.py covers the click itself.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import test_workflow
from test_workflow import no_ids, owner_channels, seed_feed

from rove import discord_feed, reasoning, submission, worker, workflow
from rove.worker import apply_command, thread_command

# The approved synthetic profile and private state root, as test_workflow builds them.
state = test_workflow.state

FIELD = {"label": "First name", "name": "first", "kind": "text", "options": [], "required": True}
FIT = {"decision": "fit", "rationale": "", "unverified": [], "requirements": []}
PREPARED = {
    "pending": [],
    "package_hash": "a" * 64,
    "filled": [],
    "final_controls": [{"ref": "0", "label": "Submit application"}],
}


def page(**extra) -> dict:
    """One observed page as the browser daemon reports it; `extra` overrides the defaults."""
    base = {
        "url": "https://jobs.example.com/form",
        "observation_id": "obs-1",
        "fields": [],
        "text": "",
        "ats_markers": {},
        "application_links": [],
        "blocked": False,
        "closed": False,
    }
    return {**base, **extra}


FORM = page(fields=[FIELD])


def scripted_browser(monkeypatch, **answers) -> list[str]:
    """`browser_call` without a browser: each action returns its scripted page, a list is
    consumed in order and its last item repeats, an exception is raised. The returned
    list records the actions in order."""
    calls: list[str] = []

    def browser(action, **_kw):
        calls.append(action)
        answer = answers[action]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return json.loads(json.dumps(answer))  # a fresh copy, as the socket round trip gives

    monkeypatch.setattr(worker, "browser_call", browser)
    return calls


def ready_to_fill(monkeypatch):
    """The steps after the form is found: fit, resume and an adapter, all succeeding."""
    monkeypatch.setattr(reasoning, "review_job", lambda *a: dict(FIT))
    monkeypatch.setattr(
        worker, "prepare_resume", lambda *a: {"ready": True, "resume_sha256": "b" * 64}
    )
    monkeypatch.setattr(submission, "enabled_adapter", lambda url: object())


def enabled_worker(monkeypatch, **settings) -> tuple[list, list]:
    """An enabled worker: owner channels, an owner id and a Discord with no new messages.
    Returns the workflow's Discord calls and the lines the worker itself posted."""
    calls = owner_channels(monkeypatch)
    base = workflow.config()
    monkeypatch.setattr(
        workflow, "config", lambda: {**base, "control_channel_id": "control", **settings}
    )
    monkeypatch.setattr(worker, "private_env", lambda: {"DISCORD_OWNER_USER_ID": "owner"})
    posted = []

    def fake(method, path, payload=None):
        if method == "POST":
            posted.append((path, payload))
            return {"id": f"p{len(posted)}"}
        return []

    monkeypatch.setattr(worker, "discord", fake)
    return calls, posted


def feed_job(path: str) -> str:
    return workflow.enqueue(
        f"https://jobs.example.com/{path}", source="keryx", title="Example — Intern"
    )["application_id"]


def posts(calls, channel: str = "action") -> list[dict]:
    return [p for m, path, p in calls if m == "POST" and path == f"/channels/{channel}/messages"]


def the_card(calls, channel: str = "action") -> dict:
    """The one card an owner channel received, as Discord got it."""
    cards = posts(calls, channel)
    assert len(cards) == 1, cards
    return cards[0]["embeds"][0]


def reply_block(card: dict) -> str:
    return {f["name"]: f["value"] for f in card.get("fields", [])}["Reply"]


def nothing_sent(app: str, browser_calls: list[str]):
    assert "submit" not in browser_calls
    with workflow.db() as conn:
        for table in ("live_submission_attempts", "owner_commands"):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE application_id=?", (app,)
                ).fetchone()[0]
                == 0
            )


def event_kinds(app: str) -> list[str]:
    with workflow.db() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM application_events WHERE application_id=? ORDER BY id", (app,)
            )
        ]


def test_a_site_that_blocks_twice_hands_over_without_sending(state, monkeypatch):
    blocked = page(blocked=True, block_marker="Access Denied", text="Access Denied")
    calls = scripted_browser(monkeypatch, open=blocked, reopen=blocked)
    discord_calls = owner_channels(monkeypatch)
    monkeypatch.setattr(
        reasoning, "review_job", lambda *a: pytest.fail("a blocked page is never reviewed")
    )
    app = feed_job("blocked")
    result = worker.process(app)
    assert result["status"] == "MANUAL_TAKEOVER" and not result["submitted"]
    assert calls == ["open", "reopen"]
    assert workflow.get(app)["status"] == "MANUAL_TAKEOVER"
    card = the_card(discord_calls)
    no_ids(card)
    assert "Blocked by the employer's site" in card["description"]
    assert "Nothing was submitted" in card["description"]
    assert reply_block(card) == workflow.command_block(["applied", "park it"])
    assert event_kinds(app).count("browser_access_blocked") == 1
    nothing_sent(app, calls)
    # Both reconcile words mean the same reply; neither needs an id.
    assert thread_command("done", app)["outcome"] == "applied"
    assert thread_command("applied", app)["outcome"] == "applied"


def test_a_block_that_clears_on_the_retry_continues_to_the_fit_review(state, monkeypatch):
    blocked = page(blocked=True, block_marker="Just a moment...", text="Just a moment...")
    calls = scripted_browser(monkeypatch, open=blocked, reopen=FORM, prepare=PREPARED)
    owner_channels(monkeypatch)
    ready_to_fill(monkeypatch)
    reviewed = []
    monkeypatch.setattr(
        reasoning, "review_job", lambda app, observed, posting: reviewed.append(observed) or FIT
    )
    base = workflow.config()
    monkeypatch.setattr(workflow, "config", lambda: {**base, "auto_submit": True})
    app = feed_job("retry")
    result = worker.process(app)
    assert calls == ["open", "reopen", "prepare"]
    assert reviewed == [FORM]  # the review read the page the retry reached, not the block
    assert result["status"] == "READY_FOR_REVIEW" and result["auto_submit"]
    with workflow.db() as conn:
        queued = conn.execute(
            "SELECT kind,status FROM owner_commands WHERE application_id=?", (app,)
        ).fetchall()
    assert [tuple(r) for r in queued] == [("submit", "applied")]


def test_a_visible_captcha_hands_over_and_go_resumes_it(state, monkeypatch):
    challenge = page(ats_markers={"captcha_challenge": True, "already_applied": False})
    calls = scripted_browser(monkeypatch, open=challenge)
    discord_calls = owner_channels(monkeypatch)
    app = feed_job("captcha")
    assert worker.process(app)["status"] == "MANUAL_TAKEOVER"
    card = the_card(discord_calls)
    no_ids(card)
    assert "CAPTCHA needs you" in card["description"]
    assert "Solve it in the recruiting browser" in card["description"]
    assert reply_block(card) == workflow.command_block(["go", "park it"])
    nothing_sent(app, calls)
    with workflow.db() as conn:
        message = conn.execute("SELECT message_id FROM owner_notices").fetchone()[0]
    command = thread_command("go", app)
    assert command["kind"] == "resume"
    apply_command(command, "m-go")
    assert workflow.get(app)["status"] == "QUEUED"
    assert worker.next_queued(1) == app  # the owner's go is picked up before anything else
    assert [(m, p) for m, p, _ in discord_calls if m == "DELETE"] == [
        ("DELETE", f"/channels/action/messages/{message}")
    ]


def test_a_closed_posting_is_parked_quietly_with_a_thread_line(state, monkeypatch):
    closed = page(
        closed=True,
        closed_marker="no longer accepting applications",
        text="This job is no longer accepting applications.",
    )
    calls = scripted_browser(monkeypatch, open=closed)
    discord_calls = owner_channels(monkeypatch)
    app = feed_job("closed")
    assert worker.process(app) == {"application_id": app, "status": "DEFERRED", "submitted": False}
    assert workflow.get(app)["status"] == "DEFERRED"
    assert posts(discord_calls) == [] and posts(discord_calls, "short") == []
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0] == 0
        lifecycle = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM application_events WHERE application_id=? AND kind='lifecycle'",
                (app,),
            )
        ]
    assert (lifecycle[-1]["from"], lifecycle[-1]["to"]) == ("PREPARING", "DEFERRED")
    line = workflow.event_embeds(app, "lifecycle", lifecycle[-1])
    assert line == [
        (
            "→ Parked · posting says it no longer accepts applications "
            "(no longer accepting applications)"
        )
    ]
    no_ids(line)
    nothing_sent(app, calls)


def test_a_sign_in_wall_without_a_stored_account_asks_the_owner(state, monkeypatch):
    wall = page(
        url="https://jobs.example.com/login",
        auth_page="login",
        manual_takeover_required=True,
        fields=[
            {"label": "Email", "name": "email", "kind": "email", "options": [], "required": True}
        ],
    )
    calls = scripted_browser(monkeypatch, open=wall)
    discord_calls = owner_channels(monkeypatch)
    app = feed_job("login")
    assert worker.process(app)["status"] == "NEEDS_USER"
    assert calls == ["open"]  # no sign-in is attempted without a stored account
    card = the_card(discord_calls)
    no_ids(card)
    assert "Sign-in needs you" in card["description"]
    assert "none stored" in card["description"] and "reply `go`" in card["description"]
    assert reply_block(card) == workflow.command_block(["go", "park it"])
    nothing_sent(app, calls)


def test_a_register_wall_waits_for_the_owner_to_ask_for_the_account(state, monkeypatch):
    wall = page(url="https://jobs.example.com/signup", auth_page="register")
    calls = scripted_browser(monkeypatch, open=wall, register=FORM, prepare=PREPARED)
    discord_calls = owner_channels(monkeypatch)
    ready_to_fill(monkeypatch)
    app = feed_job("register")
    assert worker.process(app)["status"] == "NEEDS_USER"
    assert calls == ["open"]
    card = the_card(discord_calls)
    no_ids(card)
    assert (
        "Account needed" in card["description"] and "Your policy asks first" in card["description"]
    )
    assert reply_block(card) == workflow.command_block(["create account", "park it"])
    nothing_sent(app, calls)
    command = thread_command("create account", app)
    assert command["kind"] == "account"
    apply_command(command, "m-account")
    assert workflow.get(app)["status"] == "QUEUED" and workflow.owner_override(app, "account")
    # With the owner's word on record, the next run creates the account and carries on.
    calls.clear()
    assert worker.process(app)["status"] == "READY_FOR_REVIEW"
    assert calls == ["open", "register", "prepare"]


def test_a_model_outage_returns_the_application_to_the_queue_without_a_card(state, monkeypatch):
    calls = scripted_browser(monkeypatch, open=FORM)
    discord_calls, posted = enabled_worker(monkeypatch)

    def down(*_a):
        raise reasoning.ModelUnavailable("the local model server is down")

    monkeypatch.setattr(reasoning, "review_job", down)
    app = workflow.enqueue("https://jobs.example.com/model")["application_id"]
    result = worker.tick()
    assert result == {"application_id": app, "status": "QUEUED", "waiting": "model"}
    item = workflow.get(app)
    assert item["status"] == "QUEUED" and item["error"] == "model_unavailable"
    assert "model_unavailable" in event_kinds(app)
    assert not (state / "applications" / app / "error.json").exists()
    assert posts(discord_calls) == [] and posts(discord_calls, "short") == [] and posted == []
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0] == 0
    assert worker.next_queued(1) == app  # the next tick tries again
    nothing_sent(app, calls)


def test_a_discord_outage_during_a_hold_keeps_the_card_for_the_next_tick(state, monkeypatch):
    scripted_browser(monkeypatch, open=page(ats_markers={"captcha_challenge": True}))
    owner_channels(monkeypatch)

    def offline(method, path, payload=None):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(workflow, "discord", offline)
    app = feed_job("outage")
    result = worker.process(app)  # must not raise
    assert result["status"] == "MANUAL_TAKEOVER"
    assert workflow.get(app)["status"] == "MANUAL_TAKEOVER"
    with workflow.db() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT channel,delivery FROM owner_notices")]
    assert rows == [("action", "pending")]
    assert "notice" in (state / "logs/delivery-failures.log").read_text()
    discord_calls = owner_channels(monkeypatch)  # Discord is back
    workflow.flush_pending()
    card = the_card(discord_calls)
    assert "CAPTCHA needs you" in card["description"]
    with workflow.db() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT delivery,message_id FROM owner_notices")]
    assert rows == [("sent", "m1")]
    workflow.flush_pending()
    assert len(posts(discord_calls)) == 1  # delivered once, not again


def test_a_stale_preparing_row_is_handed_back_and_a_fresh_one_is_left_alone(state, monkeypatch):
    discord_calls = owner_channels(monkeypatch)
    stale, fresh = feed_job("stale"), feed_job("fresh")
    for app in (stale, fresh):
        workflow.set_state(app, "PREPARING")

    def age(app: str, minutes: int):
        with workflow.db() as conn:
            conn.execute(
                "UPDATE application_queue SET updated_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(), app),
            )

    age(stale, 16)
    age(fresh, 14)
    worker.recover_interrupted()
    assert workflow.get(stale)["status"] == "NEEDS_USER"
    assert workflow.get(stale)["error"] == "preparation_interrupted"
    assert workflow.get(fresh)["status"] == "PREPARING"
    card = the_card(discord_calls)
    no_ids(card)
    assert "Preparation interrupted" in card["description"]
    assert "Nothing was submitted" in card["description"]
    assert reply_block(card) == workflow.command_block(["go", "park it"])
    worker.recover_interrupted()  # the handed-back row is not handed back twice
    assert len(posts(discord_calls)) == 1
    apply_command(thread_command("go", stale), "m-go")
    assert workflow.get(stale)["status"] == "QUEUED"


def test_unattended_sending_keeps_the_daily_cap_and_the_gap_except_for_pasted_links(
    state, monkeypatch
):
    noon = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return noon

    monkeypatch.setattr(worker, "datetime", Clock)
    settings = {
        "enabled": True,
        "auto_submit": True,
        "max_submissions_per_day": 2,
        "min_minutes_between_submissions": 8,
    }
    monkeypatch.setattr(workflow, "config", lambda: settings)

    def attempt(app: str, minutes_ago: int):
        with workflow.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO live_submission_attempts VALUES(?,?,?,?,?)",
                (
                    app,
                    "a" * 64,
                    f"auto-submit:{app}",
                    "APPLIED",
                    (noon - timedelta(minutes=minutes_ago)).isoformat(),
                ),
            )

    feed = feed_job("paced")
    assert worker.next_queued(1) == feed
    attempt("aaaaaaaaaaa1", 90)
    attempt("aaaaaaaaaaa2", 60)
    assert worker.next_queued(1) is None  # two sent today: the cap is reached
    pasted = workflow.enqueue("https://jobs.example.com/pasted")["application_id"]
    assert worker.next_queued(1) == pasted  # a link the owner pasted is their decision
    workflow.set_state(pasted, "DEFERRED")
    settings["max_submissions_per_day"] = 10
    attempt("aaaaaaaaaaa3", 1)
    assert worker.next_queued(1) is None  # one minute since the last send: keep the gap
    attempt("aaaaaaaaaaa3", 9)
    assert worker.next_queued(1) == feed


def test_an_unknown_submission_holds_the_queue_until_the_owner_reconciles(state, monkeypatch):
    enabled_worker(monkeypatch, max_waiting_applications=2)
    processed = []
    monkeypatch.setattr(
        worker,
        "process",
        lambda app: (
            processed.append(app)
            or {"application_id": app, "status": "DEFERRED", "submitted": False}
        ),
    )
    unknown, queued = feed_job("unknown"), feed_job("next")
    workflow.set_state(unknown, "UNKNOWN_SUBMISSION", package_hash="c" * 64)
    assert worker.tick() == {"waiting_on": {"id": unknown, "status": "UNKNOWN_SUBMISSION"}}
    assert worker.tick()["waiting_on"]["id"] == unknown
    assert processed == []
    # Nothing reopens or resends an unclear attempt; only the owner's verdict moves it.
    with pytest.raises(ValueError, match="already in flight"):
        thread_command("send it", unknown)
    with pytest.raises(PermissionError):
        apply_command(thread_command("go", unknown), "m-go")
    apply_command(thread_command("not sent", unknown), "m-not-sent")
    assert workflow.get(unknown)["status"] == "NEEDS_USER"
    assert worker.tick()["status"] == "DEFERRED"
    assert processed == [queued]


def test_the_same_posting_is_one_application_however_it_arrives(state, monkeypatch):
    first = workflow.enqueue("https://jobs.example.com/batch/0?utm_source=feed")
    again = workflow.enqueue("https://jobs.example.com/batch/0")
    tracked = workflow.enqueue("https://jobs.example.com/batch/0?ref=linkedin&gclid=x")
    assert again["already_exists"] and tracked["already_exists"]
    assert again["application_id"] == first["application_id"]
    assert tracked["application_id"] == first["application_id"]
    workflow.set_state(first["application_id"], "APPLIED")
    # The feed announces a posting whose canonical link is already applied: not re-queued.
    seed_feed(state, monkeypatch, {}, 1)  # queues https://jobs.example.com/batch/0
    assert discord_feed.tick()["sent"] == 1
    with workflow.db() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT source,status FROM application_queue")]
    assert rows == [("owner_link", "APPLIED")]


def test_a_form_the_site_rejected_after_the_click_asks_for_go_in_plain_words(state, monkeypatch):
    discord_calls = owner_channels(monkeypatch)
    app = feed_job("rejected")
    workflow.set_state(app, "SUBMITTING", package_hash="c" * 64)
    with workflow.db() as conn:
        conn.execute(
            "INSERT INTO live_submission_attempts VALUES(?,?,?,?,?)",
            (app, "c" * 64, f"auto-submit:{app}", "SUBMITTING", workflow.now()),
        )
    checks = {"no_form_error": False, "post_rejected": True, "post_accepted": False}
    after = {"ats_markers": {"form_error": "Email address is invalid. Please correct it."}}
    reason = submission.GenericV1.reason(checks, after)
    submission.finish_attempt(
        app,
        "NOT_SUBMITTED",
        {
            "application_id": app,
            "package_hash": "c" * 64,
            "status": "NOT_SUBMITTED",
            "reason": reason,
        },
    )
    assert workflow.get(app)["status"] == "NEEDS_USER"
    with workflow.db() as conn:
        assert conn.execute("SELECT status FROM live_submission_attempts").fetchone()[0] == (
            "NOT_SUBMITTED"
        )
    card = the_card(discord_calls)
    no_ids(card)
    assert "The site rejected the form" in card["description"]
    assert "nothing was sent" in card["description"]
    assert "Email address is invalid" in card["description"]
    assert reply_block(card) == workflow.command_block(["go", "park it"])
    thread = workflow.event_embeds(app, "needs_action", workflow.latest_hold(app))[0]
    no_ids(thread)
    assert reply_block(thread) == workflow.command_block(["go", "park it"])
    apply_command(thread_command("go", app), "m-go")
    assert workflow.get(app)["status"] == "QUEUED"


def test_a_reply_that_cannot_apply_gets_one_plain_line_and_changes_nothing(state, monkeypatch):
    discord_calls = owner_channels(monkeypatch)
    app = feed_job("reply")
    workflow.set_state(app, "NEEDS_USER", thread_id="t1")
    workflow.action_needed(
        app, "A question needs you.", commands=["go", "park it"], headline="Answers needed"
    )
    monkeypatch.setattr(worker, "private_env", lambda: {"DISCORD_OWNER_USER_ID": "owner"})
    monkeypatch.setattr(worker, "discord", lambda *a: [])
    worker.poll_commands()  # establishes the thread's cursor
    with workflow.db() as conn:
        cursor = int(
            conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id='t1'"
            ).fetchone()[0]
        )
    replies = ["send it", "applied", "thanks, looks good"]
    posted = []

    def fake(method, path, payload=None):
        if method == "GET" and path.startswith("/channels/t1/"):
            return [
                {"id": str(cursor + n), "author": {"id": "owner"}, "content": text}
                for n, text in enumerate(replies, start=1)
            ]
        if method == "POST":
            posted.append((path, payload["content"]))
            return {"id": f"p{len(posted)}"}
        return []

    monkeypatch.setattr(worker, "discord", fake)
    worker.poll_commands()
    assert posted == [
        ("/channels/t1/messages", "Nothing is ready to send here yet."),
        (
            "/channels/t1/messages",
            "Only an unknown submission or a blocked application can be reconciled",
        ),
        ("/channels/t1/messages", worker.HELP_LINE),
    ]
    for _, line in posted:
        no_ids(line)
    assert workflow.get(app)["status"] == "NEEDS_USER"
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_commands").fetchone()[0] == 0
        notices = [tuple(r) for r in conn.execute("SELECT delivery FROM owner_notices")]
    assert notices == [("sent",)]  # the standing card is untouched
    assert len(posts(discord_calls)) == 1
    assert event_kinds(app).count("needs_action") == 1
