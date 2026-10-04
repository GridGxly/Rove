"""Rove shortcuts for the Hermes gateway.

Hermes calls `on_message` for every incoming message before it authorizes the sender or
starts a model turn (the `pre_gateway_dispatch` hook). This plugin hands each Discord
message to Rove's own code (`rove shortcut`, run with the repository's Python) and does
what Rove decides:

- a fixed request from the owner in agent-control (status, what's waiting, sends today,
  pause, resume, help, a pasted job link, `first`, why did you skip a company): post
  Rove's line in the channel and tell Hermes the message is handled, so no model turn
  runs and nothing else answers it;
- any other message from the owner in agent-control: start that chat fresh when it has
  been quiet for a while or its history has grown, then let the model answer;
- everything else, or any failure here: do nothing, and Hermes carries on as before.

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
        await gateway._handle_reset_command(dataclasses.replace(event, text="/new"))
        logger.info(
            "rove_shortcuts: agent-control chat started fresh (quiet %.0fs, last prompt %s tokens)",
            quiet,
            entry.last_prompt_tokens,
        )
        return True
    except Exception:  # the message still goes to the model
        logger.warning("rove_shortcuts: could not start the chat fresh", exc_info=True)
        return False


async def on_message(event, gateway=None, session_store=None, **kwargs):
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
        if reply:
            await post(gateway, event, reply)
        # Handled: by this reply, or (for a paste the worker reached first) by the worker's.
        return {"action": "skip", "reason": "answered by rove"}
    if decision.get("control") and gateway is not None:
        await keep_short(event, gateway, session_store)
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_message)
