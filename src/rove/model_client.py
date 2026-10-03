"""The worker's structured prompts as one HTTP request to the local model server.

Fit reviews, answer drafts, the writing cleanup, the pop-up choice and the mail label go
straight to the server's OpenAI-compatible endpoint: one streamed POST carrying the system
prompt and the context, temperature 0, and thinking switched off twice (the chat
template's `enable_thinking` and `/no_think` in the user turn). A model that starts to
reason anyway is stopped at its first words, not after a hundred seconds of it. Hermes
stays the Discord agent harness; `model_transport: "hermes"` in the workflow config sends
these prompts through it instead. The evidence for the split is in docs/local-runtime.md.

Every failure of the server itself (down, a 5xx, a dropped stream, a timeout) is
`ModelUnavailable`: the work waits for the next tick instead of failing an application.
"""

import json
import math
import os
import re
import time

import httpx

from . import runtime

# The server holds 16,384 tokens and answers with at most 2,048. A prompt is kept under
# PROMPT_TOKEN_BUDGET by an estimate that errs high (JSON-heavy text runs about 2.8
# characters a token), so the real count stays clear of the window.
MAX_OUTPUT_TOKENS = 2048
PROMPT_TOKEN_BUDGET = 12_500
CHARS_PER_TOKEN = 2.8
# oMLX reuses a prompt prefix only in whole blocks of this many tokens.
CACHE_BLOCK_TOKENS = 2048
# A low estimate of the same text, used to be sure a static prefix fills a whole block.
CHARS_PER_TOKEN_HIGH = 4.2
# The model unloads after 600 idle seconds; a ping a little before that keeps it loaded.
KEEPALIVE_SECONDS = 480
NO_THINK = "/no_think"


class ModelUnavailable(RuntimeError):
    """The local model server is down or failed; the queue waits instead of failing."""


class ThinkingLeak(RuntimeError):
    """The model reasoned instead of answering: thinking is not switched off."""


class PromptTooLong(ValueError):
    """A prompt over the budget; it is never sent."""


def estimate_tokens(text: str) -> int:
    """Tokens a text may take, erring high."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def at_least_tokens(text: str) -> int:
    """Tokens a text takes at the least, erring low."""
    return int(len(text) / CHARS_PER_TOKEN_HIGH)


def user_content(context: dict) -> str:
    """The user turn: the context as JSON in its own key order, then the second switch.

    Key order is the cache's business: the static keys come first so the same leading
    blocks repeat from job to job. Non-ASCII text stays as written, which costs fewer
    tokens than escapes.
    """
    return json.dumps(context, ensure_ascii=False) + "\n\n" + NO_THINK


def prompt_tokens(system: str, context: dict) -> int:
    return estimate_tokens(system) + estimate_tokens(user_content(context))


# How reasoning prose begins when it lands in the answer itself.
REASONING_START = re.compile(
    r"^(?:<think>|okay[,.! ]|alright[,.! ]|hmm[,.! ]|let me |let's |we need to |i need to "
    r"|the user (?:wants|asks|is asking)|first, (?:i|let)|looking at )",
    re.IGNORECASE,
)


def reasoning_start(text: str, expects_json: bool) -> bool:
    """Whether an answer opens with reasoning instead of the answer itself.

    A JSON answer must open with `{`, `[` or a code fence; anything else is the model
    talking to itself (or a preamble the parser would refuse anyway). Prose is judged by
    its first words.
    """
    start = text.lstrip()
    if not start:
        return False
    if expects_json:
        return start[0] not in "{[`"
    return bool(REASONING_START.match(start))


def failure_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str((body or {}).get("detail") or body)[:200]


def refuse(response: httpx.Response):
    detail = failure_detail(response)
    if response.status_code >= 500:
        raise ModelUnavailable(f"Model server error {response.status_code}: {detail}")
    too_long = re.search(r"too long|context|max.*tokens", detail, re.IGNORECASE)
    if response.status_code == 400 and too_long:
        raise PromptTooLong(f"The model server refused the prompt as too long: {detail}")
    raise RuntimeError(f"Model server refused the request ({response.status_code}): {detail}")


def chat(
    system: str,
    user: str,
    *,
    expects_json: bool = True,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> dict:
    """One answer: `content`, `finish_reason`, `usage` (the server's own counts), `seconds`.

    Streamed so a reasoning start is caught at once: the request is dropped (the server
    cancels it on disconnect) and ThinkingLeak raised. The answer is only returned whole.
    """
    payload = {
        "model": runtime.MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.perf_counter()
    parts: list[str] = []
    usage: dict = {}
    finish = None
    judged = False
    try:
        with (
            runtime.client() as client,
            client.stream("POST", "/chat/completions", json=payload) as response,
        ):
            if response.status_code >= 400:
                response.read()
                refuse(response)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue  # keepalive comments and blank lines
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    detail = chunk["error"]
                    detail = detail.get("message", detail) if isinstance(detail, dict) else detail
                    raise ModelUnavailable(f"Model server failed mid-answer: {str(detail)[:200]}")
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content") or delta.get("reasoning"):
                        raise ThinkingLeak("Qwen started reasoning instead of answering")
                    if delta.get("content"):
                        parts.append(delta["content"])
                        if not judged:
                            text = "".join(parts).lstrip()
                            # Enough to judge: one character of JSON, a few words of prose.
                            if text and (expects_json or len(text) >= 48):
                                judged = True
                                if reasoning_start(text, expects_json):
                                    raise ThinkingLeak(
                                        "Qwen began with reasoning instead of the answer"
                                    )
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
    except httpx.TimeoutException as error:
        raise ModelUnavailable("The model server did not answer in time") from error
    except httpx.TransportError as error:
        raise ModelUnavailable(f"The model server connection failed: {error}") from error
    except json.JSONDecodeError as error:
        raise ModelUnavailable("The model server sent an unreadable stream") from error
    text = "".join(parts).strip()
    details = usage.get("completion_tokens_details") or {}
    if (details.get("reasoning_tokens") or 0) > 0:
        raise ThinkingLeak("The model server reports reasoning tokens; thinking is on")
    if reasoning_start(text, expects_json):
        raise ThinkingLeak("Qwen began with reasoning instead of the answer")
    if finish is None:
        raise ModelUnavailable("The model server stream ended before the answer finished")
    return {
        "content": text,
        "finish_reason": finish,
        "usage": usage,
        "seconds": round(time.perf_counter() - started, 3),
    }


def server_status() -> dict | None:
    """oMLX's own status (`active_requests`, `waiting_requests`), or None when unreachable."""
    url = runtime.BASE_URL.rsplit("/v1", 1)[0] + "/api/status"
    try:
        with runtime.client() as client:
            response = client.get(url, timeout=3)
        return response.json() if response.is_success else None
    except (httpx.HTTPError, ValueError):
        return None


def busy() -> bool:
    """Whether the server is serving or queueing a request (anyone's), or cannot tell."""
    status = server_status()
    if not isinstance(status, dict):
        return True
    return int(status.get("active_requests") or 0) + int(status.get("waiting_requests") or 0) > 0


def last_use_path():
    return runtime.state_root() / "model-last-use"


def mark_used():
    """Every request from any Rove process leaves its time here, for the keepalive."""
    try:
        path = last_use_path()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.utime(path)
    except OSError:
        pass


def idle_seconds() -> float:
    try:
        return max(time.time() - last_use_path().stat().st_mtime, 0.0)
    except OSError:
        return math.inf


def ping() -> bool:
    """A one-token request: the server counts it as use and keeps the weights loaded."""
    payload = {
        "model": runtime.MODEL,
        "messages": [{"role": "user", "content": "ok " + NO_THINK}],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        with runtime.client() as client:
            response = client.post("/chat/completions", json=payload, timeout=120)
    except httpx.HTTPError:
        return False
    mark_used()
    return response.is_success
