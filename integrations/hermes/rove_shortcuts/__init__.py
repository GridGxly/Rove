"""Rove shortcuts for the Hermes gateway.

Hermes calls `on_message` for every incoming message before it authorizes the sender or
starts a model turn (the `pre_gateway_dispatch` hook). This plugin hands each Discord
message to Rove's own code (`rove shortcut`, run with the repository's Python) and does
what Rove decides:

- an exact fixed form from the owner in agent-control (a message that is only links,
  `status`, `what's waiting`, `how many did you send today`, `pause`, `resume`, `help`,
  `first`, `/new`, `/reset`): post Rove's line in the channel and tell Hermes the
  message is handled, so no model turn runs and nothing else answers it (`/new` and
  `/reset` start the chat fresh without Hermes' banner);
- any other message from the owner in agent-control: start that chat fresh when it has
  been quiet for a while or its history has grown, then let the model answer;
- everything else, or any failure here: do nothing, and Hermes carries on as before.

It also keeps the model honest on Discord: a reply that says something was queued,
saved, parked or paused, in a turn where no Rove tool ran, is replaced (`on_reply`).

The plugin imports nothing from Rove or Hermes; Rove checks the owner and the channel
itself. `rove gateway install-shortcuts` copies this directory into the Hermes plugins
directory and writes `settings.json` next to it with the command to run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
ANSWER_TIMEOUT = 15.0  # seconds Rove may take to decide; after that the model answers
RUNNING: set = set()  # finished `rove shortcut` processes still posting their log line
IDLE_RESET = 15 * 60  # seconds of quiet after which the chat in agent-control starts fresh
MAX_PROMPT_TOKENS = 7000  # a longer history also starts fresh; the model has 16K in all


def load_settings() -> dict | None:
    try:
        settings = json.loads((HERE / "settings.json").read_text())
    except (OSError, ValueError):
        return None
    return settings if isinstance(settings, dict) and settings.get("command") else None


def platform_name(source) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "")


def message_of(event) -> dict | None:
    """The fields Rove needs from a Discord message, or None for anything else."""
    source = getattr(event, "source", None)
    if source is None or getattr(event, "internal", False) or platform_name(source) != "discord":
        return None
    text = getattr(event, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "id": str(getattr(event, "message_id", None) or getattr(source, "message_id", None) or ""),
        "content": text,
        "author_id": str(getattr(source, "user_id", None) or ""),
        "bot": bool(getattr(source, "is_bot", False)),
        "channel_id": str(getattr(source, "chat_id", None) or ""),
        "thread_id": getattr(source, "thread_id", None),
        "reply_to": getattr(event, "reply_to_message_id", None),
    }


async def ask_rove(message: dict, settings: dict) -> dict:
    """Run `rove shortcut` with the message on stdin and read its one-line decision.

    The process is not waited for: it posts its system-log line after printing the
    decision, and is reaped in the background.
    """
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        **{str(k): str(v) for k, v in (settings.get("env") or {}).items()},
    }
    process = await asyncio.create_subprocess_exec(
        *settings["command"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=settings.get("cwd") or None,
        env=env,
    )
    try:
        process.stdin.write(json.dumps(message).encode())
        await process.stdin.drain()
        process.stdin.close()
        line = await asyncio.wait_for(process.stdout.readline(), ANSWER_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
        raise
    reaper = asyncio.ensure_future(process.wait())
    RUNNING.add(reaper)
    reaper.add_done_callback(RUNNING.discard)
    decision = json.loads(line.decode() or "{}")
    return decision if isinstance(decision, dict) else {}


async def post(gateway, event, text: str) -> bool:
    adapter = (getattr(gateway, "adapters", None) or {}).get(event.source.platform)
    if adapter is None:
        logger.warning("rove_shortcuts: no adapter to answer on %s", platform_name(event.source))
        return False
    result = await adapter.send(event.source.chat_id, text)
    if not getattr(result, "success", False):
        logger.warning("rove_shortcuts: reply not delivered: %s", getattr(result, "error", None))
        return False
    return True


async def start_fresh(event, gateway):
    """Start the chat fresh through Hermes' own `/new` path, without its banner.

    `/new` returns a "Session reset" notice with the model and endpoint; it is a reply
    for the person who typed `/new`, so here it is dropped. Rove says its own short line
    when he asked for the reset himself.
    """
    await gateway._handle_reset_command(dataclasses.replace(event, text="/new"))


async def keep_short(event, gateway, session_store) -> bool:
    """Start the agent-control chat fresh after IDLE_RESET of quiet or a long history.

    Uses the same reset as `/new`. Every message there is a request of its own, and a
    long history only makes each answer slower. Returns whether it reset.
    """
    try:
        key = gateway._session_key_for_source(event.source)
        entry = session_store.lookup_by_session_key(key) if session_store is not None else None
        if entry is None:
            return False
        updated = entry.updated_at
        # Hermes keeps session times as naive local time.
        now = datetime.now(updated.tzinfo) if updated.tzinfo else datetime.now()
        quiet = (now - updated).total_seconds()
        if quiet < IDLE_RESET and (entry.last_prompt_tokens or 0) < MAX_PROMPT_TOKENS:
            return False
        await start_fresh(event, gateway)
        logger.info(
            "rove_shortcuts: agent-control chat started fresh (quiet %.0fs, last prompt %s tokens)",
            quiet,
            entry.last_prompt_tokens,
        )
        return True
    except Exception:  # the message still goes to the model
        logger.warning("rove_shortcuts: could not start the chat fresh", exc_info=True)
        return False


async def on_message(event, gateway=None, session_store=None, **_kwargs):
    message = message_of(event)
    settings = load_settings() if message else None
    if not settings:
        return None
    try:
        decision = await ask_rove(message, settings)
    except Exception:  # Rove could not decide: the model answers as before
        logger.warning("rove_shortcuts: rove shortcut failed", exc_info=True)
        return None
    if decision.get("handled"):
        reply = str(decision.get("reply") or "")
        if decision.get("reset") and gateway is not None:
            await start_fresh(event, gateway)
        if reply:
            await post(gateway, event, reply)
        # Handled: by this reply, or (for a paste the worker reached first) by the worker's.
        return {"action": "skip", "reason": "answered by rove"}
    if decision.get("control") and gateway is not None:
        await keep_short(event, gateway, session_store)
    return None


# --- no claimed action without a tool ------------------------------------------------
#
# Rove's tools write every "Queued", "Saved", "Parked" the owner reads. The model now and
# then writes one of those itself without calling the tool, which tells him something
# happened that did not. A Discord reply that claims such an action in a turn where no
# Rove tool ran is replaced by an honest line. This reads the model's words, never his.

ACTED: dict = {}  # (session id, turn id) -> a Rove tool ran in that turn
CLAIM = re.compile(
    r"\b(queued|saved|parked|paused|resumed|moved it up|going again)\b", re.IGNORECASE
)
NOT_DONE = "I haven't done that yet. Say it once more and I'll do it."


def on_tool(tool_name=None, session_id=None, turn_id=None, **_kwargs):
    if str(tool_name or "").startswith("mcp__rove__"):
        ACTED[(session_id, turn_id)] = True
        while len(ACTED) > 256:
            ACTED.pop(next(iter(ACTED)))


def on_reply(response_text=None, session_id=None, turn_id=None, platform=None, **_kwargs):
    if str(platform or "") != "discord":
        return None
    if ACTED.pop((session_id, turn_id), False):
        return None
    if CLAIM.search(str(response_text or "")):
        logger.warning("rove_shortcuts: the reply claimed an action no tool took; replaced")
        return NOT_DONE
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_message)
    ctx.register_hook("post_tool_call", on_tool)
    ctx.register_hook("transform_llm_output", on_reply)
