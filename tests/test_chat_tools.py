"""The model's tools in agent-control: it reads him, code checks facts and acts.

However he phrases a request, the model passes its reading as arguments. These tests
check what code then does with them: a link gets his standing only when it is in a
message he wrote, a name finds one of his applications or asks which, an answer is
saved only in his own words and by the rules of its thread, and the system log says
what really happened.
"""

import asyncio
import itertools
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from rove import chat, discord_feed, inbound, server, workflow
from rove.onboarding import approve, digest, draft, propose

OWNER = "1001"
SETTINGS = {"enabled": True, "control_channel_id": "control", "action_channel_id": "action"}
IDS = itertools.count(5000)


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("ROVE_STATE_DIR", str(root))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/workflow.json").write_text(json.dumps(SETTINGS))
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {"id": "m"})
    monkeypatch.setattr(workflow, "system_line", lambda *a, **k: None)
    return root


@pytest.fixture
def channel(state, monkeypatch):
    """agent-control as Discord returns it to the tools: newest message first."""
    messages = []
    monkeypatch.setattr(
        discord_feed, "discord", lambda method, path, payload=None: list(reversed(messages))
    )
    monkeypatch.setattr(discord_feed, "private_env", lambda: {"DISCORD_OWNER_USER_ID": OWNER})
    return messages


def message(text, author=OWNER, minutes_ago=0, **extra):
    stamp = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "id": str(next(IDS)),
        "author": {"id": author, **extra.pop("author_extra", {})},
        "content": text,
        "timestamp": stamp.isoformat(),
        **extra,
    }


def link(n):
    return f"https://jobs.lever.co/acme/{n}"


def queued():
    with workflow.db() as conn:
        return [tuple(r) for r in conn.execute("SELECT url,source FROM application_queue")]


def add(application_id, title, status, hold=None):
    stamp = workflow.now()
    with workflow.db() as conn:
        conn.execute(
            "INSERT INTO application_queue(id,url,source_url,source,title,status,profile_hash,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                application_id,
                f"https://jobs.example.org/{application_id}",
                f"https://jobs.example.org/{application_id}",
                "feed",
                title,
                status,
                "p" * 64,
                stamp,
                stamp,
            ),
        )
    if hold is not None:
        workflow.record(application_id, "needs_action", hold)


# --- apply_to_link: provenance, not wording --------------------------------------


def test_a_link_he_pasted_is_queued_as_his_however_he_asked(channel):
    channel.append(message(f"yo apply to this rq {link(1)}"))
    result = chat.apply_to_link(link(1))
    assert result == {"say": "Queued. It goes next.", "outcome": "queued his link"}
    assert queued() == [(link(1), "owner_link")]
    # The model's copy of the link may lose its query string or gain a slash.
    channel.append(message(f"and this one too {link(2)}?gh_src=abc please"))
    assert chat.apply_to_link(link(2) + "/")["outcome"] == "queued his link"
    assert (link(2), "owner_link") in queued()  # the tracking parameter is dropped, as ever


def test_a_link_he_did_not_paste_gains_nothing(channel):
    channel.append(message(f"apply to this {link(1)}"))
    for url, why in (
        (link(9), "the model's own or a page's link"),
        ("https://evil.example/jobs/1", "a link from a tool result"),
    ):
        result = chat.apply_to_link(url)
        assert result["outcome"] == "link not in his messages, nothing queued", why
        assert "paste it" in result["say"].lower()
    assert queued() == []


@pytest.mark.parametrize(
    "written",
    [
        message(f"apply to {link(1)}", author="666"),
        message(f"apply to {link(1)}", author_extra={"bot": True}),
        message(f"apply to {link(1)}", webhook_id="77"),
        message(f"apply to {link(1)}", minutes_ago=45),
    ],
    ids=["another user", "a bot", "a webhook", "too old"],
)
def test_only_his_own_recent_messages_count(channel, written):
    channel.append(written)
    assert chat.apply_to_link(link(1))["outcome"] == "link not in his messages, nothing queued"
    assert queued() == []


def test_first_puts_his_link_ahead_and_works_on_one_already_queued(channel):
    channel.append(message(f"apply to both {link(1)} {link(2)} do the second one first"))
    assert chat.apply_to_link(link(1))["say"] == "Queued. It goes next."
    result = chat.apply_to_link(link(2), first=True)
    assert result == {"say": "Queued. It goes next.", "outcome": "queued his link, put first"}
    with workflow.db() as conn:
        order = inbound.line_of_mine(conn)
    ids = {url: app for app, url in _ids()}
    assert order == [ids[link(2)], ids[link(1)]]
    # Later: "actually do the first one first" moves the queued one up.
    assert chat.apply_to_link(link(1), first=True)["outcome"] == "link already tracked, put first"
    with workflow.db() as conn:
        assert inbound.line_of_mine(conn)[0] == ids[link(1)]


def test_apply_now_with_no_link_means_the_link_he_pasted_last_and_puts_it_first(channel):
    # What he typed live: "apply <link>", then "apply now".
    channel.append(message(f"apply {link(1)}", minutes_ago=5))
    channel.append(message(f"apply {link(2)}", minutes_ago=2))
    assert chat.apply_to_link(link(1))["say"] == "Queued. It goes next."
    assert chat.apply_to_link(link(2))["say"].startswith("Queued. 1 of your links is ahead")
    channel.append(message("apply now"))
    result = chat.apply_to_link("", first=False)
    assert result == {
        "say": "Already queued. It goes next.",
        "outcome": "link already tracked, put first",
    }
    ids = {url: app for app, url in _ids()}
    with workflow.db() as conn:
        assert inbound.line_of_mine(conn) == [ids[link(2)], ids[link(1)]]
    # While another application holds the browser, the line says which one.
    add("a00000000009", "Northwind — Intern", "PREPARING")
    assert chat.apply_to_link()["say"] == (
        "Already queued. Northwind — Intern is running now; it goes right after."
    )


def test_apply_now_with_no_recent_link_asks_for_one(channel):
    channel.append(message(f"apply {link(1)}", minutes_ago=45))
    channel.append(message("apply now"))
    assert chat.apply_to_link("") == {
        "say": "Paste the job link and I'll start on it.",
        "outcome": "no link named and none pasted lately, nothing queued",
    }
    # A link in somebody else's message is never the one he means.
    channel.append(message(f"apply {link(3)}", author="666"))
    assert chat.apply_to_link("")["outcome"].startswith("no link named")
    assert queued() == []


def _ids():
    with workflow.db() as conn:
        return [tuple(r) for r in conn.execute("SELECT id,url FROM application_queue")]


def test_a_bad_or_switched_off_link_is_one_plain_line(channel):
    assert chat.apply_to_link("not a link")["outcome"] == "not a public link, nothing queued"
    chat.set_setting("enabled", False)
    assert chat.apply_to_link(link(1))["outcome"] == "queue is switched off, nothing queued"


# --- retry and park by name --------------------------------------------------------


def test_retry_by_name_does_what_go_does_in_its_thread(channel):
    add("a00000000001", "Tesla — Embedded Software Intern", "NEEDS_USER", {"headline": "Needs you"})
    add("a00000000002", "Tesla — Battery Intern", "APPLIED")
    result = chat.retry_application("tesla embedded")
    assert result["outcome"] == "retried one application"
    assert result["say"].startswith("Going again on Tesla — Embedded Software Intern.")
    assert workflow.get("a00000000001")["status"] == "QUEUED"
    # Only one Tesla is waiting, so "tesla" alone is enough.
    workflow.set_state("a00000000001", "DEFERRED")
    assert chat.retry_application("Tesla")["outcome"] == "retried one application"


def test_retry_on_one_still_in_the_queue_moves_it_to_the_front(channel):
    add("a00000000001", "Globex — Data Intern", "QUEUED")
    add("a00000000002", "Initech — Web Intern", "QUEUED")
    result = chat.retry_application("globex")
    assert result["outcome"] == "moved one queued application to the front"
    assert result["say"].startswith("Moved Globex — Data Intern to the front.")
    assert workflow.get("a00000000001")["status"] == "QUEUED"
    with workflow.db() as conn:
        assert inbound.line_of_mine(conn)[0] == "a00000000001"


def test_retry_asks_which_when_several_match_and_says_so_when_none_do(channel):
    add("a00000000001", "Sierra — Agent Intern", "NEEDS_USER", {"headline": "Needs you"})
    add("a00000000002", "Sierra — Platform Intern", "DEFERRED")
    several = chat.retry_application("sierra")
    assert several["outcome"] == "2 applications match, asked which"
    assert several["say"].startswith("Which one: ") and "Platform Intern" in several["say"]
    none = chat.retry_application("initech")
    assert none == {
        "say": "I don't have an application matching “initech”.",
        "outcome": "no application matches",
    }
    add("a00000000003", "Hooli — QA Intern", "APPLIED")
    sent = chat.retry_application("hooli")
    assert sent["outcome"] == "nothing to retry, it is applied"
    assert workflow.get("a00000000003")["status"] == "APPLIED"


def test_park_by_name(channel):
    add("a00000000001", "Walleye Capital — Developer Intern", "NEEDS_USER", {"headline": "x"})
    result = chat.park_application("walleye")
    assert result["outcome"] == "parked one application"
    assert workflow.get("a00000000001")["status"] == "DEFERRED"
    again = chat.park_application("walleye")
    assert again["outcome"] == "nothing to park, it is parked"


# --- answering a waiting question from the chat -------------------------------------


@pytest.fixture
def held(channel, state):
    """xAI waits on two questions: hours a week, and work authorization (sensitive)."""
    hold = {
        "headline": "Needs you",
        "questions": [
            {"key": "k_hours", "label": "How many hours per week can you work?", "state": "open"},
            {
                "key": "k_auth",
                "label": "Are you legally authorized to work in the United States?",
                "options": ["Yes", "No"],
                "state": "open",
            },
        ],
    }
    add("a00000000009", "xAI — Software Engineer Intern", "NEEDS_USER", hold)
    folder = state / "applications/a00000000009"
    folder.mkdir(parents=True)
    fields = [
        {
            "key": "k_hours",
            "label": hold["questions"][0]["label"],
            "kind": "text",
            "required": True,
        },
        {
            "key": "k_auth",
            "label": hold["questions"][1]["label"],
            "kind": "select",
            "required": True,
            "options": ["Yes", "No"],
        },
    ]
    (folder / "observation.json").write_text(json.dumps({"fields": fields}))
    return channel


def saved():
    with workflow.db() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT field_key,value FROM application_answers")}


def test_an_answer_in_his_words_is_saved_like_a_thread_reply(held):
    held.append(message("for the xai one put 40 hrs"))
    result = chat.answer_application("xai", "40 hrs", "hours per week")
    assert result["outcome"] == "saved his answer to question 1"
    assert result["say"].startswith("Saved for xAI — Software Engineer Intern:")
    assert "1 more question open" in result["say"]
    assert saved() == {"k_hours": "40 hrs"}


def test_two_open_questions_and_no_question_named_asks_which(held):
    held.append(message("for xai 40 hrs"))
    result = chat.answer_application("xai", "40 hrs")
    assert result["outcome"] == "asked which question"
    assert "1) How many hours" in result["say"] and "2) Are you legally" in result["say"]
    assert saved() == {}


def test_an_answer_not_in_his_words_is_not_saved(held):
    held.append(message("for the xai one put 40 hrs"))
    result = chat.answer_application("xai", "40 hours a week, flexible", "hours")
    assert result["outcome"] == "answer not in his messages, nothing saved"
    assert saved() == {}


def test_a_personal_question_needs_naming_and_an_option(held):
    workflow.record(
        "a00000000009",
        "needs_action",
        {
            "headline": "Needs you",
            "questions": [
                {
                    "key": "k_auth",
                    "label": "Are you legally authorized to work in the United States?",
                    "options": ["Yes", "No"],
                    "state": "open",
                }
            ],
        },
    )
    held.append(message("xai: yes im authorized"))
    unclear = chat.answer_application("xai", "yes")
    assert unclear["outcome"] == "asked to confirm a personal question"
    assert unclear["say"].startswith("To be sure:")
    assert saved() == {}
    bad = chat.answer_application("xai", "yes im authorized", "authorized to work")
    assert bad["outcome"] == "answer is not one of the options, nothing saved"
    good = chat.answer_application("xai", "yes", "authorized to work")
    assert good["outcome"] == "saved his answer to question 1"
    assert saved() == {"k_auth": "Yes"}


def test_answer_with_no_waiting_application(held):
    held.append(message("for tesla 6 months"))
    assert chat.answer_application("tesla", "6 months")["outcome"] == "no application matches"
    add("a00000000004", "Tesla — Intern", "QUEUED")
    assert chat.answer_application("tesla", "6 months")["outcome"] == (
        "no open question there, nothing saved"
    )


# --- what the system log says --------------------------------------------------------


def test_the_log_line_says_what_happened_never_a_bare_ok(channel, monkeypatch):
    lines = []
    monkeypatch.setattr(server, "post_line", lines.append)

    def call(name, arguments):
        result = asyncio.run(server.mcp.call_tool(name, arguments))
        assert not result.is_error
        return result.content[0].text

    channel.append(message(f"apply to this {link(1)}"))
    assert call("apply_to_link", {"url": link(1)}) == "Queued. It goes next."
    assert call("apply_to_link", {"url": link(7)}).startswith("I can only queue")
    call("retry_application", {"name": "initech"})
    call("pause_feed", {})
    call("pause_feed", {})
    call("rove_status", {})
    outcomes = [re.sub(r" · \d+\.\d s$", "", line) for line in lines]
    assert outcomes == [
        "apply to a link · queued his link",
        "apply to a link · link not in his messages, nothing queued",
        "retry an application · no application matches",
        "paused the feed · feed paused",
        "paused the feed · feed was already paused",
        "read the status · 0 need him, 1 queued, 0 in progress",
    ]
    assert not any(line.endswith(" ok") for line in lines)
    assert link(1) not in " ".join(lines) and "initech" not in " ".join(lines)


def test_answer_paste_is_retired():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "answer_paste" not in names and not hasattr(chat, "answer_paste")
    assert {"apply_to_link", "retry_application", "park_application", "answer_application"} <= names
