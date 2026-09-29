"""Measure the real Hermes wire request, then replay that identical payload directly.

Run with the pinned Hermes managed Python, passing --hermes-checkout. Results and
synthetic request payloads remain in the private Autopilot state directory.
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-checkout", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HERMES_AUTOPILOT_16K"] = "1"
    sys.path.insert(0, str(args.hermes_checkout))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import hermes_bootstrap  # noqa: F401
    import httpx
    from hermes_cli.config import load_config
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import discover_mcp_tools

    from erga_autopilot.benchmark import request
    from erga_autopilot.metrics import memory_snapshot
    from erga_autopilot.runtime import api_key, state_root, write_private

    config = load_config()
    discover_mcp_tools()
    results = []
    # The pinned tool/system prompt occupies roughly 3,800 tokens; each fixture
    # line is 28 tokens. Record actual server counts rather than labeling them exact.
    for label, lines in [("4k-class", 11), ("8k-class", 156), ("16k-class", 420)]:
        wire = []
        agent = AIAgent(
            model=config["model"]["default"],
            provider="custom",
            base_url=config["model"]["base_url"],
            api_key=api_key(),
            enabled_toolsets=["mcp-erga-autopilot"],
            disabled_toolsets=config["agent"]["disabled_toolsets"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            max_tokens=512,
            max_iterations=2,
            stream_delta_callback=lambda _text: None,
            request_overrides={
                "temperature": 0,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            },
        )
        names = [x["function"]["name"] for x in agent.tools]
        assert names and all(n.startswith("mcp__erga_autopilot__") for n in names), names

        def capture_request(req, wire=wire):
            if req.url.path != "/v1/chat/completions":
                return
            assert req.url.host == "127.0.0.1" and req.url.port == 8000
            wire.append(
                {
                    "payload": json.loads(req.content),
                    "started": time.perf_counter(),
                    "first": None,
                    "last": None,
                    "usage": {},
                }
            )

        class Tee(httpx.SyncByteStream):
            def __init__(self, inner, record):
                self.inner, self.record, self.buffer = inner, record, b""

            def __iter__(self):
                for chunk in self.inner:
                    self.buffer += chunk
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        if line.startswith(b"data: ") and line[6:].strip() != b"[DONE]":
                            event = json.loads(line[6:])
                            if event.get("usage"):
                                self.record["usage"] = event["usage"]
                            for choice in event.get("choices", []):
                                if any(
                                    choice.get("delta", {}).get(k)
                                    for k in ("content", "reasoning_content", "tool_calls")
                                ):
                                    now = time.perf_counter()
                                    self.record["first"] = self.record["first"] or now
                                    self.record["last"] = now
                    yield chunk

            def close(self):
                self.inner.close()

        def capture_response(response, wire=wire, Tee=Tee):
            if response.request.url.path == "/v1/chat/completions" and wire:
                if wire[-1]["payload"].get("stream"):
                    response.stream = Tee(response.stream, wire[-1])
                else:
                    response.read()
                    wire[-1]["usage"] = response.json().get("usage", {})

        # Hermes clones an HTTP client for streaming attempts, so hooks attached
        # only to agent.client miss the actual request. Instrument this benchmark
        # process's send boundary and restore it immediately after the turn.
        original_send = httpx.Client.send

        def measured_send(http_client, req, *args, original_send=original_send, **kwargs):
            capture_request(req)
            response = original_send(http_client, req, *args, **kwargs)
            capture_response(response)
            return response

        prompt = (
            "Synthetic comparison "
            + label
            + ". Do not use any tools.\n"
            + "Example Labs builds local Python and SQLite tools. Alex Example tested reliable "
            "imports and documented recoverable workflow states. All facts here are synthetic.\n"
            * lines
            + "Write exactly 12 numbered recommendations for reliable application preparation. "
            "Each must contain 15 to 18 words. Stop after recommendation 12."
        )
        samples, stop = [memory_snapshot()], threading.Event()

        def monitor(stop=stop, samples=samples):
            while not stop.wait(2):
                samples.append(memory_snapshot())

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        start = time.perf_counter()
        httpx.Client.send = measured_send
        try:
            answer = agent.run_conversation(prompt)
        finally:
            httpx.Client.send = original_send
            stop.set()
            thread.join(timeout=5)
        elapsed = time.perf_counter() - start
        samples.append(memory_snapshot())
        write_private(state_root() / f"benchmarks/hermes-{label}-wire.json", {"requests": wire})
        if not wire or not wire[0]["usage"]:
            raise RuntimeError("Hermes did not provide measurable streaming usage")
        item = wire[0].copy()
        item.update(
            label=label,
            hermes_total_seconds=elapsed,
            hermes_ttft_seconds=item["first"] - start if item["first"] else None,
            tool_names=names,
            memory_samples=samples,
            completed=answer.get("completed"),
            api_calls=len(wire),
            request_history=wire,
        )
        item["direct_replay"] = request(item["payload"]["messages"], extra=item["payload"])
        results.append(item)
        write_private(state_root() / "benchmarks/hermes-comparison.json", {"results": results})
        print(
            json.dumps(
                {
                    "label": label,
                    "hermes_seconds": elapsed,
                    "hermes_ttft": item["hermes_ttft_seconds"],
                    "usage": item["usage"],
                    "direct_usage": item["direct_replay"]["usage"],
                }
            ),
            flush=True,
        )
        agent.close()


if __name__ == "__main__":
    main()
