"""Discord delivery: batching within Discord's limits, the delivery window, crash safety,
refusals, closed threads, and the worker's reading of owner messages.

The real `discord()` runs against a synthetic Discord served by an httpx mock transport.
Nothing leaves the machine; every id, name and value here is made up.
"""

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rove import delivery, discord_feed, worker, workflow
from rove.onboarding import approve, digest, draft, propose

THREAD = "5001"
SETTINGS = {
    "enabled": True,
    "guild_id": "900",
    "forum_channel_id": "1000",
    "action_channel_id": "1001",
    "shortlist_channel_id": "1002",
    "control_channel_id": "1003",
    "system_channel_id": "1004",
    "memory_channel_id": "1006",
}


class FakeDiscord:
    """Answers every route the worker uses; `refuse` scripts the next answers to a route."""

    def __init__(self):
        self.requests: list[tuple] = []
        self.scripted: list[tuple] = []
        self.messages: dict = {}
        self.channels: list = []
        self.active: dict | list = {"threads": [], "members": []}
        self.counter = 0

    def refuse(self, method, prefix, status, code=None, times=1, when=None):
        body = {"message": "refused", "code": code} if code else {"message": "refused"}
        for _ in range(times):
            self.scripted.append((method, prefix, status, body, when))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        body = None
        kind = request.headers.get("content-type", "")
        if request.content and kind.startswith("application/json"):
            body = json.loads(request.content)
        elif kind.startswith("multipart/form-data"):
            text = request.content.decode("utf-8", "replace")
            payload = text.split('name="payload_json"', 1)[1].split("\r\n\r\n", 1)[1]
            body = {**json.loads(payload.split("\r\n--", 1)[0]), "file": True}
        self.requests.append((request.method, path, body))
        for index, (method, prefix, status, answer, when) in enumerate(self.scripted):
            if request.method == method and path.startswith(prefix) and (not when or when(body)):
                del self.scripted[index]
                return httpx.Response(status, json=answer, request=request)
        if request.method == "POST" and path.endswith("/messages"):
            self.counter += 1
            return httpx.Response(200, json={"id": str(9000 + self.counter)}, request=request)
        if request.method == "POST" and path.endswith("/threads"):
            return httpx.Response(200, json={"id": THREAD}, request=request)
        if request.method == "GET" and path == "/guilds/900/channels":
            return httpx.Response(200, json=self.channels, request=request)
        if request.method == "GET" and path == "/guilds/900/threads/active":
            return httpx.Response(200, json=self.active, request=request)
        if request.method == "GET" and "/messages" in path:
            channel = path.split("/")[2]
            return httpx.Response(200, json=self.messages.pop(channel, []), request=request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": path.split("/")[-1], "thread_metadata": {}}, request=request
            )
        if request.method == "DELETE":
            return httpx.Response(204, request=request)
        return httpx.Response(200, json={}, request=request)

    def posts(self, channel: str) -> list[dict]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path == f"/channels/{channel}/messages"
        ]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    return tmp_path / "state"


@pytest.fixture
def fake(monkeypatch, state):
    server = FakeDiscord()
    transport = httpx.MockTransport(lambda request: server.handler(request))
    http = httpx.Client(base_url=discord_feed.API, transport=transport)
    monkeypatch.setattr(discord_feed, "client", lambda: http)
    monkeypatch.setattr(discord_feed, "private_env", lambda: {"DISCORD_BOT_TOKEN": "synthetic"})
    monkeypatch.setattr(discord_feed.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workflow, "config", lambda: dict(SETTINGS))
    return server


def application(status: str = "PREPARING") -> str:
    app = workflow.enqueue("https://jobs.example.com/delivery")["application_id"]
    workflow.set_state(app, status, thread_id=THREAD)
    return app


def deliveries(app: str) -> list[tuple]:
    with workflow.db() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT kind,delivery FROM application_events WHERE application_id=? ORDER BY id",
                (app,),
            )
        ]


def card(title: str, *, fields=(), description: str = "") -> dict:
    return workflow.embed(title, description, fields=fields)


# --- packing within Discord's limits -------------------------------------------------


def test_rows_of_a_stage_become_one_message_not_one_per_row():
    entries = [(n, [card(f"Draft {n}")], False) for n in range(1, 8)]
    messages = delivery.pack(entries)
    assert len(messages) == 1
    assert [e["title"] for e in messages[0].embeds] == [f"Draft {n}" for n in range(1, 8)]
    assert messages[0].rows == list(range(1, 8))
    lines = delivery.pack([(n, [f"→ step {n}"], False) for n in range(1, 10)])
    assert len(lines) == 1 and lines[0].payload("n")["content"].count("\n") == 8


def test_a_message_holds_at_most_ten_embeds():
    messages = delivery.pack([(n, [card(f"Card {n}")], False) for n in range(11)])
    assert [len(m.embeds) for m in messages] == [10, 1]


def test_content_stays_under_two_thousand_characters():
    line = "→ " + "x" * 700
    messages = delivery.pack([(n, [line], False) for n in range(4)])
    assert [len(m.lines) for m in messages] == [2, 2]
    assert all(len(m.payload("n")["content"]) <= delivery.CONTENT_LIMIT for m in messages)
    huge = delivery.pack([(1, ["→ " + "y" * 5000], False)])
    assert len(huge[0].payload("n")["content"]) <= delivery.CONTENT_LIMIT


def test_embeds_of_one_message_stay_under_six_thousand_characters():
    heavy = card("Heavy", description="z" * 1900)
    messages = delivery.pack([(n, [heavy], False) for n in range(5)])
    assert len(messages) == 2
    for message in messages:
        assert sum(delivery.embed_size(e) for e in message.embeds) <= delivery.EMBED_TOTAL


def test_a_card_over_the_limits_is_split_not_refused():
    fields = [(f"Question {n} " + "q" * 200, "a" * 1500, True) for n in range(40)]
    big = {"title": "T" * 400, "color": 1, "description": "d" * 5000, "footer": {"text": "f"}}
    big["fields"] = [{"name": n, "value": v, "inline": i} for n, v, i in fields]
    parts = delivery.fit_embed(big)
    assert len(parts) > 2
    assert sum(len(p.get("fields", [])) for p in parts) == 40  # nothing dropped
    for part in parts:
        assert delivery.embed_size(part) <= delivery.EMBED_TOTAL
        assert len(part.get("fields", [])) <= delivery.FIELDS_PER_EMBED
        assert delivery.units(part["title"]) <= delivery.TITLE_LIMIT
        assert delivery.units(part.get("description", "")) <= delivery.DESCRIPTION_LIMIT
        for item in part.get("fields", []):
            assert delivery.units(item["value"]) <= delivery.FIELD_VALUE_LIMIT
            assert delivery.units(item["name"]) <= delivery.FIELD_NAME_LIMIT
    assert parts[1]["title"].endswith("(continued)") and "footer" in parts[-1]
    many = {"title": "Form", "fields": [{"name": str(n), "value": "v"} for n in range(30)]}
    assert [len(p["fields"]) for p in delivery.fit_embed(many)] == [25, 5]
    for message in delivery.pack([(1, [big], False)]):
        assert sum(delivery.embed_size(e) for e in message.embeds) <= delivery.EMBED_TOTAL


def test_sizes_count_what_discord_counts():
    assert delivery.units("✅") == 1 and delivery.units("😀") == 2
    assert delivery.units(delivery.clip("😀" * 600, 1024)) <= 1024


def test_reading_order_keeps_lines_after_the_cards_they_follow():
    form = card("Form filled · 3 fields")
    messages = delivery.pack(
        [
            (1, ["→ Opened"], False),
            (2, [form, "→ 1 question left"], False),
            (3, [card("Draft 1")], False),
            (4, [{"attachment": "/nowhere/resume.pdf", "line": "→ Resume"}], False),
            (5, ["→ after the file"], False),
        ]
    )
    shown = [(m.lines, [e["title"] for e in m.embeds], bool(m.file)) for m in messages]
    assert shown == [
        (["→ Opened"], ["Form filled · 3 fields"], False),
        (["→ 1 question left"], ["Draft 1"], False),
        (["→ Resume"], [], True),
        (["→ after the file"], [], False),
    ]


def test_drafting_rows_list_their_cards_before_the_owner_questions():
    rows = [
        {"id": 1, "kind": "opened"},
        {"id": 2, "kind": "qwen_answer_proposal"},
        {"id": 3, "kind": "qwen_question"},
        {"id": 4, "kind": "qwen_answer_proposal"},
        {"id": 5, "kind": "qwen_question"},
        {"id": 6, "kind": "lifecycle"},
    ]
    assert [r["id"] for r in delivery.reading_order(rows)] == [1, 2, 4, 3, 5, 6]


# --- the thread record ---------------------------------------------------------------


def test_a_stage_is_delivered_as_one_message_with_a_stable_nonce(fake):
    app = application()
    for n in range(4):
        workflow.record(app, "opened", {"title": f"Page {n}"})
    workflow.record(app, "qwen_failure", {"phase": "answer_drafting", "reason": "synthetic"})
    workflow.flush_events(app)
    posts = fake.posts(THREAD)
    assert len(posts) == 1
    assert posts[0]["content"].count("→ Opened") == 4 and len(posts[0]["embeds"]) == 1
    assert posts[0]["enforce_nonce"] and len(posts[0]["nonce"]) <= 25
    assert {d for _, d in deliveries(app)} == {"sent"}
    workflow.flush_events(app)
    assert len(fake.posts(THREAD)) == 1  # nothing twice


def test_inside_the_window_only_the_hold_card_and_status_go_out(fake):
    app = application()
    with workflow.delivery_window():
        workflow.record(app, "opened", {"title": "Posting"})
        workflow.flush_events(app)
        workflow.system_line(app, "fit · fit")
        assert fake.posts(THREAD) == [] and fake.posts("1004") == []
        workflow.set_state(app, "NEEDS_USER")
        workflow.action_needed(app, "One question.", commands=["go"], headline="Answers needed")
        # The hold: the owner's card first, then the thread in order, at once.
        posted = [p for m, p, _ in fake.requests if m == "POST"]
        assert posted[:2] == ["/channels/1001/messages", f"/channels/{THREAD}/messages"]
        thread = fake.posts(THREAD)[0]
        assert thread["content"] == "→ Opened · Posting"
        assert thread["embeds"][0]["title"] == "Answers needed"
        workflow.record(app, "opened", {"title": "After the hold"})
        workflow.flush_events(app)
        assert len(fake.posts(THREAD)) == 1 and fake.posts("1004") == []
    # The window's end delivers what waited: the thread, then the system log in one message.
    assert fake.posts(THREAD)[-1]["content"] == "→ Opened · After the hold"
    lines = fake.posts("1004")
    assert len(lines) == 1 and lines[0]["content"].count(f"`{app}`") >= 2


def test_files_wait_for_the_window_and_are_the_file_as_it_was(fake, state):
    app = application()
    shot = state / "applications" / app / "browser.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"first")
    with workflow.delivery_window():
        assert workflow.attach_file(app, shot, "→ What the browser showed")
        shot.write_bytes(b"later page")
        assert fake.posts(THREAD) == []
    upload = fake.posts(THREAD)[-1]
    assert upload["file"] and upload["content"] == "→ What the browser showed"
    assert not list((state / "applications" / app / "outbox").iterdir())  # copy removed


def test_an_interrupted_batch_resumes_where_it_stopped_with_the_same_nonces(fake):
    app = application()
    note = {f"detail {n}": "r" * 390 + str(n) for n in range(10)}
    for _ in range(3):
        workflow.record(app, "synthetic_note", note)  # each card fills most of a message
    original = fake.handler
    nonces = []

    def lose_second(request):
        if request.method == "POST" and request.url.path.endswith(f"/{THREAD}/messages"):
            nonces.append(json.loads(request.content)["nonce"])
            if len(nonces) == 2:
                raise httpx.ReadTimeout("response lost", request=request)
        return original(request)

    fake.handler = lose_second
    workflow.flush_events(app)
    assert [d for _, d in deliveries(app)] == ["sent", "sending", "sending"]
    fake.handler = original
    workflow.flush_pending()  # the next tick
    delivered = [b["nonce"] for b in fake.posts(THREAD)]
    # The first message is not posted again; the unconfirmed one keeps its nonce, which
    # Discord enforces, so it cannot appear twice.
    assert delivered == [nonces[0], nonces[1], nonces[1][:-1] + "2"]
    assert [d for _, d in deliveries(app)] == ["sent", "sent", "sent"]


def test_rows_survive_a_crash_after_they_were_claimed(fake):
    app = application()
    workflow.record(app, "opened", {"title": "Posting"})
    workflow.record(app, "resume_prepared", {"tailored": False})
    token = delivery.claim(
        delivery.Box("application_events", app, THREAD, "e", True, "event")
    )  # the process died right here
    assert token
    workflow.record(app, "opened", {"title": "Later"})
    workflow.flush_pending()
    posts = fake.posts(THREAD)
    assert posts[0]["nonce"] == f"e{token}.0"
    assert "Resume ready" in posts[0]["content"] and "Later" in posts[1]["content"]
    assert {d for _, d in deliveries(app)} == {"sent"}


def test_an_outage_keeps_rows_waiting_and_costs_one_call(fake):
    app = application()
    other = workflow.enqueue("https://jobs.example.com/other")["application_id"]
    workflow.set_state(other, "PREPARING", thread_id="5002")
    for target in (app, other):
        workflow.record(target, "opened", {"title": "Posting"})
    original = fake.handler

    def offline(request):
        fake.requests.append((request.method, request.url.path, None))
        raise httpx.ConnectError("no route", request=request)

    fake.handler = offline
    fake.requests.clear()
    workflow.flush_pending()
    assert len(fake.requests) == 1  # the first failure ends the pass
    assert {d for _, d in deliveries(app) + deliveries(other)} == {"pending"}
    fake.handler = original
    workflow.flush_pending()
    assert {d for _, d in deliveries(app) + deliveries(other)} == {"sent"}


def test_a_refused_message_is_retried_three_times_then_failed_with_one_line(fake, state):
    app = application()
    workflow.record(app, "opened", {"title": "Refused"})
    fake.refuse("POST", f"/channels/{THREAD}/messages", 400, 50035, times=3)
    for _ in range(3):
        workflow.flush_events(app)
    assert deliveries(app) == [("opened", "failed")]
    lines = [p["content"] for p in fake.posts("1004")]
    assert len([line for line in lines if "given up" in line]) == 1
    workflow.record(app, "opened", {"title": "Next"})
    workflow.flush_events(app)
    workflow.flush_events(app)
    assert deliveries(app)[-1] == ("opened", "sent")  # never retried forever, never blocking
    assert len([p for p in fake.posts(THREAD) if "Refused" in (p.get("content") or "")]) == 3


def test_a_refused_row_is_retried_alone_so_the_rest_get_through(fake):
    app = application()
    workflow.record(app, "opened", {"title": "One"})
    workflow.record(app, "opened", {"title": "Two"})
    fake.refuse("POST", f"/channels/{THREAD}/messages", 400, 50035)
    workflow.flush_events(app)
    workflow.flush_events(app)
    contents = [p["content"] for p in fake.posts(THREAD)]
    assert contents[-2:] == ["→ Opened · One", "→ Opened · Two"]


def test_an_archived_thread_is_reopened_once(fake):
    app = application()
    workflow.record(app, "opened", {"title": "Posting"})
    fake.refuse("POST", f"/channels/{THREAD}/messages", 400, 50083)
    workflow.flush_events(app)
    patches = [b for m, p, b in fake.requests if m == "PATCH" and p == f"/channels/{THREAD}"]
    assert patches == [{"archived": False}]
    assert deliveries(app) == [("opened", "sent")]


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [(403, 160005, "locked"), (404, 10003, "deleted")],
)
def test_a_locked_or_deleted_thread_stops_with_one_line(fake, status, code, reason):
    app = application()
    workflow.record(app, "opened", {"title": "Posting"})
    fake.refuse("POST", f"/channels/{THREAD}/messages", status, code)
    workflow.flush_events(app)
    assert deliveries(app) == [("opened", "closed")]
    closed = [p["content"] for p in fake.posts("1004") if "thread closed" in p["content"]]
    assert len(closed) == 1 and reason in closed[0]
    before = len(fake.requests)
    workflow.record(app, "opened", {"title": "Later"})
    workflow.flush_events(app)
    workflow.set_state(app, "DEFERRED")  # the status card is not edited either
    assert len(fake.requests) == before and deliveries(app)[-1] == ("opened", "closed")


def test_a_locked_thread_found_by_lookup_is_not_reopened(fake):
    app = application()
    workflow.record(app, "opened", {"title": "Posting"})
    fake.refuse("POST", f"/channels/{THREAD}/messages", 403, 50013)
    original = fake.handler

    def locked(request):
        if request.method == "GET" and request.url.path.endswith(f"/channels/{THREAD}"):
            return httpx.Response(
                200, json={"thread_metadata": {"archived": True, "locked": True}}, request=request
            )
        return original(request)

    fake.handler = locked
    workflow.flush_events(app)
    assert not [r for r in fake.requests if r[0] == "PATCH" and r[1] == f"/channels/{THREAD}"]
    assert deliveries(app) == [("opened", "closed")]


# --- owner cards and the status card --------------------------------------------------


def test_an_owner_card_refused_three_times_is_failed_and_does_not_block_others(fake):
    first = application("NEEDS_USER")
    second = workflow.enqueue("https://jobs.example.com/second")["application_id"]
    workflow.set_state(second, "NEEDS_USER", thread_id="5002")
    refused = lambda body: "Refused card." in json.dumps(body)
    fake.refuse("POST", "/channels/1001/messages", 400, 50035, times=3, when=refused)
    workflow.action_needed(first, "Refused card.", commands=["go"])
    workflow.action_needed(second, "Good card.", commands=["go"])
    workflow.flush_notices()
    with workflow.db() as conn:
        rows = dict(conn.execute("SELECT application_id,delivery FROM owner_notices").fetchall())
    assert rows == {first: "failed", second: "sent"}


def test_the_status_card_is_edited_only_when_it_changes(fake):
    app = application()
    workflow.refresh_status(app)
    workflow.refresh_status(app)
    workflow.set_state(app, "PREPARING")
    edits = [r for r in fake.requests if r[0] == "PATCH" and "/messages/" in r[1]]
    assert len(edits) == 1
    workflow.set_state(app, "DEFERRED")
    assert len([r for r in fake.requests if r[0] == "PATCH" and "/messages/" in r[1]]) == 2


def test_hold_cards_list_every_open_question_with_its_reply(fake):
    app = application("NEEDS_USER")
    questions = [
        {"key": f"{n:012x}", "label": f"Synthetic question {n}?", "state": "open"}
        for n in range(1, 12)
    ]
    commands = [f"{n}: " for n in range(1, 5)] + ["go", "park it"]
    workflow.action_needed(app, "11 questions.", questions=questions, commands=commands)
    channel_card = fake.posts("1001")[0]["embeds"][0]
    listed = "\n".join(f["value"] for f in channel_card["fields"] if "`" in f["value"])
    for n in range(1, 12):
        assert f"`{n}:` Synthetic question {n}?" in listed
    reply = next(f["value"] for f in channel_card["fields"] if f["name"] == "Reply")
    assert reply == workflow.command_block(["go", "park it"])
    thread = fake.posts(THREAD)[0]["embeds"][0]
    assert "`11:` Synthetic question 11?" in "\n".join(f["value"] for f in thread["fields"])
    status = [b for m, p, b in fake.requests if m == "PATCH" and "/messages/" in p][-1]
    replies = next(f["value"] for f in status["embeds"][0]["fields"] if f["name"] == "Reply")
    assert replies == workflow.command_block(
        ["N: your answer  (questions 1 to 4)", "go", "park it"]
    )


def test_a_long_question_list_is_cut_on_the_channel_card_and_whole_in_the_thread(fake):
    app = application("NEEDS_USER")
    questions = [
        {"key": f"{n:012x}", "label": f"Question {n} " + "w" * 100, "state": "open"}
        for n in range(1, 61)
    ]
    workflow.action_needed(app, "Many questions.", questions=questions, commands=["go"])
    channel_card = fake.posts("1001")[0]["embeds"][0]
    assert delivery.embed_size(channel_card) <= delivery.EMBED_TOTAL
    text = "\n".join(f["value"] for f in channel_card["fields"])
    assert "`1:` Question 1" in text and "more in the thread" in text
    thread_text = "\n".join(
        f["value"] for p in fake.posts(THREAD) for e in p.get("embeds", []) for f in e["fields"]
    )
    assert all(f"`{n}:` Question {n} " in thread_text for n in range(1, 61))
    for post in fake.posts(THREAD):
        assert sum(delivery.embed_size(e) for e in post.get("embeds", [])) <= delivery.EMBED_TOTAL


# --- the transport ---------------------------------------------------------------------


def test_one_client_serves_the_process_and_a_transport_error_replaces_it(monkeypatch):
    discord_feed.drop_client()
    first = discord_feed.client()
    assert discord_feed.client() is first
    discord_feed.drop_client()
    second = discord_feed.client()
    assert second is not first and first.is_closed
    discord_feed.drop_client()


def test_a_stale_connection_is_retried_once_only_when_that_cannot_post_twice(fake, monkeypatch):
    attempts = []
    original = fake.handler

    def stale_once(request):
        attempts.append(request.method)
        if len(attempts) == 1:
            raise httpx.RemoteProtocolError("server closed the idle connection", request=request)
        return original(request)

    fake.handler = stale_once
    assert discord_feed.discord("GET", "/channels/1001/messages?limit=1") == []
    assert attempts == ["GET", "GET"]
    attempts.clear()
    with pytest.raises(httpx.RemoteProtocolError):
        discord_feed.discord("POST", "/channels/1001/messages", {"content": "no nonce"})
    assert attempts == ["POST"]
    attempts.clear()
    discord_feed.discord(
        "POST", "/channels/1001/messages", {"content": "x", "nonce": "n1", "enforce_nonce": True}
    )
    assert attempts == ["POST", "POST"]


def test_the_empty_channel_cursor_comes_from_discords_clock(fake):
    discord_feed._seen.date = None
    assert discord_feed.empty_cursor() == "0"
    original = fake.handler
    discord_time = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

    def dated(request):
        response = original(request)
        response.headers["date"] = "Sat, 03 Oct 2026 12:00:00 GMT"
        return response

    fake.handler = dated
    discord_feed.discord("GET", "/channels/1001/messages?limit=1")
    cursor = int(discord_feed.empty_cursor())
    stamp = datetime.fromtimestamp(((cursor >> 22) + discord_feed.DISCORD_EPOCH_MS) / 1000, UTC)
    assert stamp == discord_time - timedelta(minutes=1)


# --- reading owner messages --------------------------------------------------------------


def owner(monkeypatch):
    monkeypatch.setattr(worker, "private_env", lambda: {"DISCORD_OWNER_USER_ID": "42"})
    monkeypatch.setattr(worker, "discord", discord_feed.discord)


def test_a_cursor_never_comes_from_the_local_clock(fake, monkeypatch):
    owner(monkeypatch)
    discord_feed._seen.date = None
    worker.poll_commands()
    with workflow.db() as conn:
        cursors = dict(conn.execute("SELECT channel_id,message_id FROM workflow_checkpoints"))
    assert set(cursors) == {"1001", "1002", "1003", "1004", "1006"}  # 1006 is #memory
    assert set(cursors.values()) == {"0"}  # Discord sent no clock; never this Mac's
    fake.messages["1003"] = [{"id": "7", "author": {"id": "42"}, "content": "resume abcdef012345"}]
    applied = []
    monkeypatch.setattr(worker, "apply_command", lambda command, message: applied.append(command))
    worker.poll_commands()
    assert applied == [{"kind": "resume", "application_id": "abcdef012345"}]


def test_a_transport_error_while_reading_does_not_stop_the_tick(fake, monkeypatch):
    owner(monkeypatch)
    original = fake.handler

    def asleep(request):
        fake.requests.append((request.method, request.url.path, None))
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    fake.handler = asleep
    assert worker.poll_commands() is False
    assert len(fake.requests) == 1  # one failed read is enough for this tick
    application_id = workflow.enqueue("https://jobs.example.com/queued")["application_id"]
    monkeypatch.setattr(worker, "process", lambda _a: pytest.fail("no pass without Discord"))
    result = worker.tick()
    assert result == {"idle": True, "discord": "unreachable"}
    assert workflow.get(application_id)["status"] == "QUEUED"
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner_notices").fetchone()[0] == 0
    fake.handler = original
    assert worker.poll_commands() is True


def test_a_server_error_or_a_refused_channel_is_routine(fake, monkeypatch):
    owner(monkeypatch)
    worker.poll_commands()  # cursors
    with workflow.db() as conn:
        conn.execute("DELETE FROM poll_marks")
    fake.refuse("GET", "/channels/1001/messages", 403, 50001)
    fake.messages["1003"] = [{"id": "8", "author": {"id": "42"}, "content": "resume abcdef012345"}]
    applied = []
    monkeypatch.setattr(worker, "apply_command", lambda command, message: applied.append(command))
    assert worker.poll_commands() is True
    assert applied == [{"kind": "resume", "application_id": "abcdef012345"}]
    fake.refuse("GET", "/guilds/900/channels", 503)  # the guild-wide read of the next tick
    assert worker.poll_commands() is False


def test_an_idle_tick_reads_only_channels_with_something_new(fake, monkeypatch):
    owner(monkeypatch)
    app = application("NEEDS_USER")
    parked = workflow.enqueue("https://jobs.example.com/parked")["application_id"]
    workflow.set_state(parked, "DEFERRED", thread_id="5009")
    worker.poll_commands()  # first sight: every channel, full read
    with workflow.db() as conn:
        cursors = dict(conn.execute("SELECT channel_id,message_id FROM workflow_checkpoints"))
    fake.channels = [
        {"id": c, "last_message_id": cursors.get(c)} for c in ("1001", "1002", "1003", "1004")
    ]
    fake.channels[0]["last_message_id"] = "77777"  # a new message in action-needed
    fake.active = {"threads": [{"id": THREAD, "last_message_id": cursors[THREAD]}]}
    fake.requests.clear()
    worker.poll_commands()
    reads = [p for m, p, _ in fake.requests if m == "GET"]
    assert reads == [
        "/guilds/900/channels",
        "/guilds/900/threads/active",
        "/channels/1001/messages",
        "/channels/1006/messages",  # the memory channel keeps its own reading
    ]
    with workflow.db() as conn:
        moved = conn.execute(
            "SELECT message_id FROM workflow_checkpoints WHERE channel_id='1001'"
        ).fetchone()[0]
    assert moved == "77777"  # the newest message was deleted: its id still moves the cursor
    assert app  # the held thread was skipped: nothing past its cursor


def test_edits_are_not_read(fake, monkeypatch):
    """By design: an edited message keeps its id, so it is never applied again."""
    owner(monkeypatch)
    worker.poll_commands()
    fake.messages["1003"] = [{"id": "9", "author": {"id": "42"}, "content": "defer abcdef012345"}]
    applied = []
    monkeypatch.setattr(worker, "apply_command", lambda command, message: applied.append(command))
    worker.poll_commands()
    with workflow.db() as conn:
        conn.execute("DELETE FROM poll_marks")
    fake.messages["1003"] = []  # Discord returns only messages after the cursor
    worker.poll_commands()
    assert len(applied) == 1


def test_no_pid_check_closes_a_parents_client(monkeypatch):
    discord_feed.drop_client()
    first = discord_feed.client()
    monkeypatch.setattr(discord_feed, "_client", {"client": first, "pid": os.getpid() + 1})
    second = discord_feed.client()
    assert second is not first and not first.is_closed
    first.close()
    discord_feed.drop_client()
