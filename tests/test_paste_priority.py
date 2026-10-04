"""The owner's pasted links in agent-control: where each one stands, and `first`.

His pasted links run before any feed job, oldest first. A message that is nothing but
links is queued by code at once; a link he asks for in his own words goes through the
model's `apply_to_link` (see test_chat_tools.py), which can also put it first. `first`
on its own right after pasting moves his latest paste to the front. The line Rove posts
always says where the link stands.
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
    """What the code's fast lane says to one message; None leaves it to the model."""
    return inbound.owner_message(said(text, reply_to), "control", SETTINGS, {})


def link(n: int) -> str:
    return f"https://jobs.lever.co/acme/{n}"


def app_of(n: int) -> str:
    with workflow.db() as conn:
        row = conn.execute("SELECT id FROM application_queue WHERE url=?", (link(n),)).fetchone()
    return row[0]


def line() -> list[str]:
    with workflow.db() as conn:
        return inbound.line_of_mine(conn)


def test_each_paste_says_where_it_stands(state):
    assert paste(link(1)) == "Queued. It goes next."
    assert paste(link(2)) == "Queued. 1 of your links is ahead of it; say `first` to move it up."
    assert paste(f"<{link(3)}>") == (
        "Queued. 2 of your links are ahead of it; say `first` to move it up."
    )
    assert paste(link(1)) == "Already queued. It goes next."
    assert line() == [app_of(1), app_of(2), app_of(3)]
    workflow.set_state(app_of(1), "APPLIED")
    assert paste(link(1)) == "Already tracked: applied."


def test_a_paste_behind_a_running_application_names_what_it_waits_for(state):
    # What he saw live: "It goes next" while another application held the browser.
    running = workflow.enqueue(
        "https://jobs.example.com/running", source="owner_link", title="Northwind — Intern"
    )["application_id"]
    workflow.set_state(running, "PREPARING")
    assert paste(link(1)) == "Queued. Northwind — Intern is running now; it goes right after."
    assert paste(link(1)) == (
        "Already queued. Northwind — Intern is running now; it goes right after."
    )
    assert paste(link(2)) == "Queued. 1 of your links is ahead of it; say `first` to move it up."
    workflow.set_state(running, "NEEDS_USER")
    assert paste(link(1)) == "Already queued. It goes next."


def test_only_a_message_of_nothing_but_links_is_the_fast_lane(state):
    assert inbound.pasted_links(f"{link(1)}\n{link(2)}  {link(1)}.") == [link(1), link(2)]
    assert inbound.pasted_links(f"<{link(1)}>, <{link(2)}>") == [link(1), link(2)]
    # Any word at all, however it reads, is the model's to understand: nothing is queued
    # here and nothing is answered, so it can never be turned down by code.
    for text in (
        f"hi apply to this {link(1)}",
        f"{link(1)} first",
        f"dont apply to {link(1)}",
        f"is this one even worth it {link(1)}",
        f"apply to both {link(1)} {link(2)} do the second one first",
    ):
        assert inbound.pasted_links(text) == []
        assert paste(text) is None
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0] == 0
    many = " ".join(link(n) for n in range(6))
    assert inbound.pasted_links(many) == []


def test_no_word_list_decides_what_he_meant():
    for name in ("PASTE_WORDS", "APPLY_WORDS", "ASKING_WORDS", "REFUSAL_WORDS", "REQUEST_WORDS"):
        assert not hasattr(inbound, name), name
    assert not hasattr(inbound, "wants_first")
    assert set(inbound.FIRST_REPLIES) == {"first", "move it up"}


def test_several_links_go_in_the_order_pasted(state):
    paste(link(1))
    assert paste(f"{link(2)} {link(3)}") == (
        "Queued 2 of your links. 1 of your links is ahead of them; say `first` to move them up."
    )
    assert line() == [app_of(1), app_of(2), app_of(3)]
    assert worker.next_queued(5) == app_of(1)


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
    # Any other wording goes to the model.
    assert paste("first things first, how are you") is None
    assert paste("put that one first") is None


def test_first_is_read_only_in_agent_control(state):
    paste(link(1))
    paste(link(2))
    for channel in ("action", "shortlist", "t1"):
        message = {"id": "2", "author": {"id": "1001"}, "content": "first"}
        assert inbound.owner_message(message, channel, SETTINGS, {"t1": "x"}) is None
    assert line() == [app_of(1), app_of(2)]


def test_the_help_tells_him_about_first(state):
    assert "add `first` to jump the line" in chat.help_text(SETTINGS)


def test_whoever_applies_a_paste_first_is_the_only_one_to_answer_it(state):
    message = said(f"{link(1)} {link(2)}")
    # The worker got there first: it says the line (the caller posts it)...
    assert inbound.owner_message(message, "control", SETTINGS, {}).startswith("Queued 2")
    # ...and the gateway's fast lane, reaching the same message, applies and says nothing.
    assert inbound.control_line(message) == ""
    assert line() == [app_of(1), app_of(2)]
