"""Erga's MCP server, one local process per application pass.

Inside a pass every Erga call (career evidence for the job-fit review and drafting, job
intake, render validation) goes through one stdio session instead of starting erga-mcp
for each call. The session lives on its own event-loop thread, so the worker and the
background resume preparation can both call it, at the same time.

Outside a pass (mail, submission confirmation, the Hermes tool server) each call starts
its own short-lived process, as before. Erga's stderr goes to a private log; the last
exception it names becomes the failure's cause, so a repeated failure is recognizable.
"""

import asyncio
import atexit
import os
import re
import threading
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .runtime import state_root

# The narrowest Erga profile that serves every tool Rove calls; it also shows the
# managed master records list_evidence needs.
PROFILE = "career-private"
TIMEOUTS = {"intake_job_url": 300.0}
DEFAULT_TIMEOUT = 120.0
OPEN_TIMEOUT = 60.0
LOG_LIMIT = 1024 * 1024
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))(?::\s*(.*))?$")
# Framework wrappers around the real error; their own text says nothing new.
_WRAPPERS = {"ToolError", "UnexpectedToolError", "McpError", "ExceptionGroup"}


class ErgaError(RuntimeError):
    """Erga could not complete a call. `cause` is the exception Erga logged, if any."""

    def __init__(self, tool: str, cause: str = ""):
        super().__init__("Erga could not complete this operation; inspect its private result")
        self.tool = tool
        self.cause = cause

    @property
    def signature(self) -> str:
        """What makes two failures 'the same': the tool and the error Erga raised."""
        return f"{self.tool} · {self.cause or 'no detail'}"[:240]


def parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=str(Path.home() / ".local/bin/erga-mcp"),
        env=dict(
            os.environ,
            ERGA_MCP_CONFIG=str(state_root() / "erga/config.toml"),
            ERGA_MCP_TOOL_PROFILE=PROFILE,
        ),
    )


def log_path() -> Path:
    return state_root() / "logs/erga-mcp.log"


def open_log():
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        if path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name(path.name + ".1"))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(descriptor, "a", buffering=1)


def log_offset() -> int:
    try:
        return log_path().stat().st_size
    except OSError:
        return 0


def cause_since(offset: int) -> str:
    """The last real exception Erga logged after `offset`, as `Type: message`."""
    try:
        with open(log_path(), "rb") as log:
            log.seek(offset)
            text = log.read(256 * 1024).decode("utf-8", "replace")
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        match = _EXCEPTION.match(line.strip().lstrip("|+- ").strip())
        if not match:
            continue
        name = match.group(1).rsplit(".", 1)[-1]
        if name not in _WRAPPERS:
            found = f"{name}: {match.group(2) or ''}".strip().rstrip(":")
    return " ".join(found.split())[:200]


def content_text(raw: dict) -> str:
    texts = [
        str(item.get("text", "")) for item in raw.get("content") or [] if isinstance(item, dict)
    ]
    return " ".join(" ".join(texts).split())[:200]


class Session:
    """One erga-mcp process and stdio session, served from a private event-loop thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="erga", daemon=True)
        self.session: ClientSession | None = None
        self.closing: asyncio.Event | None = None
        self.served: Future | None = None
        self.broken = False

    def open(self):
        self.thread.start()
        ready: Future = Future()
        self.served = asyncio.run_coroutine_threadsafe(self._serve(ready), self.loop)
        try:
            ready.result(OPEN_TIMEOUT)
        except BaseException:
            self.close()
            raise
        return self

    async def _serve(self, ready: Future):
        # The stdio and session contexts must open and close in this one task.
        self.closing = asyncio.Event()
        try:
            with open_log() as errlog:
                async with (
                    stdio_client(parameters(), errlog=errlog) as (reader, writer),
                    ClientSession(reader, writer) as session,
                ):
                    await session.initialize()
                    self.session = session
                    ready.set_result(None)
                    await self.closing.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            self.broken = True
            raise
        finally:
            self.session = None

    async def _call(self, name: str, arguments: dict) -> dict:
        if self.session is None:
            raise ConnectionError("Erga session is closed")
        result = await asyncio.wait_for(
            self.session.call_tool(name, arguments), TIMEOUTS.get(name, DEFAULT_TIMEOUT)
        )
        return result.model_dump(by_alias=True)

    def submit(self, name: str, arguments: dict) -> Future:
        return asyncio.run_coroutine_threadsafe(self._call(name, arguments), self.loop)

    def close(self):
        if self.closing is not None:
            self.loop.call_soon_threadsafe(self.closing.set)
        if self.served is not None:
            with suppress(BaseException):
                self.served.result(30)
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        if not self.thread.is_alive():
            self.loop.close()


_lock = threading.Lock()
_shared: Session | None = None
_sharing = False


def begin_pass():
    """Erga calls from here on share one process until the worker process exits."""
    global _sharing
    _sharing = True


def end_pass():
    global _shared, _sharing
    with _lock:
        session, _shared, _sharing = _shared, None, False
    if session is not None:
        session.close()


atexit.register(end_pass)


def shared() -> Session | None:
    """The pass's session, opened on first use and reopened if Erga went away."""
    global _shared
    if not _sharing:
        return None
    with _lock:
        if _shared is not None and (_shared.broken or _shared.session is None):
            stale, _shared = _shared, None
            stale.close()
        if _shared is None:
            _shared = Session().open()
        return _shared


async def one_off(name: str, arguments: dict) -> dict:
    with open_log() as errlog:
        async with (
            stdio_client(parameters(), errlog=errlog) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            result = await asyncio.wait_for(
                session.call_tool(name, arguments), TIMEOUTS.get(name, DEFAULT_TIMEOUT)
            )
    return result.model_dump(by_alias=True)


async def call(name: str, arguments: dict) -> dict:
    """Erga's structured result for one tool call; ErgaError when Erga could not do it."""
    offset = log_offset()
    session = None
    try:
        session = await asyncio.to_thread(shared)
        if session is not None:
            raw = await asyncio.wrap_future(session.submit(name, arguments))
        else:
            raw = await one_off(name, arguments)
    except Exception as error:  # a crash, a timeout or a closed pipe: Erga did not answer
        if session is not None:
            session.broken = True
        raise ErgaError(name, cause_since(offset) or type(error).__name__) from error
    if raw.get("isError"):
        # Full upstream details stay in the private log, never in public output.
        raise ErgaError(name, cause_since(offset) or content_text(raw))
    return raw.get("structuredContent", {})
