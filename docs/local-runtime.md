# Local runtime

The first runtime is under certification. A successful install or HTTP response is not an end-to-end acceptance result.

The implemented `uv run autopilot smoke` fixture runs visible Chromium in a separate persistent profile, observes fields once, fills ten approved synthetic facts, verifies a frozen resume hash, and leaves work authorization and the writing question pending. It records a checkpoint in private SQLite and never submits. The production MCP server exposes only synthetic preparation, approved fixture evidence, and unapproved answer drafts. Real ATS navigation and submission are not implemented by these tools.

## Pinned components

- oMLX 0.6.4 official signed macOS release, with bundled MLX 0.32.0 and native Metal kernels.
- `orcarouter/Qwen3.8-27B-Uncensored-MLX`, revision `14963e70f886455cf93090ac95bdbf4c8730cbe1`.
- Root weights: MLX affine 4-bit, group size 64, 16,054,541,599 bytes. Vision/norm/conv components retain their source precision.
- Base: `Qwen/Qwen3.8-27B`; converter/abliteration publisher: OrcaRouter. Apache-2.0. Model last updated 2026-08-27.
- The bundled Qwen tokenizer/chat template supports thinking and tools. These capabilities still require local runtime tests.
- A separate MTP head is available. Acceleration stays disabled until a comparison proves it works with this backend and tool calls.

The similarly named OptiQ 4bit model uses substantial 8-bit mixed precision and a different registration path. The uniform 4-bit language-weight build above matches the requested memory budget more directly.

Sources: [model card](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX), [oMLX release](https://github.com/jundot/omlx/releases/tag/v0.6.4), [Hermes installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation).

## Local configuration

oMLX stores private settings in `~/.omlx/settings.json`. Weights live under `~/.omlx/models`, outside Git. Initial settings are loopback `127.0.0.1:8000`, API authentication, 16,384 total context tokens, one active request, 600-second model idle TTL, 1GB hot cache and 20GB SSD cache. The server does not automatically start on app launch. Model pinning is off.

Hermes uses `model.provider: custom`, `model.base_url: http://127.0.0.1:8000/v1`, `model.default: Qwen3.8-27B-Uncensored-4bit`, `model.context_length: 16384`, and streaming. Its API credential is private. External-login adoption and telemetry sharing are disabled. This does not install or select Hermes' llama.cpp runtime.

## Commands

From the checkout, run `uv sync` once. The following commands operate the official oMLX app-managed service:

```sh
uv run autopilot model start
uv run autopilot model stop
uv run autopilot model restart
uv run autopilot model status
uv run autopilot model logs
```

Benchmark prompt files are arrays of OpenAI-format messages. Keep real prompt contents and results outside Git:

```sh
uv run autopilot benchmark /path/to/synthetic-4k.json /path/to/synthetic-8k.json /path/to/synthetic-16k.json
```

The benchmark records streaming TTFT, API token counts and server timings, observed sustained decode rate, latency, and sampled macOS counters. Null values mean unmeasured. `psutil_used_bytes` is not Activity Monitor's Memory Used; the report must distinguish them. GPU utilization is system-wide, not exclusive to inference. Cache hits are checked against API-reported cached token counts; the first/repeat labels alone do not establish cache behavior.

Do not connect real applicant data until the prepare-only browser and authority tests pass. Discord credentials stay in private local configuration, never in examples or reports.
