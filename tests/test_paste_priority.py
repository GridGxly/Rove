"""The owner's pasted links in agent-control: where each one stands, and `first`.

His pasted links run before any feed job, oldest first. A priority word next to a link,
or `first` right after pasting it, puts it ahead of his other pasted links that have not
started. The line Rove posts always says where the link stands.
"""

import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest

from rove import chat, inbound, worker, workflow
from rove.onboarding import approve, digest, draft, propose

SETTINGS = {"enabled": True, "control_channel_id": "control", "action_channel_id": "action"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A private state root with an approved synthetic profile and the control channel."""
    root = tmp_path / "state"
    monkeypatch.setenv("ROVE_STATE_DIR", str(root))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose(
        "identity",
        {"legal_first_name": "Alex", "legal_last_name": "Example", "city": "Columbus"},
        digest(draft()),
    )
    approve(digest(draft()))
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/workflow.json").write_text(json.dumps(SETTINGS))
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {"id": "m"})
    monkeypatch.setattr(workflow, "system_line", lambda *a, **k: None)
    return root


OWNER = "1001"
IDS = itertools.count(9000)


def said(text: str, reply_to: str | None = None, **extra) -> dict:
    """One owner message in agent-control, as Discord returns it (a fresh id each time)."""
    message = {
        "id": str(next(IDS)),
        "author": {"id": OWNER},
        "content": text,
        "timestamp": datetime.now(UTC).isoformat(),
        **extra,
    }
    if reply_to:
        message["message_reference"] = {"message_id": reply_to}
    return message


def paste(text: str, reply_to: str | None = None) -> str | None:
    """What the worker's code says to one message (it holds the line for the agent)."""
    return inbound.owner_message(said(text, reply_to), "control", SETTINGS, {})


def link(n: int) -> str:
    return f"https://jobs.lever.co/acme/{n}"


def app_of(n: int) -> str:
    with workflow.db() as conn:
        return conn.execute("SELECT id FROM application_queue WHERE url=?", (link(n),)).fetchone()[
            0
        ]


def line() -> list[str]:
    with workflow.db() as conn:
        return inbound.line_of_mine(conn)


def test_each_paste_says_where_it_stands(state):
    assert paste(link(1)) == "Queued. It goes next."
    assert paste(link(2)) == "Queued. 1 of your links is ahead of it; say `first` to move it up."
    assert paste(f"apply to this {link(3)}") == (
        "Queued. 2 of your links are ahead of it; say `first` to move it up."
    )
    assert paste(link(1)) == "Already queued. It goes next."
    assert line() == [app_of(1), app_of(2), app_of(3)]
    workflow.set_state(app_of(1), "APPLIED")
    assert paste(link(1)) == "Already tracked: applied."


@pytest.mark.parametrize(
    "words",
    ["{} now", "do this one first {}", "{} next please", "priority {}", "asap {}", "first {}"],
)
def test_a_priority_word_puts_the_link_ahead_of_his_other_pasted_links(state, words):
    paste(link(1))
    paste(link(2))
    assert paste(words.format(link(3))) == "Queued. It goes next."
    assert line() == [app_of(3), app_of(1), app_of(2)]
    # The worker takes it next, before the links pasted earlier.
    assert worker.next_queued(5) == app_of(3)


def test_the_latest_first_wins_and_feed_jobs_stay_behind(state):
    workflow.enqueue(link(9), source="keryx")
    paste(f"{link(1)} first")
    paste(link(2))
    assert paste(f"{link(3)} asap") == "Queued. It goes next."
    assert line() == [app_of(3), app_of(1), app_of(2)]
    assert app_of(9) not in line()
    assert worker.next_queued(5) == app_of(3)


def test_several_links_with_a_priority_word_go_first_in_the_order_pasted(state):
    paste(link(1))
    assert paste(f"{link(2)} {link(3)} first") == "Queued 2 of your links. The first goes next."
    assert line() == [app_of(2), app_of(3), app_of(1)]
    assert paste(f"{link(4)} {link(5)}") == (
        "Queued 2 of your links. 3 of your links are ahead of them; say `first` to move them up."
    )


def test_first_right_after_pasting_moves_the_latest_paste_up(state):
    paste(link(1))
    paste(link(2))
    paste(link(3))
    assert paste("first") == "Moved it up. It goes next."
    assert line() == [app_of(3), app_of(1), app_of(2)]
    paste(link(4))
    assert paste("Move it up.") == "Moved it up. It goes next."
    assert line() == [app_of(4), app_of(3), app_of(1), app_of(2)]
    assert worker.next_queued(5) == app_of(4)


def test_first_alone_counts_only_for_a_recent_paste_but_a_reply_always_counts(state):
    paste(link(1))
    paste(link(2))
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with workflow.db() as conn:
        conn.execute("UPDATE owner_link_order SET pasted_at=?", (old,))
    assert paste("first").startswith("Nothing you pasted in the last half hour is waiting.")
    assert line() == [app_of(1), app_of(2)]
    # A reply on Rove's line about the paste means that paste, however long ago.
    with workflow.db() as conn:
        conn.execute(
            "UPDATE owner_link_order SET pasted_at=? WHERE application_id=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), app_of(2)),
        )
    assert paste("first", reply_to="rove-line") == "Moved it up. It goes next."
    assert line() == [app_of(2), app_of(1)]


def test_first_when_nothing_is_waiting_says_so(state):
    assert paste("first").startswith("Nothing you pasted is waiting.")
    paste(link(1))
    workflow.set_state(app_of(1), "PREPARING")
    assert paste("first") == "That link already started, so there is nothing to move."
    # Words that are not a request to move stay with the agent.
    assert paste("first things first, how are you") is None
    assert paste("what came first") is None


def test_first_is_read_only_in_agent_control(state):
    paste(link(1))
    paste(link(2))
    for channel in ("action", "shortlist", "t1"):
        message = {"id": "2", "author": {"id": "1001"}, "content": "first"}
        assert inbound.owner_message(message, channel, SETTINGS, {"t1": "x"}) is None
    assert line() == [app_of(1), app_of(2)]


def test_the_help_tells_him_about_first(state):
    assert "add `first` to jump the line" in chat.help_text(SETTINGS)


# --- one answer in agent-control -----------------------------------------------------


@pytest.fixture
def control(state, monkeypatch):
    """Discord as the agent's tool and the worker's flush see it: the channel's newest
    messages, and what gets posted there."""
    from rove import discord_feed

    channel = {"messages": [], "posted": []}

    def fake(method, path, payload=None):
        if method == "GET":
            return list(reversed(channel["messages"]))  # newest first, as Discord returns them
        channel["posted"].append(payload["content"])
        return {"id": "p"}

    monkeypatch.setattr(discord_feed, "discord", fake)
    monkeypatch.setattr(workflow, "discord", fake)
    monkeypatch.setattr(discord_feed, "private_env", lambda: {"DISCORD_OWNER_USER_ID": OWNER})
    return channel


def test_the_agent_applies_his_paste_and_says_the_line_and_the_worker_stays_quiet(control):
    message = said(f"{link(1)} please")
    control["messages"].append(message)
    assert chat.answer_paste() == {"say": "Queued. It goes next."}
    assert line() == [app_of(1)]
    # The worker reaches the same message later: nothing is applied twice or said twice.
    assert inbound.owner_message(message, "control", SETTINGS, {}) == ""
    assert line() == [app_of(1)]
    # Asked again, the agent has nothing new to say for it.
    assert chat.answer_paste()["found"] is False


def test_whoever_applies_a_paste_first_is_the_only_one_to_answer_it(control):
    first, again = said(link(1)), said(f"{link(2)} first")
    control["messages"] += [first, again]
    # The worker got there first: it says the line itself (the caller posts it)...
    assert inbound.owner_message(first, "control", SETTINGS, {}) == "Queued. It goes next."
    # ...and the agent finds only the message the worker has not applied.
    assert chat.answer_paste() == {"say": "Queued. It goes next."}
    assert line() == [app_of(2), app_of(1)]
    assert inbound.owner_message(again, "control", SETTINGS, {}) == ""
    assert inbound.control_line(first) == "" and inbound.control_line(again) == ""


def test_the_agent_applies_only_his_own_recent_messages(control):
    stranger = said(link(1), author={"id": "666"})
    bot = said(link(2), author={"id": OWNER, "bot": True})
    old = said(link(3), timestamp=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    chatter = said("how is it going")
    control["messages"] += [stranger, bot, old, chatter]
    assert chat.answer_paste()["found"] is False
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0] == 0
    control["messages"].append(said("first"))
    assert chat.answer_paste() == {
        "say": "Nothing you pasted is waiting. Paste a link with `first` to put it at the front."
    }


def test_a_bad_link_is_one_plain_line_through_the_agent(control):
    control["messages"].append(said("http://127.0.0.1/admin"))
    assert "public HTTPS" in chat.answer_paste()["say"]
