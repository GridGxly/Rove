"""The chat agent's tools: a quiet log line per call, plain failures, plain answers.

The owner reads agent-control on his phone. Tool calls never show there; each one is a
line in the system log without its arguments or result. The answers to his everyday
questions are written by code, so they are checked here word for word where it matters.
"""

import asyncio
import json
import re
import stat
from datetime import UTC, datetime

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rove import chat, discord_feed, server, workflow

SETTINGS = {
    "enabled": True,
    "guild_id": "g1",
    "control_channel_id": "control",
    "action_channel_id": "action",
    "shortlist_channel_id": "short",
    "system_channel_id": "sys",
    "max_submissions_per_day": 12,
}
SECRET = "alex.example@inbox.example.org"


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A private state root with a workflow config and an empty queue."""
    root = tmp_path / "state"
    monkeypatch.setenv("ROVE_STATE_DIR", str(root))
    (root / "config").mkdir(parents=True)
    (root / "config/workflow.json").write_text(json.dumps(SETTINGS))
    return root


@pytest.fixture
def lines(monkeypatch):
    """System-log lines the tool wrapper writes, in order."""
    posted = []
    monkeypatch.setattr(server, "post_line", posted.append)
    return posted


def call(name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, arguments or {}))
    assert not result.is_error
    return json.loads(result.content[0].text)


def add(application_id: str, title: str, status: str, **values):
    stamp = values.pop("updated_at", workflow.now())
    with workflow.db() as conn:
        conn.execute(
            "INSERT INTO application_queue(id,url,source_url,source,title,status,profile_hash,"
            "created_at,updated_at,thread_id,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                application_id,
                f"https://jobs.example.org/{application_id}",
                f"https://jobs.example.org/{application_id}",
                values.get("source", "feed"),
                title,
                status,
                "p" * 64,
                stamp,
                stamp,
                values.get("thread_id"),
                values.get("error"),
            ),
        )


def card(application_id: str, channel: str, payload: dict):
    with workflow.db() as conn:
        conn.execute(
            "INSERT INTO owner_notices(application_id,channel,data,created_at,delivery,message_id) "
            "VALUES(?,?,?,?,'sent','m1')",
            (application_id, channel, json.dumps(payload), workflow.now()),
        )


def sent(application_id: str, status: str = "APPLIED", when: str | None = None):
    with workflow.db() as conn:
        conn.execute(
            "INSERT INTO live_submission_attempts(application_id,package_hash,owner_message_id,"
            "status,created_at) VALUES(?,?,?,?,?)",
            (application_id, "h" * 64, "o-" + application_id, status, when or workflow.now()),
        )


def plain_words(text: str):
    """What the owner reads: no ids, hashes, field keys or state names in capitals."""
    assert not re.search(r"\b[0-9a-f]{12,}\b", text)
    assert not re.search(r"\b[A-Z]{3,}_[A-Z_]+\b", text)
    for state in workflow.STATES:
        assert not re.search(rf"\b{state}\b", text), state


# --- the wrapper ------------------------------------------------------------------


def test_each_call_writes_one_quiet_line_without_arguments_or_result(state, lines):
    add("a00000000001", "Example Labs — Intern 5550100199", "QUEUED")
    said = call("company_history", {"company": "Example Labs"})["say"]
    assert "5550100199" in said  # the answer carries what the owner asked about
    assert len(lines) == 1
    line = lines[0]
    assert re.fullmatch(r"looked up one company · \d+\.\d s · ok", line)
    assert "Example" not in line and "5550100199" not in line


def test_a_failure_is_one_plain_sentence_and_one_failed_line(state, lines, monkeypatch):
    with pytest.raises(ToolError) as refused:
        asyncio.run(server.mcp.call_tool("read_candidate_section", {"section": "identity"}))
    assert "Could not finish: No candidate profile has been approved" in str(refused.value)
    assert re.fullmatch(r"read the approved profile · \d+\.\d s · failed · ValueError", lines[-1])

    def crash():
        raise RuntimeError(f"Traceback: /Users/example/state {SECRET}")

    monkeypatch.setattr(server.chat, "status", crash)
    with pytest.raises(ToolError) as broken:
        asyncio.run(server.mcp.call_tool("rove_status", {}))
    text = str(broken.value)
    assert "Something broke on Rove's side while it read the status" in text
    assert "Traceback" not in text and SECRET not in text and "/Users" not in text
    assert lines[-1].endswith("failed · RuntimeError")
    assert SECRET not in " ".join(lines)


def test_async_tools_stay_async_and_their_failures_are_plain(state, lines, monkeypatch):
    tool = server.mcp._tool_manager.get_tool("read_career_evidence")
    assert tool.is_async

    async def offline(query):
        raise OSError("connection refused to 127.0.0.1:9999")

    monkeypatch.setattr(server, "career_evidence", offline)
    with pytest.raises(ToolError) as broken:
        asyncio.run(server.mcp.call_tool("read_career_evidence", {"query": SECRET}))
    assert "while it read career evidence" in str(broken.value)
    assert "127.0.0.1" not in str(broken.value)
    assert lines == [lines[0]] and lines[0].startswith("read career evidence · ")
    assert SECRET not in lines[0]


def test_every_tool_has_plain_words_and_keeps_its_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) <= set(server.WORDS)
    for name, words in server.WORDS.items():
        assert words == words.lower() and "_" not in words, name
    search = tools["search_job_feed"].input_schema["properties"]
    assert set(search) == {"query", "program", "cycle", "limit", "offset"}
    assert tools["company_history"].input_schema["required"] == ["company"]
    # Python callers keep the plain function: its own exception, no log line.
    assert server.read_candidate_section.__name__ == "read_candidate_section"
    assert not hasattr(server.read_candidate_section, "__wrapped__")


def test_the_log_line_goes_to_the_system_log_off_the_calling_thread(state, monkeypatch):
    seen = []
    monkeypatch.setattr(workflow, "system_line", lambda app, text: seen.append((app, text)))
    server.post_line("read the status · 0.1 s · ok")
    server.LOG.submit(lambda: None).result(timeout=5)  # the single worker keeps order
    assert seen == [("chat", "read the status · 0.1 s · ok")]


# --- the everyday answers -------------------------------------------------------


def test_status_counts_in_plain_words(state):
    assert chat.status()["say"] == ("Nothing is in the queue.\nSent today: 0. The cap is 12 a day.")
    add("a00000000001", "Example Labs — Software Intern", "PREPARING")
    add("a00000000002", "Globex — Data Intern", "NEEDS_USER")
    add("a00000000003", "Initech — Platform Intern", "READY_FOR_REVIEW")
    add("a00000000004", "Hooli — QA Intern", "QUEUED")
    add("a00000000005", "Vandelay — Intern", "DEFERRED")
    add("a00000000006", "Acme — Web Intern", "APPLIED")
    sent("a00000000006")
    sent("a00000000004", status="UNKNOWN_SUBMISSION")
    sent("a00000000005", when="2020-01-01T00:00:00+00:00")
    said = chat.status()["say"]
    assert said == (
        "Working on Example Labs — Software Intern now.\n"
        "2 need you. Ask “what's waiting” to see which.\n"
        "In the queue: 1. Parked: 1.\n"
        "Sent today: 1, and 1 I could not confirm. The cap is 12 a day."
    )
    plain_words(said)
    chat.set_setting("feed_paused", True)
    assert chat.status()["say"].endswith(
        "The feed is paused: its jobs wait, your own links still go."
    )


def test_status_when_the_queue_is_off(state):
    chat.set_setting("enabled", False)
    assert "switched off" in chat.status()["say"]
    assert "switched off" in chat.waiting()["say"]


def test_whats_waiting_names_each_card_and_its_channel(state):
    assert chat.waiting()["say"] == "Nothing waits on you right now."
    add("a00000000002", "Globex — Data Intern", "NEEDS_USER")
    card(
        "a00000000002",
        "action",
        {"headline": "Needs you", "questions": [{"label": "Pronouns"}, {"label": "Start"}]},
    )
    add("a00000000003", "Initech — Platform Intern", "READY_FOR_REVIEW")
    card("a00000000003", "action", {"headline": "Ready to send"})
    add("a00000000007", "Umbrella — Lab Intern", "NEEDS_USER")
    card("a00000000007", "shortlist", {"headline": "Your call"})
    from rove import intake

    with intake.db() as conn:
        lines = [{"number": 1, "answer": None}, {"number": 2, "answer": "yes"}]
        conn.execute(
            "INSERT INTO intake_digests(day,data,created_at,delivery) VALUES(?,?,?,'sent')",
            ("2026-10-03", json.dumps({"lines": lines}), workflow.now()),
        )
    said = chat.waiting()["say"]
    assert said == (
        "3 need you:\n"
        "• Globex — Data Intern: 2 questions only you can answer\n"
        "• Initech — Platform Intern: ready to send\n"
        "• Umbrella — Lab Intern: your call\n"
        "Their cards are in <#action> and <#short>.\n"
        "Today's list in <#short> has 1 job waiting for a yes or no."
    )
    plain_words(said)


def test_sends_today_names_what_went_out_and_the_cap(state):
    add("a00000000006", "Acme — Web Intern", "APPLIED")
    add("a00000000008", "Soylent — Food Intern", "APPLIED")
    sent("a00000000006")
    sent("a00000000008", when="2020-01-01T00:00:00+00:00")
    said = chat.sent_today()["say"]
    assert said == (
        "Sent today: 1. The cap is 12 a day.\n"
        "Acme — Web Intern.\n"
        "I send only after you reply “send it”."
    )
    chat.set_setting("auto_submit", True)
    assert "send it" not in chat.sent_today()["say"]
    assert chat.today() == datetime.now(UTC).strftime("%Y-%m-%d")


def test_pause_and_resume_write_the_switch_and_keep_the_rest(state, monkeypatch):
    logged = []
    monkeypatch.setattr(workflow, "system_line", lambda app, text: logged.append(text))
    said = chat.pause_feed()["say"]
    assert said.startswith("Paused.") and "Links you paste" in said
    path = state / "config/workflow.json"
    stored = json.loads(path.read_text())
    assert stored["feed_paused"] is True and stored["control_channel_id"] == "control"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert chat.resume_feed()["say"].startswith("The feed is running again")
    assert json.loads(path.read_text())["feed_paused"] is False
    assert logged == [
        "feed paused by the owner in agent-control",
        "feed resumed by the owner in agent-control",
    ]
    assert not list((state / "config").glob(".workflow.json.*"))


def test_company_history_tells_what_happened_and_why(state):
    from rove import intake

    add(
        "a00000000005",
        "Vandelay Industries — Import Intern",
        "DEFERRED",
        error="excluded by your rules: machine learning role",
    )
    add(
        "a00000000009",
        "Vandelay Industries — Export Intern",
        "APPLIED",
        updated_at="2026-09-30T12:00:00+00:00",
    )
    add("a00000000010", "Globex — Data Intern", "NEEDS_USER")
    card(
        "a00000000010",
        "shortlist",
        {"headline": "Your call", "reason": "The posting asks for a 2026 graduate."},
    )
    with intake.db() as conn:
        for identity, title, status, reason in (
            ("i1", "Sales Intern", "dropped", "sales role · posted 40 days ago"),
            ("i2", "Data Analyst Intern", "offered", "data role · location not listed"),
        ):
            conn.execute(
                "INSERT INTO intake_decisions(identity,job_id,family,score,tier,reason,status,"
                "payload,decided_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    "j-" + identity,
                    "f-" + identity,
                    20,
                    3,
                    reason,
                    status,
                    json.dumps({"company": "Vandelay Industries, Inc.", "title": title}),
                    workflow.now(),
                    workflow.now(),
                ),
            )
    said = chat.company_history("vandelay industries")["say"]
    # Newest first: the applications, then the feed's decisions about the company's jobs.
    assert said.splitlines() == [
        (
            "• Vandelay Industries — Import Intern: parked: excluded by your rules: machine "
            "learning role"
        ),
        "• Vandelay Industries — Export Intern: applied (last change 2026-09-30)",
        (
            "• Data Analyst Intern: on today's daily list, waiting for your yes (data role · "
            "location not listed)"
        ),
        "• Sales Intern: skipped by your feed rules (sales role · posted 40 days ago)",
    ]
    plain_words(said)
    globex = chat.company_history("Globex")["say"]
    assert globex == "• Globex — Data Intern: your call — The posting asks for a 2026 graduate."
    assert chat.company_history("Nobody Corp")["say"] == (
        "I have no record of Nobody Corp: not applied, not skipped."
    )
    assert chat.company_history(" ")["say"] == "Which company? Give me its name."


# --- the pinned help ------------------------------------------------------------


def test_the_help_lists_six_to_eight_examples_in_plain_words(state):
    text = chat.help_text({"enabled": True})
    examples = [line for line in text.splitlines() if line.startswith("• ")]
    assert 6 <= len(examples) <= 8
    assert text.startswith("**What you can ask Rove**")
    assert "#memory" in text and "<#" not in text
    plain_words(text)
    assert len(text) < 1900
    with_ids = chat.help_text({**SETTINGS, "memory_channel_id": "mem"})
    assert "<#mem>" in with_ids and "<#action>" in with_ids and "<#sys>" in with_ids


class FakeDiscord:
    def __init__(self, pins=None):
        self.calls, self.pins, self.gone = [], list(pins or []), set()

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "PATCH" and path.rsplit("/", 1)[-1] in self.gone:
            response = httpx.Response(404, request=httpx.Request(method, "https://x" + path))
            raise httpx.HTTPStatusError("gone", request=response.request, response=response)
        if method == "GET":
            return {"items": [{"message": message} for message in self.pins]}
        if method == "POST":
            return {"id": f"help-{len(self.calls)}"}
        return {}


def test_the_help_is_posted_and_pinned_once_and_edited_when_it_changes(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(discord_feed, "discord", fake)
    assert chat.ensure_help_message() == {"posted": True, "pinned": True}
    methods = [(m, p) for m, p, _ in fake.calls]
    assert methods == [
        ("GET", "/channels/control/messages/pins"),
        ("POST", "/channels/control/messages"),
        ("PUT", "/channels/control/messages/pins/help-2"),
    ]
    stored = json.loads((state / "config/workflow.json").read_text())
    assert stored["control_help_message_id"] == "help-2"
    # Nothing changed: no Discord call at all, so the gateway restarting never duplicates it.
    fake.calls.clear()
    assert chat.ensure_help_message() == {"posted": False, "unchanged": True}
    assert fake.calls == []
    # New text (here: the memory channel became known) edits the same message.
    chat.set_setting("memory_channel_id", "mem")
    assert chat.ensure_help_message() == {"posted": False, "edited": True}
    assert [(m, p) for m, p, _ in fake.calls] == [("PATCH", "/channels/control/messages/help-2")]
    assert "<#mem>" in fake.calls[0][2]["content"]
    assert fake.calls[0][2]["allowed_mentions"] == {"parse": []}


def test_a_deleted_help_is_posted_again_and_a_pinned_copy_is_adopted(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(discord_feed, "discord", fake)
    chat.set_setting("control_help_message_id", "old")
    chat.set_setting("control_help_hash", "stale")
    fake.gone.add("old")
    assert chat.ensure_help_message()["posted"] is True
    assert (
        json.loads((state / "config/workflow.json").read_text())["control_help_message_id"] != "old"
    )
    # The config lost the id, and the help is already pinned: it is adopted, not duplicated.
    fresh = FakeDiscord(
        pins=[
            {"id": "someone", "author": {"bot": False}, "content": "**What you can ask Rove**"},
            {"id": "pinned", "author": {"bot": True}, "content": "**What you can ask Rove**\nold"},
        ]
    )
    monkeypatch.setattr(discord_feed, "discord", fresh)
    chat.set_setting("control_help_message_id", None)
    assert chat.ensure_help_message() == {"posted": False, "adopted": True}
    assert [m for m, _, _ in fresh.calls] == ["GET", "PATCH"]
    assert (
        json.loads((state / "config/workflow.json").read_text())["control_help_message_id"]
        == "pinned"
    )


def test_without_the_pin_permission_the_help_still_posts(state, monkeypatch):
    fake = FakeDiscord()

    def no_pin(method, path, payload=None):
        if method == "PUT":
            response = httpx.Response(403, request=httpx.Request(method, "https://x" + path))
            raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        return fake(method, path, payload)

    monkeypatch.setattr(discord_feed, "discord", no_pin)
    assert chat.ensure_help_message() == {"posted": True, "pinned": False}
    assert chat.ensure_help_message() == {"posted": False, "unchanged": True}


def test_starting_the_server_refreshes_the_help_and_a_failure_is_one_line(state, monkeypatch):
    logged = []
    monkeypatch.setattr(workflow, "system_line", lambda app, text: logged.append(text))

    def broken():
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(chat, "ensure_help_message", broken)
    server.refresh_help_message()
    assert logged == ["help message not updated · ConnectError"]
