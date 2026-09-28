"""Repeatable streaming benchmark with raw timings and OS snapshots."""

import json
import threading
import time
from pathlib import Path

from .metrics import memory_snapshot
from .runtime import MODEL, client, state_root, write_private


def request(messages: list, *, max_tokens=512, thinking=False, extra=None) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    payload.update(extra or {})
    started = time.perf_counter()
    first = None
    last = None
    text, reasoning, calls = [], [], {}
    usage = {}
    samples = [memory_snapshot()]
    stop = threading.Event()

    def monitor():
        while not stop.wait(2):
            samples.append(memory_snapshot())

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        with client() as c, c.stream("POST", "/chat/completions", json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                d = json.loads(line[6:])
                if d.get("usage"):
                    usage = d["usage"]
                for choice in d.get("choices", []):
                    delta = choice.get("delta", {})
                    if (
                        delta.get("content")
                        or delta.get("reasoning_content")
                        or delta.get("tool_calls")
                    ):
                        now = time.perf_counter()
                        first = first or now
                        last = now
                    if delta.get("content"):
                        text.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    for call in delta.get("tool_calls", []):
                        item = calls.setdefault(
                            call.get("index", 0), {"id": "", "name": "", "arguments": ""}
                        )
                        item["id"] = call.get("id") or item["id"]
                        f = call.get("function", {})
                        item["name"] += f.get("name", "")
                        item["arguments"] += f.get("arguments", "")
    finally:
        stop.set()
        thread.join(timeout=5)
    elapsed = time.perf_counter() - started
    samples.append(memory_snapshot())
    generated = usage.get("completion_tokens")
    return {
        "model": MODEL,
        "ttft_seconds": first - started if first else None,
        "latency_seconds": elapsed,
        "usage": usage,
        "observed_decode_tokens_per_second": (generated - 1) / (last - first)
        if generated and first and last and last > first
        else None,
        "text": "".join(text),
        "reasoning": "".join(reasoning),
        "tool_calls": list(calls.values()),
        "memory_samples": samples,
        "parameters": {"max_tokens": max_tokens, "thinking": thinking},
    }


def run_suite(prompt_files: list[Path], output: Path | None = None) -> Path:
    results = []
    output = output or state_root() / "benchmarks/direct.json"
    for path in prompt_files:
        for state in ["first", "repeat"]:
            result = request(json.loads(path.read_text()), max_tokens=512)
            result.update({"prompt_file": path.name, "cache_trial": state})
            cached = result["usage"].get("prompt_tokens_details", {}).get("cached_tokens")
            result["cache_observed"] = "hit" if cached else "miss" if cached == 0 else "unreported"
            results.append(result)
            write_private(output, {"results": results})
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in [
                            "prompt_file",
                            "cache_trial",
                            "ttft_seconds",
                            "latency_seconds",
                            "usage",
                            "observed_decode_tokens_per_second",
                        ]
                    }
                ),
                flush=True,
            )
    return output
