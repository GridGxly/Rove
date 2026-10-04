"""The fast lane in agent-control, answered by code through the Hermes gateway plugin.

`rove.shortcuts` decides; `integrations/hermes/rove_shortcuts` is the plugin that asks it
and posts the answer. Only exact forms are taken here: a message that is nothing but
links, `/new` and `/reset`, and the few requests on the pinned help. Each gets exactly
one reply and no model turn. Every other message, however it is worded and whoever
wrote it, goes on to the model untouched; this lane never answers "not understood".
"""

import asyncio
import importlib.util
import io
import json
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from rove import chat, inbound, shortcuts, workflow
from rove.onboarding import approve, digest, draft, propose

REPOSITORY = Path(__file__).resolve().parents[1]
OWNER = "1001"
SETTINGS = {
    "enabled": True,
    "control_channel_id": "control",
    "action_channel_id": "action",
    "shortlist_channel_id": "short",
    "system_channel_id": "sys",
}


def load_plugin():
    path = REPOSITORY / "integrations/hermes/rove_shortcuts/__init__.py"
    spec = importlib.util.spec_from_file_location("rove_shortcuts_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def eval_phrases() -> list[str]:
    path = REPOSITORY / "tests/chat_eval/phrases.jsonl"
    return [json.loads(line)["say"] for line in path.read_text().splitlines() if line.strip()]


# --- the fast lane: exact forms only ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("status", "status"),
        ("Status?", "status"),
        ("STATUS.", "status"),
        ("whats waiting", "waiting"),
        ("What’s waiting on me?", "waiting"),
        ("how many did you send today", "sends"),
        ("How many did you send today??", "sends"),
        ("pause", "pause"),
        ("Resume!", "resume"),
        ("help", "help"),
        ("?", "help"),
        ("https://jobs.lever.co/acme/1", "paste"),
        ("<https://jobs.lever.co/acme/1> https://jobs.lever.co/acme/2", "paste"),
        ("first", "first"),
        ("Move it up.", "first"),
        ("/new", "reset"),
        ("/reset", "reset"),
    ],
)
def test_exact_forms_take_the_fast_lane(text, kind):
    assert shortcuts.match(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hi",
        "yo rove status please",
        "whats good",
        "pause the feed",
        "pause rn im busy",
        "status of acme",
        "resume 0123456789ab",  # the worker's explicit form, never a feed resume
        "hi apply to this https://jobs.lever.co/acme/1",
        "https://jobs.lever.co/acme/1 first",
        "why did you skip initech",
        "y did u skip acme",
        "put that one first",
        "!!!",
        "👍",
    ],
)
def test_everything_else_goes_to_the_model(text):
    assert shortcuts.match(text) is None


def test_no_phrase_table_reads_his_meaning():
    for name in ("COMPANY", "FILLER"):
        assert not hasattr(shortcuts, name), name
    assert len(shortcuts.REQUESTS) <= 10


# --- deciding one message -------------------------------------------------------


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
    (root / "config/setup.env").write_text(f"DISCORD_OWNER_USER_ID={OWNER}\n")
    logged = []
    monkeypatch.setattr(workflow, "system_line", lambda app, text: logged.append(text))
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {"id": "m"})
    return SimpleNamespace(root=root, logged=logged)


def message(content, **extra):
    return {
        "id": extra.pop("id", "7001"),
        "content": content,
        "author_id": OWNER,
        "bot": False,
        "channel_id": "control",
        "thread_id": None,
        "reply_to": None,
        **extra,
    }


def test_only_the_owner_in_agent_control_is_ever_answered(state):
    for other in (
        message("status", author_id="666"),
        message("status", bot=True),
        message("status", channel_id="action"),
        message("status", thread_id="t1"),
    ):
        assert shortcuts.handle(other) == {"handled": False, "reply": "", "control": False}
    assert shortcuts.handle(message("where do i go to school")) == {
        "handled": False,
        "reply": "",
        "control": True,
    }
    chat.set_setting("enabled", False)
    assert shortcuts.handle(message("status"))["handled"] is False
    assert state.logged == []


def test_no_message_in_his_own_words_is_answered_or_turned_down_by_code(state):
    """Every phrase of the chat evaluation that is not an exact form reaches the model."""
    for number, text in enumerate(eval_phrases()):
        decision = shortcuts.handle(message(text, id=f"e{number}"))
        if shortcuts.match(text) is None:
            assert decision == {"handled": False, "reply": "", "control": True}, text
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("text", "expected", "outcome"),
    [
        ("status", lambda: chat.status()["say"], "0 need him, 0 queued, 0 in progress"),
        ("what's waiting on me", lambda: chat.waiting()["say"], "nothing waits on him"),
        ("how many did you send today", lambda: chat.sent_today()["say"], "0 sent today"),
        ("help", lambda: chat.help_reply()["say"], "showed the help"),
    ],
)
def test_each_request_gets_the_words_the_mcp_tool_would_give(state, text, expected, outcome):
    decision = shortcuts.handle(message(text))
    assert decision == {"handled": True, "reply": expected(), "control": True}
    assert len(state.logged) == 1
    assert state.logged[0].startswith("shortcut · ") and f" · {outcome} · " in state.logged[0]


def test_pause_and_resume_flip_the_switch_and_log_what_changed(state):
    assert shortcuts.handle(message("pause"))["reply"].startswith("Paused.")
    stored = json.loads((state.root / "config/workflow.json").read_text())
    assert stored["feed_paused"] is True
    assert shortcuts.handle(message("Resume", id="7002"))["reply"].startswith("The feed is running")
    assert json.loads((state.root / "config/workflow.json").read_text())["feed_paused"] is False
    assert [line.split(" · ")[2] for line in state.logged] == ["feed paused", "feed resumed"]


def test_new_and_reset_start_fresh_with_a_plain_line(state):
    decision = shortcuts.handle(message("/new"))
    assert decision == {"handled": True, "reply": "Fresh start.", "control": True, "reset": True}


def test_a_paste_is_applied_once_and_answered_once(state):
    pasted = message("https://jobs.lever.co/acme/1")
    assert shortcuts.handle(pasted) == {
        "handled": True,
        "reply": "Queued. It goes next.",
        "control": True,
    }
    assert " · queued his pasted link · " in state.logged[-1]
    # The same message again (Hermes retried, or the worker reads it): handled, silent.
    assert shortcuts.handle(pasted) == {"handled": True, "reply": "", "control": True}
    assert " · already answered by the worker · " in state.logged[-1]
    worker_view = {"id": pasted["id"], "author": {"id": OWNER}, "content": pasted["content"]}
    assert inbound.owner_message(worker_view, "control", SETTINGS, {}) == ""
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0] == 1
    # The worker got to the next paste first: it answered, the shortcut stays silent.
    second = {"id": "7003", "author": {"id": OWNER}, "content": "https://jobs.lever.co/acme/2"}
    said = inbound.owner_message(second, "control", SETTINGS, {})
    assert said.startswith("Queued.")
    assert shortcuts.handle(message(second["content"], id="7003"))["reply"] == ""


def test_first_as_a_reply_moves_the_paste_up(state):
    shortcuts.handle(message("https://jobs.lever.co/acme/1", id="7101"))
    shortcuts.handle(message("https://jobs.lever.co/acme/2", id="7102"))
    moved = shortcuts.handle(message("FIRST!", id="7103", reply_to="rove-line"))
    assert moved["reply"] == "Moved it up. It goes next."


def test_a_failure_is_one_plain_line_and_one_log_line(state, monkeypatch):
    def broken():
        raise RuntimeError("/Users/example/secret detail")

    monkeypatch.setattr(chat, "status", broken)
    decision = shortcuts.handle(message("status"))
    assert decision["handled"] is True
    assert decision["reply"] == (
        "That did not work on my side just now. The details are in the system log."
    )
    assert state.logged == ["shortcut · status · failed · RuntimeError"]


def test_the_cli_prints_the_decision_before_it_posts_the_log_line(state, monkeypatch):
    order = []
    out = io.StringIO()
    out.close = lambda: order.append("closed")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(message("status"))))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(workflow, "system_line", lambda app, text: order.append(text))
    shortcuts.main()
    decision = json.loads(out.getvalue())
    assert decision["handled"] is True and decision["reply"] == chat.status()["say"]
    assert order[0] == "closed" and order[1].startswith("shortcut · status · ")
    # Garbage in is no decision: the model answers.
    out = io.StringIO()
    out.close = lambda: None
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.setattr(sys, "stdout", out)
    shortcuts.main()
    assert json.loads(out.getvalue())["handled"] is False


# --- the Hermes plugin ----------------------------------------------------------


class Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True)


class Platform:
    """Hashable like Hermes' Platform enum, which keys `gateway.adapters`."""

    def __init__(self, value):
        self.value = value


def event(text, *, platform="discord", internal=False):
    source = SimpleNamespace(
        platform=Platform(platform),
        chat_id="control",
        user_id=OWNER,
        is_bot=False,
        thread_id=None,
        message_id="8001",
    )
    return SimpleNamespace(
        text=text, source=source, message_id="8001", reply_to_message_id=None, internal=internal
    )


def gateway_with(adapter, source_platform):
    return SimpleNamespace(adapters={source_platform: adapter})


def run(coroutine):
    return asyncio.run(coroutine)


def test_the_plugin_posts_one_reply_and_skips_the_model_turn(monkeypatch):
    plugin = load_plugin()
    asked = []

    async def ask(message, settings):
        asked.append(message)
        return {"handled": True, "reply": "Nothing is in the queue.", "control": True}

    monkeypatch.setattr(plugin, "load_settings", lambda: {"command": ["rove", "shortcut"]})
    monkeypatch.setattr(plugin, "ask_rove", ask)
    adapter = Adapter()
    incoming = event("status")
    result = run(plugin.on_message(incoming, gateway_with(adapter, incoming.source.platform)))
    assert result == {"action": "skip", "reason": "answered by rove"}
    assert adapter.sent == [("control", "Nothing is in the queue.")]
    assert asked == [
        {
            "id": "8001",
            "content": "status",
            "author_id": OWNER,
            "bot": False,
            "channel_id": "control",
            "thread_id": None,
            "reply_to": None,
        }
    ]
    # Handled with nothing to say (the worker answered that paste): still no model turn.
    monkeypatch.setattr(
        plugin, "ask_rove", lambda m, s: asyncio.sleep(0, {"handled": True, "reply": ""})
    )
    adapter.sent.clear()
    assert run(plugin.on_message(incoming, gateway_with(adapter, incoming.source.platform)))
    assert adapter.sent == []


def test_the_plugin_leaves_everything_else_to_hermes(monkeypatch):
    plugin = load_plugin()
    asked = []

    async def ask(message, settings):
        asked.append(message)
        return {"handled": False, "reply": "", "control": False}

    monkeypatch.setattr(plugin, "load_settings", lambda: {"command": ["rove", "shortcut"]})
    monkeypatch.setattr(plugin, "ask_rove", ask)
    adapter = Adapter()
    for incoming in (
        event("hello"),
        event("status", platform="telegram"),
        event("status", internal=True),
    ):
        assert (
            run(plugin.on_message(incoming, gateway_with(adapter, incoming.source.platform)))
            is None
        )
    assert len(asked) == 1 and adapter.sent == []

    async def broken(message, settings):
        raise OSError("rove is not installed")

    monkeypatch.setattr(plugin, "ask_rove", broken)
    incoming = event("status")
    assert run(plugin.on_message(incoming, gateway_with(adapter, incoming.source.platform))) is None
    monkeypatch.setattr(plugin, "load_settings", lambda: None)
    assert run(plugin.on_message(incoming, gateway_with(adapter, incoming.source.platform))) is None
    assert adapter.sent == []


def test_a_quiet_or_long_chat_starts_fresh_before_the_model_answers(monkeypatch):
    plugin = load_plugin()

    async def ask(message, settings):
        return {"handled": False, "reply": "", "control": True}

    monkeypatch.setattr(plugin, "load_settings", lambda: {"command": ["rove", "shortcut"]})
    monkeypatch.setattr(plugin, "ask_rove", ask)
    resets = []

    class Gateway:
        def __init__(self):
            self.adapters = {}

        def _session_key_for_source(self, source):
            return "agent:main:discord:group:control"

        async def _handle_reset_command(self, incoming):
            resets.append(incoming.text)
            return "fresh"

    @__import__("dataclasses").dataclass
    class Event:
        text: str
        source: object
        message_id: str = "8001"
        reply_to_message_id: object = None
        internal: bool = False

    def store(minutes_quiet, tokens):
        entry = SimpleNamespace(
            updated_at=datetime.now() - timedelta(minutes=minutes_quiet),
            last_prompt_tokens=tokens,
        )
        return SimpleNamespace(lookup_by_session_key=lambda key: entry)

    incoming = Event("where do i go to school", event("x").source)
    for quiet, tokens, expected in ((1, 4500, []), (30, 4500, ["/new"]), (1, 9000, ["/new"])):
        resets.clear()
        assert run(plugin.on_message(incoming, Gateway(), store(quiet, tokens))) is None
        assert resets == expected
    assert incoming.text == "where do i go to school"  # the message itself is untouched


def test_the_plugin_reads_rove_s_decision_without_waiting_for_its_log_line(tmp_path):
    plugin = load_plugin()
    script = tmp_path / "fake_rove.py"
    script.write_text(
        "import json, sys, time\n"
        "message = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'handled': True, 'reply': 'echo ' + message['content']}), flush=True)\n"
        "sys.stdout.close()\n"
        "time.sleep(3)  # the system-log line, posted after the reply\n"
    )
    settings = {"command": [sys.executable, str(script)], "cwd": str(tmp_path)}

    async def timed():
        loop = asyncio.get_running_loop()
        started = loop.time()
        decision = await plugin.ask_rove({"content": "status"}, settings)
        seconds = loop.time() - started
        await asyncio.gather(*plugin.RUNNING)  # the process finishes on its own afterwards
        return decision, seconds

    decision, seconds = run(timed())
    assert decision == {"handled": True, "reply": "echo status"}
    assert seconds < 2.5


async def through(plugin, incoming, gateway):
    """One message through the plugin, then wait for its `rove shortcut` process to end."""
    result = await plugin.on_message(incoming, gateway)
    await asyncio.gather(*plugin.RUNNING)
    return result


def test_the_plugin_drives_the_real_rove_shortcut_command(state, tmp_path, monkeypatch):
    """End to end: the plugin runs `rove shortcut` in a fresh process, as the gateway will."""
    plugin = load_plugin()
    settings = {
        "command": [sys.executable, "-m", "rove.cli", "shortcut"],
        "cwd": str(REPOSITORY),
        "env": {"ROVE_STATE_DIR": str(state.root), "PYTHONPATH": str(REPOSITORY / "src")},
    }
    monkeypatch.setattr(plugin, "load_settings", lambda: settings)
    adapter = Adapter()
    incoming = event("Status?")
    result = run(through(plugin, incoming, gateway_with(adapter, incoming.source.platform)))
    assert result == {"action": "skip", "reason": "answered by rove"}
    assert adapter.sent == [
        ("control", "Nothing is in the queue.\nSent today: 0. The cap is 30 a day.")
    ]
    stranger = event("status")
    stranger.source.user_id = "666"
    assert run(through(plugin, stranger, gateway_with(adapter, stranger.source.platform))) is None
    assert len(adapter.sent) == 1


def test_install_copies_the_plugin_writes_its_settings_and_enables_it(state, tmp_path):
    hermes_home = tmp_path / "hermes"
    called = tmp_path / "called.txt"
    fake_hermes = tmp_path / "hermes-cli"
    fake_hermes.write_text(f'#!/bin/sh\necho "$@" > "{called}"\n')
    fake_hermes.chmod(0o755)
    rove = tmp_path / "rove"
    rove.write_text("#!/bin/sh\n")
    done = shortcuts.install(hermes_home=hermes_home, hermes=str(fake_hermes), executable=rove)
    target = hermes_home / "plugins/rove_shortcuts"
    source = REPOSITORY / "integrations/hermes/rove_shortcuts"
    for name in ("plugin.yaml", "__init__.py"):
        assert (target / name).read_text() == (source / name).read_text()
    settings = json.loads((target / "settings.json").read_text())
    assert settings["command"] == [str(rove), "shortcut"] and settings["env"][
        "ROVE_STATE_DIR"
    ] == str(state.root)
    assert stat.S_IMODE((target / "settings.json").stat().st_mode) == 0o600
    assert called.read_text().split() == ["plugins", "enable", "rove_shortcuts"]
    assert done[-2] == "enabled rove_shortcuts in Hermes"
    assert "restart the gateway" in done[-1]
    # The persona goes in beside the plugin; the one it replaces is kept.
    persona = (REPOSITORY / "integrations/hermes/SOUL.md").read_text()
    assert (hermes_home / "SOUL.md").read_text() == persona
    (hermes_home / "SOUL.md").write_text("You are Hermes Agent.")
    shortcuts.install(hermes_home=hermes_home, hermes=str(fake_hermes), executable=rove)
    kept = list(hermes_home.glob("SOUL.md.before-rove-*"))
    assert len(kept) == 1 and kept[0].read_text() == "You are Hermes Agent."
    assert (hermes_home / "SOUL.md").read_text() == persona
    done = shortcuts.install(hermes_home=hermes_home, hermes=str(fake_hermes), executable=rove)
    assert any("already this checkout's persona" in line for line in done)


def test_new_or_reset_starts_fresh_without_hermes_banner(monkeypatch):
    plugin = load_plugin()

    async def ask(message, settings):
        return {"handled": True, "reply": "Fresh start.", "control": True, "reset": True}

    monkeypatch.setattr(plugin, "load_settings", lambda: {"command": ["rove", "shortcut"]})
    monkeypatch.setattr(plugin, "ask_rove", ask)
    resets = []

    class Gateway:
        def __init__(self, adapter, platform):
            self.adapters = {platform: adapter}

        async def _handle_reset_command(self, incoming):
            resets.append(incoming.text)
            return "✨ Session reset! Starting fresh.\n◆ Model: ..."

    @__import__("dataclasses").dataclass
    class Event:
        text: str
        source: object
        message_id: str = "8001"
        reply_to_message_id: object = None
        internal: bool = False

    adapter = Adapter()
    incoming = Event("/reset", event("x").source)
    result = run(plugin.on_message(incoming, Gateway(adapter, incoming.source.platform)))
    assert result == {"action": "skip", "reason": "answered by rove"}
    assert resets == ["/new"]
    assert adapter.sent == [("control", "Fresh start.")]  # Hermes' banner is never posted


def test_a_reply_claiming_an_action_no_tool_took_is_replaced():
    plugin = load_plugin()
    # The model wrote "Queued." itself, with no tool in the turn: he is told the truth.
    assert plugin.on_reply("Queued.", session_id="s", turn_id="t1", platform="discord") == (
        plugin.NOT_DONE
    )
    assert plugin.on_reply("Got it, 40 hrs saved for xAI.", "s", "t2", "discord") == (
        plugin.NOT_DONE
    )
    # A Rove tool ran in the turn: its words stand.
    plugin.on_tool(tool_name="mcp__rove__apply_to_link", session_id="s", turn_id="t3")
    assert plugin.on_reply("Queued. It goes next.", "s", "t3", "discord") is None
    # Ordinary answers, and other platforms, are left alone.
    assert plugin.on_reply("Hey. What do you need?", "s", "t4", "discord") is None
    assert plugin.on_reply("Queued.", "s", "t5", "cli") is None
    plugin.on_tool(tool_name="read_file", session_id="s", turn_id="t6")
    assert plugin.on_reply("Saved.", "s", "t6", "discord") == plugin.NOT_DONE
    registered = []
    plugin.register(SimpleNamespace(register_hook=lambda name, fn: registered.append(name)))
    assert registered == ["pre_gateway_dispatch", "post_tool_call", "transform_llm_output"]
