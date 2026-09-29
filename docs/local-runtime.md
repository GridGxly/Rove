# Local runtime

The local reasoning stack and synthetic prepare-only workflow are operational on the reference Mac. [Real onboarding and Keryx intake](onboarding-and-jobs.md) extend it with bounded discovery and draft-profile tools. This is not a production ATS submission implementation. The production tool boundary excludes shell execution, profile approval, and submission. The optional [application workflow](application-workflow.md) adds public job navigation and visible preparation.

See [Runtime measurements](runtime-benchmarks.md) for measured throughput, cache behavior, full-stack memory pressure and acceptance results.

## Verified components

| Component | Tested build |
| --- | --- |
| oMLX | 0.6.4, official signed macOS app |
| MLX | 0.32.0, bundled with oMLX; Metal inference verified |
| Qwen | `orcarouter/Qwen3.8-27B-Uncensored-MLX` at `14963e70f886455cf93090ac95bdbf4c8730cbe1` |
| Hermes | 0.21.5+4343.g226eeeb, source commit `226eeeb4c21ca6d9fb3880bf6aa3b9093f69530a` |
| Erga | 0.1.0 at `c4164558d7ec450e893ddd5f793b2d870eab782c` |
| QMD | 2.8.3, scoped local npm installation |
| Obsidian | 1.13.7, existing private vault |
| Tectonic | 0.17.0, local resume compilation |
| Autopilot | Python 3.12, dependencies pinned in `uv.lock` |

The model's root weights are MLX affine 4-bit, group size 64, totaling 16,054,541,599 bytes. Vision, normalization and convolution components retain source precision. The separate MTP head is 849,400,337 bytes; the complete selected download with metadata is 16,950,465,457 bytes. The base is `Qwen/Qwen3.8-27B`; OrcaRouter published the converted/abliterated checkpoint under Apache-2.0, updated 2026-08-27. `trust_remote_code` stays false. Model weights are not modified.

Sources: [model](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX), [oMLX release](https://github.com/jundot/omlx/releases/tag/v0.6.4), [Hermes installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation), [Erga](https://github.com/Adr1an04/erga-mcp), [QMD](https://github.com/tobi/qmd).

## oMLX settings

Private settings live in `~/.omlx/settings.json` and `~/.omlx/model_settings.json`; weights live under `~/.omlx/models`, outside Git.

- Authenticated API at `http://127.0.0.1:8000/v1`, never a LAN listener.
- Served model name `Qwen3.8-27B-Uncensored-4bit`.
- 16,384 total context tokens, maximum 2,048 output tokens, one active request.
- Streaming, thinking enabled by default; benchmarks can explicitly disable thinking.
- Model pinning off, idle TTL 600 seconds, server auto-start on app launch off.
- Safe memory guard; Apple's default Metal limit remains unchanged.
- Prefix/SSD caching enabled, 1GB hot cache and 20GB disk budget.
- External VLM MTP enabled with the downloaded `mtp` folder and block size 4. Logs confirm drafter attachment, draft acceptance and emitted tokens.

The checkpoint does not include embedded `mtp.*` tensors. The correct path is `vlm_mtp_enabled` with an external `qwen3_5_mtp` drafter, not the native `mtp_enabled` flag. TurboQuant KV compression and this VLM MTP path are mutually exclusive in oMLX 0.6.4. Grammar-constrained requests can fall back to ordinary decoding. Tool arguments, JSON output and reasoning were checked with the selected configuration.

The isolated 16K-budget comparison produced 512 tokens at 21.96 tokens/second with MTP versus 13.90 first and 14.20 repeated with 8-bit TurboQuant KV. The warm TTFTs were 6.74 and 8.33 seconds respectively. Logs verified actual 8-bit conversion of 15 cache layers. MTP remains selected; the benchmark restored TurboQuant to disabled.

A green memory graph after a request does not prove pressure stayed green during inference. Record pressure, swap growth and responsiveness over the whole run. High Power mode and fan activity alone do not establish the cause of a memory-pressure change.

A temporary 30-second idle TTL test confirmed an actual model unload that freed 17.87GB. The normal 600-second TTL was restored afterward; the next inference request reloads the weights.

## Hermes integration and compatibility patch

Hermes uses `model.provider: custom`, `model.base_url: http://127.0.0.1:8000/v1`, `model.default: Qwen3.8-27B-Uncensored-4bit`, and `model.context_length: 16384`. Its custom-provider request and stale timeouts are 900 seconds. Credentials live in private local configuration and are never passed in process arguments. External-login adoption and telemetry sharing are disabled. Compression uses the same local model; no cloud fallback is configured.

**The pinned Hermes build normally requires a 64K model context.** The narrowly scoped runtime uses an experimental, opt-in patch to that capacity heuristic:

```sh
uv run python scripts/hermes_small_context.py /path/to/hermes-agent
```

Enable `HERMES_AUTOPILOT_16K=1` only for this certified local configuration. The script backs up the original file, is idempotent, and refuses an unrecognized upstream constant. An update can overwrite the patch: review and retest it after updating Hermes. The server still advertises and enforces its real 16K limit; it does not pretend to offer 64K. This patch does not change tool authorization.

The MCP server command is the absolute path to this checkout's `.venv/bin/autopilot`, with argument `mcp` and the checkout as its working directory. The historical certification allowlist was:

- `prepare_synthetic_application`
- `read_synthetic_evidence`
- `retrieve_synthetic_memory`
- `save_synthetic_answer_draft`

The production onboarding/job-review allowlist now uses the nine real tools in
[Onboarding and jobs](onboarding-and-jobs.md#hermes-connection), replacing those four
fixture tools. Disable every built-in Hermes toolset for this profile; set each
platform's toolsets to `mcp-erga-autopilot`. Set `tools.tool_search.enabled: off` so
the small schemas are present directly. Within the MCP server's `tools` config, set
`resources: false` and `prompts: false`; a name exclusion alone does not suppress
Hermes' generated wrappers. The tools discovered globally by Hermes are not necessarily
the tools exposed to an individual agent; verify the agent's actual schema list.

The Discord integration authorizes one numeric owner ID and the configured `agent-control` channel. It does not accept other bots or backfill channel history. Discord Message Content Intent is required. The lightweight Hermes launchd gateway starts at login and can restart after a crash; this does not load the 27B model at login. oMLX starts on demand. The upstream macOS service generator currently always writes `RunAtLoad`; manually editing that generated plist is not a durable way to change startup behavior.

## Erga and QMD

Erga is installed in its own managed tool environment. The adapter calls its supported MCP interface using an isolated synthetic configuration under the private runtime root, with `ERGA_MCP_TOOL_PROFILE=read`. It selects an approved synthetic evidence source and cannot select another database or write Erga facts.

Real career evidence uses separate private Erga state and the bounded
`read_career_evidence` adapter described in the onboarding guide. Managed masters
are withheld by upstream's `read` profile, so that trusted adapter internally uses
`career-private` while exposing only approved read excerpts to Hermes.

Resume generation and validation use Erga's supported CLI and Tectonic. The fixture master contains only synthetic education and project facts. Erga may correctly return `meaningful_change: false` when no supported tailoring is available. Check `validation.passed`, not merely the CLI exit code: a failed layout validation can still return exit code zero. `resume tailor` appends section content; repeating an existing project through that command duplicates it. The verified fixture uses `resume tailor-job --job-text` and a local, validated artifact instead.

The pinned `resume tailor-job --validate` CLI path does not forward the configured
multi-line bullet settings to its render validator. For a master allowing two-line
bullets, use Erga's supported `validate_tailored_resume` MCP operation on a private
package artifact. That path forwards both settings and still enforces page count,
page fill and stranded-tail checks. Inspect its `returncode`, `pdf` and `skipped`
fields; a successful MCP transport alone is not a passing render. A base-master
validation is separate from tailoring quality or application approval.

A reviewed fixture PDF can be copied to `synthetic/erga-synthetic-resume.pdf` under the private runtime root, with its SHA-256 in `synthetic/resume-manifest.json`. The manifest cannot choose another filename. The browser fails closed if those PDF bytes change. Without a provisioned manifest, the standalone smoke test creates a synthetic text resume.

QMD is installed under `~/.local/share/erga-autopilot/qmd`. Certification uses the
isolated `erga-autopilot` index and `autopilot-synthetic` collection. Real approved
profile retrieval uses the separate `erga-candidate` index and `approved-profile`
collection, rebuilt with `autopilot memory index`. The Node installation works with
packaged SQLite/extension support, so a separate Homebrew SQLite installation was
unnecessary on the tested machine.

QMD helper models are embeddinggemma-300M Q8_0, qmd-query-expansion-1.7B Q4_K_M and qwen3-reranker-0.6B Q8_0, about 2.25GB in total. These GGUF helpers belong to QMD; the main Qwen model remains MLX. The adapter uses one short-lived process per query, never a resident QMD model server. Keyword search is the tool's default; semantic retrieval is also tested through the adapter. CPU offloading is forced for the helper process because the packaged Metal helper emitted compilation warnings on this Mac. Cached queries can be much faster than first use. An empty semantic result is a legitimate threshold result, not permission to invent evidence.

Retrieved snippets are explicitly untrusted and cannot approve facts, credentials, uploads, or submission. A high-ranked injection note remains untrusted. No vault-wide content is loaded into every prompt.

## Operations

From the checkout:

```sh
uv sync
uv run playwright install chromium
uv run autopilot start
uv run autopilot status
uv run autopilot stop
```

`start` starts the official oMLX app-managed service, waits for its model inventory, and starts the Hermes gateway. Weights load on the first inference request. `stop` drains/stops Hermes before stopping oMLX.

Individual components:

```sh
uv run autopilot model start
uv run autopilot model stop
uv run autopilot model restart
uv run autopilot model status
uv run autopilot model logs
uv run autopilot gateway start
uv run autopilot gateway stop
uv run autopilot gateway restart
uv run autopilot gateway status
```

Hermes starts the narrow Autopilot MCP process as needed. `uv run autopilot mcp` is the foreground MCP entry point; it expects a stdio client. `uv run autopilot smoke` runs the visible browser fixture directly.

For overlapping memory measurements, `uv run autopilot smoke --hold-seconds 60` keeps the prepared browser open before closing it. The CLI accepts 0–120 seconds and reports the hold separately from mechanical work. The MCP tool uses the default zero-second hold.

## Verification and benchmarks

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/check_staged.py
uv run autopilot smoke
uv run autopilot benchmark /private/synthetic-4k.json /private/synthetic-8k.json /private/synthetic-16k.json
```

Prompt files contain arrays of OpenAI-format messages. Keep raw requests, output, measurements and screenshots outside Git. The 16K test must reserve space for generation; 15,872 input tokens plus 512 output tokens reaches 16,384.

`scripts/benchmark_hermes.py` runs in the pinned Hermes managed Python environment with `--hermes-checkout /path/to/hermes-agent`. It records the actual request and replays the identical payload directly. Actual tool names, token counts, cache hits, API-call count, streaming timings and memory samples are saved privately. The direct replay is normally warmer: compare cache counts and queue activity before attributing a TTFT difference to Hermes. Each request uses the same model and decoding parameters. The measurement instrumentation exists only inside the benchmark process.

With incoming gateway work paused, `uv run python scripts/benchmark_kv.py /private/synthetic-16k.json` compares the current MTP path and 8-bit TurboQuant KV, then restores the original settings even on failure. Resume the gateway afterward.

The browser fixture verifies ten known fields, a frozen upload, unknown-sensitive and writing holds, post-batch dynamic-field detection, and zero submissions. SQLite deduplicates applications, records runs/checkpoints, and completes run/application state atomically. Submission attempts and forged submitted/approved states are rejected. Tests also cover changed resume bytes, hostile labels, unapproved dynamic answers, and external-network denial.

Synthetic Discord certification used three Qwen calls and all four certification MCP tools. Routine field filling used zero model calls. The answer remained an unapproved draft. This verifies the local stack, not real ATS accounts, CAPTCHA/MFA handling, unattended submission, or optional Zoho mail integration. Real candidate onboarding is covered separately in [Onboarding and jobs](onboarding-and-jobs.md).

## Application preparation extension

See [Application workflow](application-workflow.md) for the visible browser, feed
service, durable queue, forum archive and bounded Qwen/Hermes answer proposals.
These replace the earlier discovery-only scope when explicitly configured.
