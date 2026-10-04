"""The fast lane in agent-control: exact forms answered by code before any model turn.

The Hermes gateway runs the `rove_shortcuts` plugin (`integrations/hermes/rove_shortcuts`).
For every message it receives, the plugin asks `rove shortcut` here, with the message
on stdin. When the message is from the configured owner, in agent-control, and is one
of the exact forms below, this module answers it with the same functions the MCP tools
use, and the plugin posts that line and tells Hermes the message is handled, so no
model turn runs and nothing else answers it.

This lane is for speed only and understands nothing. It takes a message that is
nothing but links, `/new` or `/reset`, and a few exact requests (the ones on the pinned
help); case, apostrophes and punctuation around them are ignored, and nothing else is.
Every other message goes to the model, which works out what he means however he puts
it. Nothing here ever answers "not understood".
"""

import json
import re
import sys
import time

from . import chat, inbound, workflow

# The exact requests, after `exact()`: lower case, no apostrophes, no punctuation.
REQUESTS = {
    "status": "status",
    "whats waiting": "waiting",
    "whats waiting on me": "waiting",
    "how many did you send today": "sends",
    "pause": "pause",
    "resume": "resume",
    "help": "help",
    "": "help",  # a message that is only "?"
    "first": "first",
    "move it up": "first",
}
RESETS = {"/new", "/reset"}  # Hermes' own commands, answered without its banner


def exact(text) -> str:
    """The message as an exact form: lower case, apostrophes and punctuation dropped."""
    text = str(text or "").lower().replace("’", "").replace("'", "")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def match(text) -> str | None:
    """The kind of an exact form, or None: then the model reads the message."""
    text = str(text or "").strip()
    if not text:
        return None
    if text.lower() in RESETS:
        return "reset"
    if inbound.pasted_links(text):
        return "paste"
    form = exact(text)
    if form == "" and text.strip("?") != "":
        return None  # only emoji or symbols: the model answers
    return REQUESTS.get(form)


def answer(kind: str, message: dict) -> dict:
    """{"say", "outcome"} for one exact form, from the functions the MCP tools use."""
    if kind == "status":
        return chat.status()
    if kind == "waiting":
        return chat.waiting()
    if kind == "sends":
        return chat.sent_today()
    if kind == "pause":
        return chat.pause_feed()
    if kind == "resume":
        return chat.resume_feed()
    if kind == "help":
        return chat.help_reply()
    if kind == "reset":
        return {"say": "Fresh start.", "outcome": "chat started fresh"}
    # A paste or `first`: applied once, by whichever reader gets there first ("" otherwise).
    if kind == "first":
        message = {**message, "content": "first"}  # the exact form, however it was typed
    line = inbound.control_line(message) or ""
    if kind == "paste":
        outcome = "queued his pasted link" if line.startswith("Queued") else "pasted link"
    else:
        outcome = "moved his latest paste up" if line.startswith("Moved") else "first"
    return {"say": line, "outcome": outcome if line else "already answered by the worker"}


def decide(message: dict) -> tuple[dict, list[str]]:
    """Decide one gateway message: ({"handled", "reply", "control"}, system-log lines).

    `control` says the message is the owner's, in agent-control: the plugin keeps that
    conversation short. Nobody else's message, and no other channel, is ever handled.
    The log lines are posted by the caller once the reply is on its way.
    """
    settings = workflow.config()
    channel = str(settings.get("control_channel_id") or "")
    owner = chat.owner_id()
    mine = bool(
        settings.get("enabled")
        and channel
        and owner
        and str(message.get("channel_id") or "") == channel
        and not message.get("thread_id")
        and str(message.get("author_id") or "") == str(owner)
        and not message.get("bot")
    )
    if not mine:
        return {"handled": False, "reply": "", "control": False}, []
    kind = match(message.get("content"))
    if not kind:
        return {"handled": False, "reply": "", "control": True}, []
    shaped = {"id": message.get("id"), "content": message.get("content") or ""}
    if message.get("reply_to"):
        shaped["message_reference"] = {"message_id": str(message["reply_to"])}
    started = time.monotonic()
    try:
        result = answer(kind, shaped)
    except Exception as error:  # noqa: BLE001 -- he gets one plain line, the log gets the type
        return (
            {
                "handled": True,
                "reply": "That did not work on my side just now. The details are in the system log.",
                "control": True,
            },
            [f"shortcut · {kind} · failed · {type(error).__name__}"],
        )
    seconds = f"{time.monotonic() - started:.1f} s"
    decision = {"handled": True, "reply": result["say"], "control": True}
    if kind == "reset":
        decision["reset"] = True  # the plugin starts the chat fresh, without Hermes' banner
    return decision, [f"shortcut · {kind} · {result['outcome']} · {seconds}"]


def handle(message: dict) -> dict:
    """`decide`, with its system-log lines posted at once."""
    decision, lines = decide(message)
    for line in lines:
        workflow.system_line("chat", line)
    return decision


def main():
    """`rove shortcut`: one message as JSON on stdin, the decision as one JSON line on stdout.

    The decision is printed and stdout closed first; the system-log line is posted after
    that, so the plugin answers without waiting for Discord.
    """
    lines: list[str] = []
    try:
        message = json.loads(sys.stdin.read() or "{}")
        decision, lines = decide(message if isinstance(message, dict) else {})
    except Exception as error:  # noqa: BLE001 -- the gateway then lets the model answer
        decision = {"handled": False, "reply": "", "control": False, "error": type(error).__name__}
    sys.stdout.write(json.dumps(decision) + "\n")
    sys.stdout.flush()
    sys.stdout.close()
    for line in lines:
        workflow.system_line("chat", line)


# --- installing the plugin into Hermes ----------------------------------------------

PLUGIN = "rove_shortcuts"


def install(hermes_home=None, hermes=None, executable=None) -> list[str]:
    """Copy the plugin into the Hermes plugins directory, write its settings, put this
    checkout's persona in place (keeping the previous one), and enable the plugin.

    Returns what was done, one line each. The gateway picks it all up on restart; a
    conversation already open keeps its old persona until it starts fresh.
    """
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from .runtime import state_root

    repo = Path(__file__).resolve().parents[2]
    source = repo / "integrations/hermes" / PLUGIN
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    target = home / "plugins" / PLUGIN
    executable = Path(executable or repo / ".venv/bin/rove")
    if not (source / "plugin.yaml").is_file():
        raise ValueError(f"Plugin source not found at {source}")
    if not executable.is_file():
        raise ValueError("Install the project virtual environment first")
    done = []
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("plugin.yaml", "__init__.py"):
        shutil.copyfile(source / name, target / name)
        done.append(f"copied {name} to {target}")
    settings = {
        "command": [str(executable), "shortcut"],
        "cwd": str(repo),
        "env": {"ROVE_STATE_DIR": str(state_root())},
    }
    path = target / "settings.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
    done.append(f"wrote {path} (runs {executable} shortcut)")
    # The chat's persona is versioned here too, beside the tools it names.
    persona, soul = repo / "integrations/hermes/SOUL.md", home / "SOUL.md"
    if soul.is_file() and soul.read_text() == persona.read_text():
        done.append(f"{soul} is already this checkout's persona")
    else:
        if soul.is_file():
            kept = soul.with_name(f"SOUL.md.before-rove-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(soul, kept)
            done.append(f"kept the previous persona as {kept}")
        shutil.copyfile(persona, soul)
        done.append(f"copied the persona to {soul}")
    hermes = hermes or str(Path.home() / ".local/bin/hermes")
    result = subprocess.run(
        [hermes, "plugins", "enable", PLUGIN], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        done.append(f"enabled {PLUGIN} in Hermes")
    else:
        done.append(f"could not enable it: run `hermes plugins enable {PLUGIN}` yourself")
    done.append("restart the gateway to load it: rove gateway restart")
    return done
