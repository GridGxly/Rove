"""Fixed requests in agent-control, answered by code before any model turn.

The Hermes gateway runs the `rove_shortcuts` plugin (`integrations/hermes/rove_shortcuts`).
For every message it receives, the plugin asks `rove shortcut` here, with the message
on stdin. When the message is from the configured owner, in agent-control, and is one
of the fixed requests below, this module answers it with the same functions the MCP
tools use, and the plugin posts that line and tells Hermes the message is handled, so
no model turn runs and nothing else answers it. Anything else falls through to the
model as before.

Matching is forgiving about case, punctuation, apostrophes and a greeting or "please"
around the words, and strict about everything else: a sentence that only contains
"status" is a question for the model, not this.
"""

import json
import re
import sys
import time

from . import chat, inbound, workflow
from .discord_feed import private_env

# Words around a request that change nothing: "yo rove status please".
FILLER = frozenset(
    {"yo", "hey", "hi", "hello", "ok", "okay", "rove", "please", "pls", "plz", "thanks", "thx"}
)
REQUESTS = {
    "status": {
        "status",
        "status update",
        "whats the status",
        "what is the status",
        "whats your status",
        "give me the status",
        "give me a status update",
        "queue status",
        "how is the queue",
        "hows the queue",
    },
    "waiting": {
        "waiting",
        "whats waiting",
        "what is waiting",
        "whats waiting on me",
        "whats waiting for me",
        "what is waiting on me",
        "what is waiting for me",
        "anything waiting",
        "anything waiting on me",
        "anything waiting for me",
        "what needs me",
        "what needs my attention",
    },
    "sends": {
        "sends",
        "sent today",
        "sends today",
        "how many did you send today",
        "how many did you send",
        "how many did you sent today",
        "how many sent today",
        "how many applications today",
        "how many did you apply to today",
        "how many applications did you send today",
    },
    "pause": {"pause", "pause the feed", "pause feed", "pause the feed jobs", "pause feed jobs"},
    "resume": {
        "resume",
        "resume the feed",
        "resume feed",
        "resume the feed jobs",
        "unpause",
        "unpause the feed",
        "start the feed again",
    },
    "help": {
        "help",
        "what can i ask",
        "what can i ask you",
        "what can you do",
        "what do you do",
        "commands",
        "",  # a message that is only "?"
    },
}
# "why did you skip Acme", "why didn't you apply to Acme", "why was Acme skipped",
# "did I apply to Acme": the company is the rest of the sentence, a few words at most.
COMPANY = (
    re.compile(
        r"^(?:why did you skip|why did you not apply to|why didn'?t you apply to"
        r"|did i apply to|did you apply to|did we apply to)\s+(?P<company>[^?!.]+?)\s*[?!.]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^why was\s+(?P<company>[^?!.]+?)\s+skipped\s*[?!.]*$", re.IGNORECASE),
)
# What the system-log line says for each shortcut.
WORDS = {
    "status": "read the status",
    "waiting": "listed what waits on the owner",
    "sends": "counted today's sends",
    "pause": "paused the feed",
    "resume": "resumed the feed",
    "help": "showed the help",
    "company": "looked up one company",
    "paste": "queued a pasted link",
    "first": "moved a pasted link up",
}


def normal(text) -> str:
    """Lower case, no apostrophes or punctuation, filler words at the edges dropped."""
    text = str(text or "").lower().replace("’", "'").replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[0] in FILLER:
        tokens.pop(0)
    while tokens and tokens[-1] in FILLER:
        tokens.pop()
    return " ".join(tokens)


def match(text) -> tuple[str, str] | None:
    """(kind, argument) for a fixed request, None for anything the model should answer."""
    text = str(text or "").strip()
    if not text or len(text) > 600:
        return None
    if inbound.pasted_links(text):
        return ("paste", "")
    if inbound.words(text) in inbound.FIRST_REPLIES:
        return ("first", "")
    if re.search(r"https?://", text, re.IGNORECASE):
        return None  # a link with a question or a comment is the model's
    plain = normal(text)
    for kind, phrases in REQUESTS.items():
        if plain in phrases and (plain or text.strip("? ") == ""):
            return (kind, "")
    sentence = " ".join(text.replace("’", "'").split())
    for pattern in COMPANY:
        found = pattern.match(sentence)
        company = found["company"].strip(" \"'`*_") if found else ""
        if 2 <= len(company) <= 60 and normal(company) and len(company.split()) <= 5:
            return ("company", company)
    return None


def answer(kind: str, argument: str, message: dict) -> str:
    """The words for one fixed request, from the functions the MCP tools use."""
    if kind == "status":
        return chat.status()["say"]
    if kind == "waiting":
        return chat.waiting()["say"]
    if kind == "sends":
        return chat.sent_today()["say"]
    if kind == "pause":
        return chat.pause_feed()["say"]
    if kind == "resume":
        return chat.resume_feed()["say"]
    if kind == "help":
        return chat.help_reply()["say"]
    if kind == "company":
        return chat.company_history(argument)["say"]
    # A paste or `first`: applied once, by whichever reader gets there first ("" otherwise).
    return inbound.control_line(message) or ""


def owner_id() -> str:
    env = private_env()
    return env.get("DISCORD_OWNER_USER_ID") or env.get("DISCORD_ALLOWED_USERS", "").split(",")[0]


def decide(message: dict) -> tuple[dict, list[str]]:
    """Decide one gateway message: ({"handled", "reply", "control"}, system-log lines).

    `control` says the message is the owner's, in agent-control: the plugin keeps that
    conversation short. Nobody else's message, and no other channel, is ever handled.
    The log lines are posted by the caller once the reply is on its way.
    """
    settings = workflow.config()
    channel = str(settings.get("control_channel_id") or "")
    owner = owner_id()
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
    found = match(message.get("content"))
    if not found:
        return {"handled": False, "reply": "", "control": True}, []
    kind, argument = found
    shaped = {"id": message.get("id"), "content": message.get("content") or ""}
    if message.get("reply_to"):
        shaped["message_reference"] = {"message_id": str(message["reply_to"])}
    started = time.monotonic()
    try:
        reply = answer(kind, argument, shaped)
    except Exception as error:  # noqa: BLE001 -- he gets one plain line, the log gets the type
        return (
            {
                "handled": True,
                "reply": "That did not work on my side just now. The details are in the system log.",
                "control": True,
            },
            [f"shortcut · {WORDS[kind]} · failed · {type(error).__name__}"],
        )
    seconds = f"{time.monotonic() - started:.1f} s"
    return (
        {"handled": True, "reply": reply, "control": True},
        [f"shortcut · {WORDS[kind]} · {seconds} · ok"],
    )


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
    """Copy the plugin into the Hermes plugins directory, write its settings, enable it.

    Returns what was done, one line each. The gateway picks the plugin up on restart.
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
