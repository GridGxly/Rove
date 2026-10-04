"""`#memory`: plain-word replies about remembered answers, through a fake Discord."""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from rove import memory_channel, questions, workflow
from rove.onboarding import approve, digest, draft, propose, read_approved
from rove.worker import apply_command

RELOCATE = "Are you willing to relocate?*"
MONTHS = "How many months are you available for an internship?"


class FakeDiscord:
    """The memory channel's transport: owner messages in, posted lines out."""

    def __init__(self):
        self.inbox: list[dict] = []
        self.posted: list[dict] = []
        self.edits: list[tuple[str, str]] = []  # (message id, new content)
        self.down = False
        self.lose_response = False
        self.refuse_edits = False
        self.cursor = 0

    def __call__(self, method, path, payload=None):
        if self.down:
            raise httpx.ConnectError("discord is down")
        if method == "GET" and path.startswith("/channels/mem/messages"):
            after = re.search(r"after=(\d+)", path)
            return [m for m in self.inbox if int(m["id"]) > int(after[1] if after else 0)]
        if method == "PATCH":
            assert path.startswith("/channels/mem/messages/")
            if self.refuse_edits:
                request = httpx.Request("PATCH", "https://discord.com/api/v10" + path)
                response = httpx.Response(404, request=request, text="Unknown Message")
                raise httpx.HTTPStatusError("gone", request=request, response=response)
            self.edits.append((path.rsplit("/", 1)[1], payload["content"]))
            return {"id": path.rsplit("/", 1)[1]}
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
    identity = {
        "legal_first_name": "Alex",
        "legal_last_name": "Example",
        "email": "alex@example.com",
        "city": "Springfield",
        "state_region": "IL",
        "linkedin": "https://www.linkedin.com/in/alex-example",
        "github": "https://github.com/alex-example",
        "portfolio": "https://alex.example/",
    }
    school = {
        "school": "Example University",
        "degree": "B.S.",
        "major": "Computer Science",
        "graduation_month": "2027-12",
    }
    propose("identity", identity, digest(draft()))
    propose("education", {"schools": [school]}, digest(draft()))
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
    memory_channel.mark(memory_channel.NUDGE_MARK)  # the one-time nudge has its own test
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
    applications = state / "applications"
    applications.mkdir(exist_ok=True)
    for _ in range(times):
        directory = applications / f"app-synthetic-{len(list(applications.iterdir()))}"
        directory.mkdir()
        field = {"label": label, "value": "x", "source": "your earlier answer"}
        (directory / "package.json").write_text(json.dumps({"filled": [field]}))


def answers_note() -> str:
    notes = list(Path(os.environ["OBSIDIAN_VAULT_PATH"]).rglob("Answers.md"))
    return notes[0].read_text() if notes else ""


def no_ids(lines):
    """Nothing the owner reads here carries an id, a field key or a hash."""
    text = json.dumps(lines)
    assert not re.search(r"[a-f0-9]{12}", text), text


def profile_lines() -> list[str]:
    """The read-only section the list opens with, for the profile the fixture approves."""
    today = datetime.now(UTC)
    graduation = "graduating" if (today.year, today.month) <= (2027, 12) else "graduated"
    return [
        "**From your profile** (read-only here)",
        "Name: Alex Example",
        f"School: Example University · B.S. in Computer Science · {graduation} December 2027",
        "Location: Springfield, IL",
        "Links: linkedin.com/in/alex-example · github.com/alex-example · alex.example",
        "Work authorization, visa sponsorship: saved",
    ]


def noon(month: int, day: int, year: int | None = None) -> str:
    """Local noon on a day of this year (or the given one), as the store writes times."""
    year = year or datetime.now(UTC).astimezone().year
    return datetime(year, month, day, 12).astimezone().isoformat()


def test_list_groups_answers_most_used_first_and_pages_with_more(state, chat):
    # Nothing remembered yet: the profile still shows what Rove knows.
    (empty,) = chat.say("what do you know")
    assert empty.split("\n") == [
        *profile_lines(),
        "No answers saved yet. I save each answer you give in an application thread.",
        "The ones marked saved are private; name one to see it.",
    ]
    commute = "Can you commute to our office in Springfield?"
    seed(RELOCATE, ["Yes", "No"], "Yes")
    seed(commute, [], "Yes")
    seed("How did you hear about us?", [], "A friend")
    for n in range(12):
        seed(f"Synthetic question number {n} about widgets?", [], f"answer {n}")
    used(state, "How did you hear about us? (Required)", 3)
    used(state, commute, 2)
    used(state, RELOCATE, 1)
    (reply,) = chat.say("what do you know")
    lines = reply.split("\n")
    assert lines[:6] == profile_lines()
    assert lines[6] == "**Answers you gave** (15, most used first)"
    assert lines[7:11] == [
        "_Other_",
        "1. How did you hear about us? → A friend",
        "2. Synthetic question number 11 about widgets? → answer 11",
        "3. Synthetic question number 10 about widgets? → answer 10",
    ]
    # The used answers are on the first message, together under what they are about.
    assert lines[-5:] == [
        "_Location_",
        "11. Can you commute to our office in Springfield? → Yes",
        "12. Are you willing to relocate? → Yes",
        "The ones marked saved are private; name one to see it.",
        "Say `more` for the other 3.",
    ]
    # Only the answers carry numbers: they are what `forget 3` and `change 3 to No` name.
    assert not any(re.match(r"\d+\. ", x) for x in lines[:7])
    assert len([x for x in lines if re.match(r"\d+\. ", x)]) == memory_channel.PAGE_ITEMS
    assert len(reply) < 2000
    (rest,) = chat.say("more")
    # One topic on the message: no heading needed.
    assert rest.split("\n") == [
        "13. Synthetic question number 2 about widgets? → answer 2",
        "14. Synthetic question number 1 about widgets? → answer 1",
        "15. Synthetic question number 0 about widgets? → answer 0",
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
    # Eligibility facts from the profile are named, never spelled out in the list.
    assert "Work authorization, visa sponsorship: saved" in reply and "→ Yes" not in reply
    # Three answers about three things: a flat list, no topic headings.
    assert not any(x.startswith("_") for x in reply.split("\n"))
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
    assert chat.say("forget 1") == ["No answers are saved, so there is nothing to change."]
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


def test_form_text_is_shown_as_plain_words(state, chat):
    seed("Do you **agree** to [our terms](https://tracker.example/x)?`*", [], "`Yes`")
    (reply,) = chat.say("terms")
    assert reply.startswith("Do you agree to [our terms] (https://tracker.example/x)? → Yes · ")
    assert "](" not in reply and "`" not in reply and "*" not in reply


def test_small_variations_in_wording_still_work(state, chat):
    seed(RELOCATE, [], "Yes")
    seed(MONTHS, [], "5 months")
    assert "**Answers you gave** (2, most used first)" in chat.say("<@1234567890> List all.")[0]
    # A colon followed by a question asks; it never overwrites the answer.
    assert chat.say("relocation: what do you answer?")[0].startswith(
        "Are you willing to relocate? → Yes"
    )
    assert workflow.recall_answer(RELOCATE) == "Yes"
    assert chat.say("Forget #2.")[0].startswith("Forgot")
    assert "**Answers you gave** (1, most used first)" in chat.say("what do you remember?")[0]


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
    memory_channel.mark(memory_channel.NUDGE_MARK)
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
    memory_channel.mark(memory_channel.NUDGE_MARK)
    worker.poll_commands()
    with workflow.db() as conn:
        cursor = int(
            conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id='mem'"
            ).fetchone()[0]
        )
    fake.inbox.append({"id": str(cursor + 1), "author": {"id": "owner"}, "content": "list"})
    worker.poll_commands()
    (reply,) = [payload["content"] for payload in fake.posted]
    assert reply.startswith("**From your profile**") and memory_channel.EMPTY_ANSWERS in reply


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


def test_what_do_you_know_about_a_topic_answers_like_the_topic(state, chat):
    seed(RELOCATE, ["Yes", "No"], "Yes", noon(9, 30))
    plain = chat.say("relocation")
    assert plain == ["Are you willing to relocate? → Yes · you told me on Sep 30"]
    assert chat.say("what do you know about relocation?") == plain
    assert chat.say("What do you remember about relocating") == plain
    (school,) = chat.say("what do you know about my school")
    assert school.startswith("School → Example University · from your approved profile")
    assert chat.say("what do you know about parking?") == [
        "I have nothing about “parking” yet, in your profile or in the answers you gave."
    ]
    # About himself: the whole list.
    assert chat.say("what do you know about me?")[0].startswith("**From your profile**")


def test_location_and_links_are_answered_from_the_profile_by_name(state, chat):
    (place,) = chat.say("location")
    assert place.startswith("Location → Springfield, IL · from your approved profile")
    (github,) = chat.say("what's my github")
    assert github.startswith("GitHub → github.com/alex-example · from your approved profile")
    # No scheme in a reply: Discord would unfurl the link into a preview card.
    assert "http" not in github
    (refused,) = chat.say("forget linkedin")
    assert refused == (
        "LinkedIn comes from your approved profile, so I do not change it here. "
        "Changing it goes through the profile flow."
    )


def test_the_list_says_so_when_the_profile_cannot_be_read(state, chat, monkeypatch):
    def changed():
        raise ValueError("Obsidian profile changed after approval; review it before use")

    seed(RELOCATE, [], "Yes")
    monkeypatch.setattr(memory_channel, "read_approved", changed)
    (reply,) = chat.say("list")
    assert reply.split("\n") == [
        "**From your profile**",
        "I could not read your approved profile just now.",
        "**Answers you gave** (1, most used first)",
        "1. Are you willing to relocate? → Yes",
    ]


def test_a_long_profile_and_many_answers_still_fit_one_message(state, chat):
    for n in range(30):
        seed(f"Synthetic question number {n} " + "about a long widget topic " * 3, [], "x" * 150)
    (reply,) = chat.say("list")
    assert len(reply) <= 1900 and reply.startswith("**From your profile**")
    assert re.search(r"Say `more` for the other \d+\.$", reply)
    seen = len(re.findall(r"^\d+\. ", reply, re.MULTILINE))
    while "Say `more`" in reply:
        (reply,) = chat.say("more")
        assert len(reply) <= 1900
        seen += len(re.findall(r"^\d+\. ", reply, re.MULTILINE))
    assert seen == 30


def observed(state: Path, application_id: str, fields: list[dict]):
    """The last form observation of a past application, as the browser wrote it."""
    directory = state / "applications" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "observation.json").write_text(json.dumps({"fields": fields}))


def field(key: str, label: str, options=(), kind: str = "text", **extra) -> dict:
    choices = [{"label": option} for option in options]
    return {"key": key, "label": label, "options": choices, "kind": kind, **extra}


def answered(application_id, key, value, message_id, kind="answer", when="2026-09-01T12:00:00"):
    """A stored application answer; `kind` is the owner command it came from, if any."""
    with workflow.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO application_answers VALUES(?,?,?,?)",
            (application_id, key, value, message_id),
        )
        if kind:
            payload = json.dumps({"kind": kind, "field_key": key, "value": value})
            conn.execute(
                "INSERT INTO owner_commands VALUES(?,?,?,?,?,?)",
                (message_id, application_id, kind, payload, "applied", when + "+00:00"),
            )


def test_backfill_saves_only_what_the_owner_typed_for_past_applications(state, chat):
    essay = "I want to work at Acme because " + "the warehouse robots are great. " * 8
    veteran = "Are you a protected veteran?"
    observed(
        state,
        "app-one",
        [
            field("k-relocate", RELOCATE, ["- Select -", "Yes", "No"], "choice"),
            field("k-months", MONTHS, ["3 months", "5 months"], "choice"),
            field("k-hear", "How did you hear about us?"),
            field("k-veteran", veteran, ["Yes", "No"], "choice"),
            field("k-yes", "Yes", [], "radio"),
            field("k-cards", "cards[a1b2c3][field0]"),
            field("k-lost", "Team", label_missing=True),
            field("k-why", "Why do you want to work at Acme?", [], "textarea"),
            field("k-email", "Email"),
            field("k-draft", "What is your favorite tool?"),
            field("k-approved", "Describe a project you are proud of"),
            field("k-optional", "Middle name"),
            field("k-setup", "Preferred start month"),
        ],
    )
    answered("app-one", "k-relocate", "Yes", "1001")
    answered("app-one", "k-months", "3 months", "1002")
    answered("app-one", "k-hear", "Keryx jobs feed", "1003")
    answered("app-one", "k-veteran", "No", "1004")
    answered("app-one", "k-yes", "Yes", "1005")
    answered("app-one", "k-cards", "Sometimes", "1006")
    answered("app-one", "k-lost", "Platform", "1007")
    answered("app-one", "k-why", essay, "1008")
    answered("app-one", "k-email", "other@example.net", "1009")
    # Not typed by the owner: a draft used by policy, a draft he approved, a blank left on
    # an optional field, and a setup reply with no owner message behind it.
    answered("app-one", "k-draft", "A drafted answer", "auto-draft:0123456789ab", kind=None)
    answered("app-one", "k-approved", "A draft the owner approved", "1010", kind="use")
    answered("app-one", "k-optional", "skip", "auto-skip:k-optional", kind=None)
    answered("app-one", "k-setup", "June", "codex-owner-reply:call 7", kind=None)
    # A later application asked the months question again; its form has moved on, so the
    # question's words come from the thread's record of the answer.
    answered("app-two", "k-months-two", "5 months", "2001", when="2026-09-20T12:00:00")
    workflow.record(
        "app-two",
        "owner_answer",
        {"kind": "answer", "field_key": "k-months-two", "value": "5 months", "label": MONTHS},
    )
    # Already remembered, and changed since: never overwritten.
    seed(RELOCATE, [], "No")

    result = memory_channel.backfill_from_applications()
    assert result["saved"] == 2
    assert result["labels"] == [memory_channel.words(MONTHS), "How did you hear about us?"]
    assert result["skipped"] == {
        "already saved": 1,
        "replaced by a later answer": 1,
        "private or manual": 1,
        "label is an answer, not a question": 1,
        "question could not be read": 2,
        "long written answer": 1,
        "answered by your profile": 1,
    }
    no_ids(result)
    assert workflow.recall_answer(MONTHS, ["3 months", "5 months"]) == "5 months"
    assert workflow.recall_answer("How did you hear about us? (required)") == "Keryx jobs feed"
    assert workflow.recall_answer(RELOCATE) == "No"
    for label in (
        veteran,
        "Yes",
        "Team",
        "Email",
        "Why do you want to work at Acme?",
        "What is your favorite tool?",
        "Describe a project you are proud of",
        "Middle name",
        "Preferred start month",
    ):
        assert workflow.recall_answer(label) is None, label
    with workflow.db() as conn:
        sources = {r[0] for r in conn.execute("SELECT owner_message_id FROM answer_memory")}
    assert sources == {"m-seed", "2001", "1003"}
    assert "Keryx jobs feed" in answers_note() and "robots" not in answers_note()
    # Old answers are not announced one by one; they are simply in the list.
    assert chat.tick() == []
    assert "How did you hear about us? → Keryx jobs feed" in chat.say("list")[0]
    # Running it again changes nothing.
    again = memory_channel.backfill_from_applications()
    assert again["saved"] == 0 and again["labels"] == []
    assert again["skipped"]["already saved"] == 3
    # Setup replies are taken only when asked for.
    setup = memory_channel.backfill_from_applications(include_setup_replies=True)
    assert setup["labels"] == ["Preferred start month"]
    assert workflow.recall_answer("Preferred start month") == "June"


def test_backfill_with_no_past_answers_saves_nothing(state):
    assert memory_channel.backfill_from_applications() == {"saved": 0, "labels": [], "skipped": {}}


# --- the warm-up ---------------------------------------------------------------------
# With the fixture's profile these corpus questions are open: the profile holds the
# links, the graduation month, work authorization and sponsorship; contact consent has a
# standing rule; GPA is not disclosed.
OPEN_LABELS = [
    "When can you start?",
    "Until when are you available?",
    "How many hours per week can you work?",
    "Desired pay",
    "Are you willing to relocate?",
    "Preferred locations",
    "Preferred work arrangement",
    "Do you have an active security clearance?",
    "Can you provide references?",
    "Which languages do you speak?",
    "Do you have a valid driver's licence?",
    "Are you at least 18 years old?",
    (
        "Are you able to perform the essential functions of the job with or without "
        "reasonable accommodation?"
    ),
    "Do you consent to a background check?",
    "Pronouns",
    "Preferred name",
    "Street address",
    "Country of residence",
    "When did you start at your current school?",
    "Which companies have you worked for, as an employee, intern or contractor?",
    "Are you related to any current employees of this company?",
    "Do you know any current employees of this company?",
    "Were you referred by a current employee of this company?",
    "Have you interviewed with this company before?",
    "Have you applied to this company before?",
]


def numbered_lines(card: str) -> list[str]:
    return [line for line in card.split("\n") if re.match(r"(?:~~)?\d+\. ", line)]


def form(label: str, options=(), kind: str = "text") -> dict:
    return {"label": label, "kind": kind, "options": [{"label": o} for o in options]}


def test_warm_up_lists_the_questions_nothing_answers_yet(state, chat):
    (card,) = chat.say("warm up")
    lines = card.split("\n")
    assert lines[0].startswith("**Common application questions** · 25 to fill in. Answer with")
    assert lines[1] == "1. When can you start? (a date, like Jan 15 2027)"
    assert "7. Preferred work arrangement (Remote / Hybrid / On-site)" in lines
    assert "8. Do you have an active security clearance? (Yes / No)" in lines
    assert lines[-1] == "Say `more` for the other 13."
    assert len(numbered_lines(card)) == 12 and len(card) < 2000
    (rest,) = chat.say("more")
    assert rest.startswith("**Common application questions** (continued)\n13. ")
    (last,) = chat.say("more")
    assert last.startswith("**Common application questions** (continued)\n25. ")
    rest += "\n" + last
    asked = [re.sub(r"^\d+\. | \(.*\)$", "", line) for line in numbered_lines(card + "\n" + rest)]
    assert asked == OPEN_LABELS
    for answered_elsewhere in ("LinkedIn profile URL", "GitHub URL", "Expected graduation date"):
        assert answered_elsewhere not in card + rest
    assert chat.say("more") == ["That is all the questions. Answer any of them with its number."]
    no_ids([card, rest])
    # The other words for it start the list over.
    for words in ("fill in the blanks", "questions", "Warm-up"):
        assert chat.say(words)[0].startswith("**Common application questions** · 25")


def test_warm_up_answers_are_checked_kept_and_struck_through(state, chat):
    (card,) = chat.say("warm up")
    (reply,) = chat.say("1: Jan 15 2027\n3: 40 hours\n5: yes\n8: no\n4: $32/hour")
    assert reply.split("\n") == [
        "1. When can you start? → January 15, 2027",
        "3. How many hours per week can you work? → 40",
        "5. Are you willing to relocate? → Yes",
        "8. Do you have an active security clearance? → No",
        "4. Desired pay → $32/hour",
    ]
    # The card is redrawn in place: answered questions struck, private ones not spelled out.
    (target, redrawn) = chat.edits[-1]
    assert target == "901" and redrawn.startswith(card.split("\n")[0])
    assert "~~1. When can you start?~~ → January 15, 2027" in redrawn
    assert "~~3. How many hours per week can you work?~~ → 40" in redrawn
    assert "~~8. Do you have an active security clearance?~~ → saved" in redrawn
    assert "~~4. Desired pay~~ → saved" in redrawn and "$32" not in redrawn
    assert "2. Until when are you available? (a date, like Aug 20 2027)" in redrawn
    # Kept under the canonical id, as the owner's own words, with the sensitive marker.
    rows = {row["canonical_id"]: row for row in workflow.remembered_answers()}
    assert rows["security_clearance"]["sensitivity"] == "sensitive"
    assert rows["security_clearance"]["origin"] == "owner"
    assert rows["salary_expectation_hourly"]["value"] == "$32/hour"
    assert rows["start_availability"]["sensitivity"] == "plain"
    # The live resolver finds them under a form's own wording.
    profile = read_approved()["profile"]
    for label, options, value in (
        ("What is your earliest available start date?", (), "January 15, 2027"),
        ("Start date", (), "January 15, 2027"),
        ("Hours per week available", (), "40"),
        # Willingness rules read past extra words, so an answer holds for its own wording.
        ("Are you willing to relocate? (Required)", ("Yes", "No"), "Yes"),
        ("Do you currently hold an active security clearance?", ("Yes", "No"), "No"),
        ("Expected hourly rate", (), "$32/hour"),
        ("Salary expectations", (), "$32/hour"),
    ):
        kind = "select-one" if options else "text"
        got = questions.resolve(form(label, options, kind), profile, recall=workflow.recall_answer)
        assert got == (value, questions.REMEMBERED), label
    # Not news for the channel, and no longer open.
    assert chat.tick() == []
    (again,) = chat.say("warm up")
    assert "20 to fill in" in again and "When can you start?" not in again
    no_ids([reply, redrawn])


def test_an_answer_of_the_wrong_kind_is_refused_and_nothing_is_kept(state, chat):
    chat.say("warm up")
    (reply,) = chat.say("1: soon\n3: 500\n8: maybe\n7: tuesday\n99: yes\nhello there")
    assert reply.split("\n") == [
        "1. When can you start?: I need a date like Jan 15 2027 or 2027-01-15.",
        "3. How many hours per week can you work?: I need a number between 1 and 80.",
        "8. Do you have an active security clearance?: I need yes or no.",
        "7. Preferred work arrangement: I need one of Remote / Hybrid / On-site.",
        "99: there is no question 99; the list goes up to 25.",
        "Could not read “hello there”: answer with the number, like `3: yes`.",
    ]
    assert workflow.remembered_answers() == [] and chat.edits == []


def test_skip_leaves_a_question_for_run_time(state, chat):
    chat.say("warm up")
    assert chat.say("10: skip") == [
        "10. Which languages do you speak? → skipped; I will ask when a form needs it."
    ]
    assert "~~10. Which languages do you speak?~~ → skipped" in chat.edits[-1][1]
    assert workflow.recall_answer("Which languages do you speak?") is None
    assert "Which languages do you speak?" in chat.say("warm up")[0]


def test_forget_and_change_go_with_the_warm_up_numbers_until_another_list(state, chat):
    chat.say("warm up")
    chat.say("5: yes")
    assert chat.say("change 5 to no") == ["5. Are you willing to relocate? → No"]
    assert workflow.recall_answer("Are you willing to relocate?", ["Yes", "No"]) == "No"
    assert chat.say("forget 5") == [
        "Forgot “Are you willing to relocate?”. I will ask you the next time a form needs it."
    ]
    assert workflow.recall_answer("Are you willing to relocate?") is None
    assert "5. Are you willing to relocate? (Yes / No)" in chat.edits[-1][1]
    assert chat.say("forget 5") == ["Nothing is saved for 5 yet."]
    chat.say("3: 40")
    # After `list`, the numbers go with the saved answers again.
    assert "1. How many hours per week can you work? → 40" in chat.say("list")[0]
    assert chat.say("1: 35") == [
        "Updated: when a form asks “How many hours per week can you work?”, I now answer 35."
    ]
    assert chat.say("forget 7") == ["There is no 7 in the last list; it goes up to 1."]
    # And while the warm-up list is current, a saved answer is not reached by number.
    chat.say("warm up")
    chat.say("list")
    chat.say("questions")
    assert chat.say("forget relocation") == [
        "I have nothing saved about that. Say `list` to see what I remember."
    ]


def test_the_warm_up_nudge_is_posted_once(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "guild_id": "g", "memory_channel_id": "mem"},
    )
    monkeypatch.setattr(memory_channel, "discord", fake)
    monkeypatch.setattr(workflow, "discord", fake)
    assert fake.tick() == [memory_channel.NUDGE_LINE]
    assert fake.tick() == [] and fake.tick() == []
    assert memory_channel.marked(memory_channel.NUDGE_MARK)


def test_no_nudge_when_there_is_nothing_to_fill_in(state, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "guild_id": "g", "memory_channel_id": "mem"},
    )
    monkeypatch.setattr(memory_channel, "discord", fake)
    monkeypatch.setattr(memory_channel, "open_warmup", list)
    assert fake.tick() == [] and memory_channel.marked(memory_channel.NUDGE_MARK)
    with workflow.db() as conn:
        fake.cursor = int(
            conn.execute(
                "SELECT message_id FROM workflow_checkpoints WHERE channel_id='mem'"
            ).fetchone()[0]
        )
    assert fake.say("warm up")[0].startswith("Nothing to fill in:")


def test_a_card_edit_discord_refuses_does_not_hold_up_the_replies(state, chat):
    chat.say("warm up")
    chat.refuse_edits = True
    assert chat.say("3: 40") == ["3. How many hours per week can you work? → 40"]
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT key,delivery FROM memory_outbox WHERE key LIKE 'edit:%'"
        ).fetchall()
    assert [row["delivery"] for row in rows] == ["failed"]
    assert "memory" in (state / "logs/delivery-failures.log").read_text()
    chat.refuse_edits = False
    assert chat.say("5: yes") == ["5. Are you willing to relocate? → Yes"]
    assert "~~5. Are you willing to relocate?~~ → Yes" in chat.edits[-1][1]


def test_a_corpus_question_left_open_is_asked_once_at_run_time_then_remembered(state, chat):
    profile = read_approved()["profile"]
    asked = {
        "label": "How many hours per week are you available?",
        "name": "hours",
        "kind": "text",
        "required": True,
        "options": [],
    }
    # Nothing answers it: the run stops and asks the owner, as before the warm-up.
    assert questions.resolve(asked, profile, recall=workflow.recall_answer) == (None, None)
    app = workflow.enqueue("https://job-boards.greenhouse.io/acme/jobs/1001")["application_id"]
    asked["key"] = workflow.field_key(asked)
    directory = state / "applications" / app
    directory.mkdir(parents=True)
    (directory / "observation.json").write_text(json.dumps({"fields": [asked]}))
    apply_command(
        {"kind": "answer", "application_id": app, "field_key": asked["key"], "value": "40"}, "m1"
    )
    # Once: the next form, in other words, is filled from that reply.
    later = form("Hours per week available")
    assert questions.resolve(later, profile, recall=workflow.recall_answer) == (
        "40",
        questions.REMEMBERED,
    )
    assert chat.tick() == [
        "Saved: when a form asks “How many hours per week are you available?”, I answer 40."
    ]
    (card,) = chat.say("warm up")
    assert "hours per week" not in card.lower() and "24 to fill in" in card
