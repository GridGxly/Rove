"""`#memory`: plain-word replies about remembered answers, through a fake Discord."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from rove import memory_channel, workflow
from rove.onboarding import approve, digest, draft, propose, read_approved

RELOCATE = "Are you willing to relocate?*"
MONTHS = "How many months are you available for an internship?"


class FakeDiscord:
    """The memory channel's transport: owner messages in, posted lines out."""

    def __init__(self):
        self.inbox: list[dict] = []
        self.posted: list[dict] = []
        self.down = False
        self.lose_response = False
        self.cursor = 0

    def __call__(self, method, path, payload=None):
        if self.down:
            raise httpx.ConnectError("discord is down")
        if method == "GET" and path.startswith("/channels/mem/messages"):
            after = re.search(r"after=(\d+)", path)
            return [m for m in self.inbox if int(m["id"]) > int(after[1] if after else 0)]
        if method == "POST":
            assert path == "/channels/mem/messages"
            self.posted.append(payload)
            if self.lose_response:
                raise httpx.ReadTimeout("sent, but the answer never came back")
            return {"id": str(900 + len(self.posted))}
        return []

    def say(self, text: str, author: str = "owner", bot: bool = False) -> list[str]:
        """One message in the channel, one worker pass; the lines posted in answer."""
        self.cursor += 1
        user = {"id": author, "bot": True} if bot else {"id": author}
        self.inbox.append({"id": str(self.cursor), "author": user, "content": text})
        return self.tick()

    def tick(self) -> list[str]:
        before = len(self.posted)
        memory_channel.poll("owner")
        return [payload["content"] for payload in self.posted[before:]]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose(
        "identity",
        {"legal_first_name": "Alex", "legal_last_name": "Example", "email": "alex@example.com"},
        digest(draft()),
    )
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
    return tmp_path / "state"


@pytest.fixture
def chat(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "guild_id": "g", "memory_channel_id": "mem"},
    )
    monkeypatch.setattr(memory_channel, "discord", fake)
    monkeypatch.setattr(workflow, "discord", fake)
    memory_channel.poll("owner")  # the first pass only sets the channel's cursor
    with workflow.db() as conn:
        fake.cursor = int(
            conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id='mem'"
            ).fetchone()[0]
        )
    assert fake.posted == []
    return fake


def seed(label: str, options: list, value: str, when: str | None = None):
    """An answer Rove already holds, as if learned and announced on an earlier day."""
    workflow.remember_answer(label, options, value, "m-seed")
    if when:
        with workflow.db() as conn:
            conn.execute("UPDATE answer_memory SET created_at=? WHERE label=?", (when, label))
    memory_channel.announce_learned(quiet=True)


def used(state: Path, label: str, times: int):
    """Prepared applications whose package filled this label from a remembered answer."""
    for n in range(times):
        directory = state / "applications" / f"app-{abs(hash(label)) % 10_000}-{n}"
        directory.mkdir(parents=True)
        field = {"label": label, "value": "x", "source": "your earlier answer"}
        (directory / "package.json").write_text(json.dumps({"filled": [field]}))


def answers_note() -> str:
    notes = list(Path(os.environ["OBSIDIAN_VAULT_PATH"]).rglob("Answers.md"))
    return notes[0].read_text() if notes else ""


def no_ids(lines):
    """Nothing the owner reads here carries an id, a field key or a hash."""
    text = json.dumps(lines)
    assert not re.search(r"[a-f0-9]{12}", text), text


def noon(month: int, day: int, year: int | None = None) -> str:
    """Local noon on a day of this year (or the given one), as the store writes times."""
    year = year or datetime.now(UTC).astimezone().year
    return datetime(year, month, day, 12).astimezone().isoformat()


def test_list_groups_answers_most_used_first_and_pages_with_more(state, chat):
    assert chat.say("list")[0].startswith("Nothing saved yet.")
    seed(RELOCATE, ["Yes", "No"], "Yes")
    seed("How did you hear about us?", [], "A friend")
    for n in range(12):
        seed(f"Synthetic question number {n} about widgets?", [], f"answer {n}")
    used(state, "How did you hear about us? (Required)", 3)
    used(state, RELOCATE, 1)
    (reply,) = chat.say("what do you know")
    lines = reply.split("\n")
    assert lines[0] == "I remember 14 answers, most used first:"
    assert lines[1:5] == [
        "**Other**",
        "1. How did you hear about us? → A friend",
        "2. Synthetic question number 11 about widgets? → answer 11",
        "3. Synthetic question number 10 about widgets? → answer 10",
    ]
    # The answer used once is on the first message, under its own heading.
    assert lines[-3:] == [
        "**Location**",
        "12. Are you willing to relocate? → Yes",
        "Say `more` for the other 2.",
    ]
    assert len([x for x in lines if re.match(r"\d+\. ", x)]) == memory_channel.PAGE_ITEMS
    assert len(reply) < 2000
    (rest,) = chat.say("more")
    assert rest.split("\n") == [
        "**Other**",
        "13. Synthetic question number 1 about widgets? → answer 1",
        "14. Synthetic question number 0 about widgets? → answer 0",
    ]
    assert chat.say("more") == ["That is everything I remember."]
    no_ids([reply, rest])


def test_sensitive_answers_are_hidden_in_lists_and_shown_when_named(state, chat):
    seed("Do you have an active security clearance?", ["Yes", "No"], "No", noon(9, 30))
    seed("Are you a protected veteran?", [], "I am not a protected veteran")
    seed(MONTHS, [], "5 months")
    (reply,) = chat.say("List")
    assert "Do you have an active security clearance? → saved" in reply
    assert "Are you a protected veteran? → saved" in reply
    assert "not a protected veteran" not in reply and "→ No" not in reply
    assert f"{memory_channel.words(MONTHS)} → 5 months" in reply
    assert "The ones marked saved are private; name one to see it." in reply
    assert chat.say("clearance") == [
        "Do you have an active security clearance? → No · you told me on Sep 30"
    ]
    # Words that only brush against a private answer do not spell it out.
    (brushed,) = chat.say("protected parking")
    assert brushed.startswith("Are you a protected veteran? → saved · you told me on ")
    assert chat.say("nice protected parking today") == [memory_channel.HELP_LINE]


def test_a_question_in_plain_words_gets_the_answer_and_where_it_came_from(state, chat):
    seed(RELOCATE, ["Yes", "No"], "Yes", noon(9, 30))
    seed("Do you need relocation assistance?", [], "No", noon(3, 5, 2020))
    seed(MONTHS, [], "5 months", noon(9, 30))
    assert chat.say("how many months?") == [
        f"{memory_channel.words(MONTHS)} → 5 months · you told me on Sep 30"
    ]
    (both,) = chat.say("what do you answer for relocation?")
    assert both.split("\n") == [
        "1. Are you willing to relocate? → Yes · you told me on Sep 30",
        "2. Do you need relocation assistance? → No · you told me on Mar 5, 2020",
    ]
    # The numbers of the last list shown are the ones a follow-up names.
    assert chat.say("forget 2")[0].startswith("Forgot “Do you need relocation assistance?”")
    assert workflow.recall_answer("Do you need relocation assistance?") is None
    assert workflow.recall_answer(RELOCATE, ["Yes", "No"]) == "Yes"
    no_ids(both)


def test_profile_facts_are_answered_from_the_approved_profile(state, chat):
    (sponsorship,) = chat.say("sponsorship")
    assert sponsorship == (
        "Visa sponsorship → No, not now and not in the future · from your approved profile "
        "(it changes through the profile flow, not here)"
    )
    (email,) = chat.say("what's my email")
    assert email.startswith("Email → alex@example.com · from your approved profile")
    (school,) = chat.say("school")
    assert school.startswith("School → Example University · from your approved profile")
    assert chat.say("work authorization")[0].startswith("Work authorization → Yes · from your")


def test_profile_facts_are_read_only_in_the_channel(state, chat):
    before = read_approved()["profile_hash"]
    note = Path(os.environ["OBSIDIAN_VAULT_PATH"]) / "Rove/Profile/Candidate.md"
    text = note.read_text()
    refusal = "comes from your approved profile, so I do not change it here."
    for attempt, name in (
        ("email: someone@example.net", "Email"),
        ("forget school", "School"),
        ("change work authorization to No", "Work authorization"),
        ("remember: email = someone@example.net", "Email"),
        (
            "remember: Are you legally authorized to work in the United States? = No",
            "“Are you legally authorized to work in the United States?”",
        ),
    ):
        (reply,) = chat.say(attempt)
        assert reply == f"{name} {refusal} Changing it goes through the profile flow."
        assert "example.net" not in reply
    assert read_approved()["profile_hash"] == before and note.read_text() == text
    assert workflow.remembered_answers() == []


def test_forget_by_word_or_number_removes_the_answer_and_updates_the_vault(state, chat):
    options = ["- Select -", "Yes", "No"]
    seed(RELOCATE, options, "Yes")
    seed(MONTHS, [], "5 months")
    assert "relocate" in answers_note() and "5 months" in answers_note()
    assert chat.say("forget relocation") == [
        "Forgot “Are you willing to relocate?”. I will ask you the next time a form needs it."
    ]
    assert workflow.recall_answer(RELOCATE, options) is None
    assert workflow.recall_answer(RELOCATE) is None
    assert "relocate" not in answers_note() and "5 months" in answers_note()
    assert chat.say("forget 1") == ["I have not shown you a list yet. Say `list` first."]
    chat.say("list")
    assert chat.say("forget 7") == ["There is no 7 in the last list; it goes up to 1."]
    assert chat.say("forget 1")[0].startswith(f"Forgot “{memory_channel.words(MONTHS)}”")
    assert workflow.remembered_answers() == [] and "5 months" not in answers_note()
    assert chat.say("forget 1") == ["Number 1 is no longer saved. Say `list` for the current list."]
    assert chat.say("forget parking") == [
        "I have nothing saved about that. Say `list` to see what I remember."
    ]
    assert chat.say("forget all") == ["Tell me which one, like `forget 3` or `forget relocation`."]


def test_change_by_number_or_word_keeps_to_the_forms_options(state, chat):
    options = ["- Select -", "Yes", "No"]
    wider = ["Yes", "No", "Not sure"]
    seed(RELOCATE, options, "Yes")
    seed(RELOCATE, wider, "Yes")
    seed(MONTHS, [], "5 months")
    assert chat.say("relocation: maybe") == [
        "That is not one of the options this question has: Yes / No"
    ]
    assert workflow.recall_answer(RELOCATE, options) == "Yes"
    assert chat.say("relocation: no") == [
        "Updated: when a form asks “Are you willing to relocate?”, I now answer No."
    ]
    # The form's own wording of the option is stored, for every option set of the question.
    assert workflow.recall_answer(RELOCATE, options) == "No"
    assert workflow.recall_answer(RELOCATE, wider) == "No"
    assert "| No |" in answers_note() and "| Yes |" not in answers_note()
    chat.say("list")
    listing = memory_channel.load_listing()[0]
    number = listing.index(workflow.question_fingerprint(MONTHS)) + 1
    assert chat.say(f"change {number} to 4 months") == [
        f"Updated: when a form asks “{memory_channel.words(MONTHS)}”, I now answer 4 months."
    ]
    assert workflow.recall_answer(MONTHS) == "4 months" and "4 months" in answers_note()
    # A target that itself contains " to " still splits at the right place.
    assert chat.say("change willing to relocate to Yes")[0].endswith("I now answer Yes.")
    assert chat.say("change 1 to ")[0].startswith("Say it like `change 3 to No`")
    assert chat.say("change relocation to skip")[0].startswith("Give me the new answer")
    assert workflow.recall_answer(RELOCATE, options) == "Yes"
    # Changing here is not news to announce a second time.
    assert chat.tick() == []


def test_remember_stores_a_volunteered_answer_once(state, chat):
    question = "What is your favorite text editor?"
    (saved,) = chat.say(f"remember: {question} = Vim")
    assert saved == (
        "Saved: when a form asks “What is your favorite text editor?”, I answer Vim. "
        "I use it on forms that word the question the same way."
    )
    assert workflow.recall_answer(question + " (required)") == "Vim"
    assert "favorite text editor" in answers_note() and "Vim" in answers_note()
    assert chat.tick() == []
    # Naming a saved question by a word or two changes it instead of adding a twin.
    assert chat.say("remember: text editor = Emacs") == [
        "Updated: when a form asks “What is your favorite text editor?”, I now answer Emacs."
    ]
    assert [row["value"] for row in workflow.remembered_answers()] == ["Emacs"]
    assert chat.say("remember relocation") == ["Say it like `remember: question = answer`."]
    (refused,) = chat.say("remember: social security number = 000-00-0000")
    assert refused.startswith("I do not keep that kind of answer.") and "000" not in refused
    assert len(workflow.remembered_answers()) == 1


def test_anything_else_gets_one_line_of_help(state, chat):
    seed(RELOCATE, [], "Yes")
    assert chat.say("nice work today") == [memory_channel.HELP_LINE]
    assert chat.say("help") == [memory_channel.HELP_LINE]
    assert chat.say("?") == [memory_channel.HELP_LINE]


def test_small_variations_in_wording_still_work(state, chat):
    seed(RELOCATE, [], "Yes")
    seed(MONTHS, [], "5 months")
    assert chat.say("<@1234567890> List all.")[0].startswith("I remember 2 answers")
    # A colon followed by a question asks; it never overwrites the answer.
    assert chat.say("relocation: what do you answer?")[0].startswith(
        "Are you willing to relocate? → Yes"
    )
    assert workflow.recall_answer(RELOCATE) == "Yes"
    assert chat.say("Forget #2.")[0].startswith("Forgot")
    assert chat.say("what do you remember?")[0].startswith("I remember 1 answer, most used")


def test_only_the_owner_is_heard(state, chat):
    seed(RELOCATE, [], "Yes")
    assert chat.say("forget relocation", author="stranger") == []
    assert chat.say("forget relocation", bot=True) == []
    assert chat.say("relocation: No", author="stranger") == []
    assert chat.say("remember: favorite color = green", author="stranger") == []
    assert workflow.recall_answer(RELOCATE) == "Yes"
    assert len(workflow.remembered_answers()) == 1
    assert chat.say("forget relocation")[0].startswith("Forgot")


def test_an_answer_learned_in_a_thread_is_posted_once(state, chat):
    options = ["3 months", "5 months"]
    workflow.remember_answer(MONTHS + "*", options, "5 months", "m1")
    assert chat.tick() == [
        f"Saved: when a form asks “{memory_channel.words(MONTHS)}”, I answer 5 months."
    ]
    assert chat.tick() == []
    # The same answer given again is not news; a different one is.
    workflow.remember_answer(MONTHS, options, "5 months", "m2")
    assert chat.tick() == []
    workflow.remember_answer(MONTHS, options, "3 months", "m3")
    assert chat.tick() == [
        f"Updated: when a form asks “{memory_channel.words(MONTHS)}”, I now answer 3 months."
    ]
    # The same question on a form with other options, answered differently, is news too.
    workflow.remember_answer(MONTHS, ["4 months", "6 months"], "4 months", "m3b")
    assert chat.tick() == [
        f"Updated: when a form asks “{memory_channel.words(MONTHS)}”, I now answer 4 months."
    ]
    workflow.remember_answer("Are you a protected veteran?", [], "I am not a veteran", "m4")
    assert chat.tick() == [
        "Saved your answer to “Are you a protected veteran?”. Name it here to see it."
    ]
    no_ids([payload["content"] for payload in chat.posted])
    assert all(payload["allowed_mentions"] == {"parse": []} for payload in chat.posted)


def test_what_was_learned_before_the_channel_existed_is_not_replayed(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "guild_id": "g", "memory_channel_id": "mem"},
    )
    monkeypatch.setattr(memory_channel, "discord", fake)
    workflow.remember_answer(RELOCATE, [], "Yes", "m1")
    fake.inbox.append({"id": "5", "author": {"id": "owner"}, "content": "forget relocation"})
    assert fake.tick() == [] and fake.tick() == []
    # Messages older than the first pass are history, not commands.
    assert workflow.recall_answer(RELOCATE) == "Yes"


def test_a_discord_outage_neither_loses_nor_repeats_a_line(state, chat):
    seed(RELOCATE, [], "Yes")
    chat.down = True
    workflow.remember_answer(MONTHS, [], "5 months", "m1")
    assert chat.tick() == []
    chat.down = False
    assert chat.tick() == [
        f"Saved: when a form asks “{memory_channel.words(MONTHS)}”, I answer 5 months."
    ]
    assert chat.tick() == []
    # Discord took the message but the answer was lost: the retry carries the same nonce,
    # which Discord is told to enforce, so the channel shows the line once.
    chat.lose_response = True
    assert len(chat.say("forget relocation")) == 1
    chat.lose_response = False
    assert len(chat.tick()) == 1
    first, second = chat.posted[-2:]
    assert first == second and first["enforce_nonce"] is True
    assert first["nonce"].startswith("memory:") and first["content"].startswith("Forgot")
    assert chat.tick() == []
    assert "memory" in (state / "logs/delivery-failures.log").read_text()
    # A delivered line leaves no copy of its words in the outbox.
    with workflow.db() as conn:
        rows = conn.execute("SELECT content,delivery FROM memory_outbox").fetchall()
    assert rows and all(r["delivery"] == "sent" and r["content"] == "" for r in rows)


def test_a_message_seen_twice_is_acted_on_once(state, chat):
    seed(RELOCATE, [], "Yes")
    with workflow.db() as conn:
        before = conn.execute(
            "SELECT message_id FROM workflow_checkpoints WHERE channel_id='mem'"
        ).fetchone()[0]
    assert len(chat.say("forget relocation")) == 1
    seed(RELOCATE, [], "Yes")
    with workflow.db() as conn:
        # A crash before the cursor moved: the same message comes back on the next pass.
        conn.execute(
            "UPDATE workflow_checkpoints SET message_id=? WHERE channel_id='mem'", (before,)
        )
    assert chat.tick() == []
    assert workflow.recall_answer(RELOCATE) == "Yes"


def test_a_failing_message_gets_a_plain_line_and_the_detail_goes_to_the_system_log(
    state, chat, monkeypatch
):
    def broken(text, message_id):
        raise RuntimeError("internal detail 0123456789abcdef")

    logged = []
    monkeypatch.setattr(memory_channel, "respond", broken)
    monkeypatch.setattr(workflow, "system_line", lambda *a: logged.append(a))
    assert chat.say("list") == [
        "That did not work and nothing was changed. The detail is in the system log."
    ]
    assert logged == [("memory", "memory command failed · RuntimeError")]


def test_the_worker_polls_memory_without_treating_it_as_a_command_channel(state, monkeypatch):
    from rove import worker

    fake = FakeDiscord()
    settings = {
        "enabled": True,
        "guild_id": "g",
        "control_channel_id": "control",
        "memory_channel_id": "mem",
    }
    monkeypatch.setattr(workflow, "config", lambda: settings)
    monkeypatch.setattr(worker, "private_env", lambda: {"DISCORD_OWNER_USER_ID": "owner"})
    monkeypatch.setattr(worker, "discord", fake)
    monkeypatch.setattr(memory_channel, "discord", fake)
    monkeypatch.setattr(workflow, "discord", fake)
    worker.poll_commands()
    with workflow.db() as conn:
        cursor = int(
            conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id='mem'"
            ).fetchone()[0]
        )
    fake.inbox.append({"id": str(cursor + 1), "author": {"id": "owner"}, "content": "list"})
    worker.poll_commands()
    assert [payload["content"][:18] for payload in fake.posted] == ["Nothing saved yet."]


def test_memory_channel_is_looked_up_by_name_and_kept_in_config(state, monkeypatch):
    path = state / "config/workflow.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "guild_id": "g"}))
    calls = []
    channels = [{"id": 7, "name": "system-log"}, {"id": 42, "name": "memory"}]
    monkeypatch.setattr(
        workflow,
        "discord",
        lambda method, path, payload=None: calls.append((method, path)) or channels,
    )
    assert workflow.ensure_memory_channel() == "42"
    assert json.loads(path.read_text())["memory_channel_id"] == "42"
    assert workflow.ensure_memory_channel() == "42"
    assert calls == [("GET", "/guilds/g/channels")]


def test_no_memory_channel_means_no_pass_and_no_error(state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True})
    monkeypatch.setattr(memory_channel, "discord", lambda *a, **k: pytest.fail("no channel"))
    memory_channel.poll("owner")
    monkeypatch.setattr(workflow, "config", lambda: {"memory_channel_id": "mem"})
    memory_channel.poll("owner")
