# Local runtime

This page records the builds and settings of the local reasoning stack as tested on the reference Mac, and the commands that verify it. The workflow that runs on top of it is described in [Application workflow](application-workflow.md). Measured throughput, cache behavior and memory pressure are in [Runtime measurements](runtime-benchmarks.md).

## Verified components

| Component | Tested build |
| --- | --- |
| oMLX | 0.6.4, official signed macOS app |
| MLX | 0.32.0, bundled with oMLX; Metal inference verified |
| Qwen | `orcarouter/Qwen3.8-27B-Uncensored-MLX` at `14963e70f886455cf93090ac95bdbf4c8730cbe1` |
| Hermes | 0.21.5+4343.g226eeeb, source commit `226eeeb4c21ca6d9fb3880bf6aa3b9093f69530a` |
| Erga | 0.1.0 from the [GridGxly/erga-mcp](https://github.com/GridGxly/erga-mcp) fork, branch `fetch-headers-and-job-text` at `f1de320514da9eaa3b7f3390ad3f8f3fc6e7939e` (upstream `c4164558d7ec450e893ddd5f793b2d870eab782c` plus one commit) |
| QMD | 2.8.3, scoped local npm installation |
| Obsidian | 1.13.7, existing private vault |
| Tectonic | 0.17.0, local resume compilation |
| Rove | Python 3.12, dependencies pinned in `uv.lock` |
| Patchright | 1.63, drives the daemon-launched Rove Browser over local CDP; its own Chromium build runs the tests and the synthetic fixture |
| Rove Browser | built by `rove browser install` from Google Chrome 154.0.8037.97; fingerprint parity with that Chrome and the public bot-detection pages checked on that build |

The model's root weights are MLX affine 4-bit, group size 64, totaling 16,054,541,599 bytes. Vision, normalization and convolution components retain source precision. The separate MTP head is 849,400,337 bytes, and the complete selected download with metadata is 16,950,465,457 bytes. The base is `Qwen/Qwen3.8-27B`. OrcaRouter published the converted, abliterated checkpoint under Apache-2.0, updated 2026-08-27. `trust_remote_code` stays false and the weights are not modified.

Sources: [model](https://huggingface.co/orcarouter/Qwen3.8-27B-Uncensored-MLX), [oMLX release](https://github.com/jundot/omlx/releases/tag/v0.6.4), [Hermes installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation), [Erga](https://github.com/Adr1an04/erga-mcp), [QMD](https://github.com/tobi/qmd).

## oMLX settings

Private settings live in `~/.omlx/settings.json` and `~/.omlx/model_settings.json`. Weights live under `~/.omlx/models`, outside Git.

- Authenticated API at `http://127.0.0.1:8000/v1`, never a LAN listener.
- Served model name `Qwen3.8-27B-Uncensored-4bit`.
- 16,384 total context tokens, a maximum of 2,048 output tokens, one active request.
- Streaming, with thinking enabled by default. Benchmarks and the worker's calls disable thinking explicitly.
- Model pinning off, idle TTL 600 seconds, server auto-start on app launch off.
- Safe memory guard. Apple's default Metal limit is unchanged.
- Prefix and SSD caching enabled, with a 1GB hot cache and a 20GB disk budget.
- External VLM MTP enabled with the downloaded `mtp` folder and block size 4. Logs confirm drafter attachment, draft acceptance and emitted tokens.

The checkpoint does not include embedded `mtp.*` tensors. The correct path is `vlm_mtp_enabled` with an external `qwen3_5_mtp` drafter. The native `mtp_enabled` flag does not apply. TurboQuant KV compression and this VLM MTP path are mutually exclusive in oMLX 0.6.4. Grammar-constrained requests can fall back to ordinary decoding. Tool arguments, JSON output and reasoning were checked with the selected configuration.

The isolated 16K-budget comparison produced 512 tokens at 21.96 tokens/second with MTP, against 13.90 first and 14.20 repeated with 8-bit TurboQuant KV. The warm TTFTs were 6.74 and 8.33 seconds. Logs verified actual 8-bit conversion of 15 cache layers. MTP remains selected, and the benchmark restored TurboQuant to disabled.

A green memory graph after a request does not prove pressure stayed green during inference. Record pressure, swap growth and responsiveness over the whole run. High Power mode and fan activity alone do not establish the cause of a memory-pressure change.

A temporary 30-second idle TTL test confirmed an actual model unload that freed 17.87GB. The normal 600-second TTL was restored afterward, and the next inference request reloads the weights.

## Hermes integration and compatibility patch

Hermes uses `model.provider: custom`, `model.base_url: http://127.0.0.1:8000/v1`, `model.default: Qwen3.8-27B-Uncensored-4bit` and `model.context_length: 16384`. Its custom-provider request and stale timeouts are 900 seconds. Credentials live in private local configuration and are never passed in process arguments. External-login adoption and telemetry sharing are disabled. Compression uses the same local model, and no cloud fallback is configured.

The pinned Hermes build normally requires a 64K model context. This setup uses an experimental, opt-in patch to that capacity check:

```sh
uv run python scripts/hermes_small_context.py /path/to/hermes-agent
```

Enable `HERMES_AUTOPILOT_16K=1` only for this certified local configuration. The script backs up the original file, is idempotent, and refuses an upstream constant it does not recognise. A Hermes update can overwrite the patch, so review and retest it after updating. The server still advertises and enforces its real 16K limit. The patch does not change tool authorization.

### The Rove MCP server in Hermes

The MCP server command is the absolute path to this checkout's `.venv/bin/rove`, with the argument `mcp` and the checkout as its working directory. Register it under the name `rove`, which makes its Hermes toolset `mcp-rove`.

- Disable every built-in Hermes toolset for this profile and set each platform's toolsets to `mcp-rove`.
- Use the include list of thirteen tools in [Onboarding and jobs](onboarding-and-jobs.md#hermes-connection), plus the eight chat tools `rove_status`, `whats_waiting`, `sends_today`, `pause_feed`, `resume_feed`, `company_history`, `answer_paste` and `what_you_can_ask`. The server also defines four synthetic certification tools and three direct browser tools, and none of those belong on the production list.
- Set `tools.tool_search.enabled: off` so the small schemas are present directly.
- Inside the MCP server's `tools` config, set `resources: false` and `prompts: false`. Excluding names alone does not suppress Hermes' generated wrappers.

The tools Hermes discovers globally are not necessarily the tools one agent receives. Check the agent's actual schema list.

### Discord gateway

The Hermes Discord integration authorizes one numeric owner ID and the configured `agent-control` channel. It does not accept other bots and does not backfill channel history. It needs Discord's Message Content Intent. Rove's own services read the same bot token and owner ID, as described in [Requirements](requirements.md#create-your-own-discord-bot).

The lightweight Hermes launchd gateway starts at login and can restart after a crash. This does not load the 27B model at login, because oMLX starts on demand. The upstream macOS service generator currently always writes `RunAtLoad`, and editing the generated plist by hand is not a durable way to change startup behavior.

The chat in `agent-control` shows answers only and runs with thinking off. These are the settings in the Hermes config:

```yaml
display:
  busy_ack_enabled: false
  platforms:
    discord:
      tool_progress: 'off'
      interim_assistant_messages: false
      long_running_notifications: false
      busy_ack_detail: false
      show_reasoning: false
      thinking_progress: false
      suppress_warning_notifications: true
agent:
  system_prompt: ''
  task_completion_guidance: false
  parallel_tool_call_guidance: false
  tool_use_enforcement: false
  execution_guidance: false
custom_providers:
  - name: rove-chat-no-thinking
    base_url: http://127.0.0.1:8000/v1
    model: Qwen3.8-27B-Uncensored-4bit
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
```

- `tool_progress` must be written as `off` for Discord, because Hermes treats an unset value as "show every tool call". The other display keys stop interim commentary, "still working" heartbeats, busy acknowledgements, reasoning and engine warnings. The typing indicator and the 👀 and ✅ reactions remain.
- Hermes merges the custom provider's `extra_body` into every request to that address and model, so chat turns skip Qwen's thinking. `agent.reasoning_effort` cannot do this: oMLX hands it to the chat template, which rejects `none` and falls back to `low`. The worker sends the same flag itself, so it is unaffected.
- The four agent switches remove Hermes' coding-agent prompt blocks, which push for tools this profile does not have.
- The persona is `SOUL.md` in the Hermes home: answer first in plain words, pass on the chat tools' words as they are, read the approved profile before stating any fact, and name the closest thing Rove can do when it cannot do something. `agent.system_prompt` stays empty.
- There is no output cap for chat turns. This Hermes build sends no `max_tokens` for a custom provider, whatever `model.max_tokens` says, and an `extra_body` cap would also cap the worker. The server's per-model ceiling of 2,048 tokens applies, and the persona keeps answers short.
- Hermes replaces a bare silence marker on a person's Discord message with a warning, so the agent always answers. That is why a pasted link is answered through the agent; see [Discord](discord.md#pasted-links).
- The model's generation config samples at temperature 1.0 when a request names none, and Hermes names none for the chat. At that temperature Qwen now and then drops the first lines of a long answer it was asked to pass on; at temperature 0 the same request was copied exactly every time. The worker sends temperature 0 and is unaffected. A lower default for the chat would be an oMLX model setting.

Restart the gateway after changing these: `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`. The display keys are also read again on every turn.

With nothing else running on the model, a warm "hi" took 1.8 to 3 seconds inside the agent after these changes, against 5 to 7.5 seconds before; a profile question took 6 to 8 seconds, against 15.6 to 16; "status" took 6 to 7 seconds, against 106 seconds and a 2,180-character answer. A first turn after oMLX unloaded the model (600 seconds idle) also pays for loading the weights.

## How the worker calls Qwen

The worker does not go through the Discord agent. Its structured prompts (the job-fit review, answer drafting, the writing cleanup, the pop-up choice and the recruiting-mail label) go to oMLX's OpenAI-compatible endpoint as one streamed `POST /v1/chat/completions` each, from `src/rove/model_client.py`:

- The system prompt comes from `scripts/recruiting_reasoning.py`; the user turn is the context as JSON, then `/no_think`.
- Temperature zero, a 2,048-token output ceiling, no tools, and `chat_template_kwargs.enable_thinking: false`. `/no_think` is the second switch.
- If the answer starts with reasoning instead of the answer (a `reasoning_content` delta, `<think>`, or prose where JSON is expected), the request is dropped at once and the call fails. oMLX cancels a request when its client disconnects. Reported reasoning tokens fail the call as well.
- A prompt is held under 12,500 tokens, system prompt included. The estimate counts 2.8 characters a token, which errs high for this JSON. Page text is trimmed first, then evidence excerpts. A prompt that is still over is never sent.
- A form's questions go in one drafting request unless their answers may not fit under the output ceiling. Each question carries its control (a one-line field, a text area, a list). Code estimates the output each answer may need, erring high, measured on drafting replays:
  - a reason for the owner: 40 tokens
  - a short answer or a choice: 60 tokens
  - a written answer: 45 tokens plus 2.9 tokens per word of its target. The target is a word count the question names, else what its character limit holds, else 130 words.
- When the estimate is over 1,600 tokens, the form is split into the fewest requests in form order, and their answers are merged. The questions come last, and everything before them is padded to a block boundary too, so the second request on reads only its own questions.
- oMLX down, a 5xx, a streamed error, a dropped connection or a timeout counts as the model being unavailable. The application goes back to the queue without a card and the next tick tries again. Anything else, such as invalid JSON twice or a reasoning start, is recorded as a failure.

The caller accepts only a finished answer. One that stopped at the output ceiling is never parsed as a draft.

Each context puts what every job shares first: the system prompt, the profile, the approved evidence excerpts and, for drafting, the voice note. The job's own text and questions come after. oMLX reuses a prompt prefix in whole 2,048-token blocks, so this shared part is read from the cache from the second job on, for the life of a profile version. A `cache_padding` field of digits (one token each) marks where the shared part ends. The first request of a profile version asks oMLX's `/v1/messages/count_tokens` for the exact size of the shared part and pads it to the next block boundary when that boundary is at most half a block away. The count is kept in `prompt-prefix-tokens.json` under the state root. The fit review and the drafting read the same evidence with one query; inside a pass both reads go through the pass's one Erga process.

`model_transport: "hermes"` in `config/workflow.json` sends the same prompts through the Hermes harness instead. That path runs `scripts/recruiting_reasoning.py` with the Hermes Python named by `hermes_python`: one non-streamed request with no tools, thinking off, temperature zero, a 2,048-token ceiling and `max_iterations=2`, with Hermes memory and context files skipped. A harness stop such as `max_iterations_reached` is retried once and then recorded as a failure.

### Why the worker calls the model server directly

`AGENTS.md` names Hermes as the agent harness, and changing that needs documented evidence. These are the measurements from 2026-10-03 on the reference Mac (oMLX 0.6.4, Qwen3.8-27B 4-bit with the external MTP drafter):

| Measured | Through Hermes | Direct |
| --- | ---: | ---: |
| Trivial prompt, model warm, wall time (three runs) | 4.68, 3.91, 3.98 s | 2.46, 2.33, 2.43 s |
| Fit-review prompt, tokens the server prefilled | 3,933 | 3,059 |

- Each Hermes call starts the Hermes Python, imports it and builds an agent: about 1.5 to 2.3 seconds before the request.
- Hermes puts about 2,000 characters (about 500 tokens) of its own identity prompt in front of Rove's on every call. That costs about 1.5 seconds of prefill, and its "You are Hermes Agent" contradicts the prompt that follows.
- Together that is about 3.5 seconds a call and 7 to 10 seconds an application, for a harness these calls do not use: they have no tools, no memory and one turn.
- Thinking stays off only because one request override reaches the chat template. Without it a fit review ran 105 seconds and returned 1,200 tokens of reasoning and no JSON. The direct path sets the switch itself, adds `/no_think`, and drops a reasoning start at once.
- A 40,000-character context was rejected with HTTP 400 at 17,170 tokens. The old budget counted characters; the direct path counts tokens.
- Decode ran at 12 to 28 tokens a second and prefill at 330 to 460 tokens a second under the owner's normal desktop load. Every output token and every prefilled token counts, which is why the contexts lead with what is shared and the answers are compact.

The same prompts after the change, replayed from stored real inputs on the same evening. The before column replays the old prompts and context order directly, so it leaves out the Hermes overhead above. Another client's requests shared the server during these runs, so the wall times are rough; the token counts are not.

| Call | Before | After |
| --- | --- | --- |
| Fit review, first job | 52.4 s · 3,432 in, 0 cached · 949 out | 47.4 s · 3,346 in, 0 cached · 483 out |
| Fit review, next job | 58.5 s · 4,362 in, 0 cached · 851 out | 26.4 s · 4,068 in, 2,048 cached · 351 out |
| Drafting, one question | 24.2 s · 5,015 in · 111 out | 12.4 s · 5,020 in, 2,048 cached · 58 out |
| Drafting, three questions | 39.3 s · 5,300 in · 375 out | 36.1 s · 5,737 in, 0 cached · 331 out |
| Drafting, 19 questions, same prompt replayed | 98.8 s · 7,056 in, 6,144 cached · 1,693 out | 70.8 s · 7,122 in, 6,144 cached · 1,212 out |
| Drafting, 19 questions, nothing cached | 98.6 s · 7,056 in · 1,693 out | 97.9 s · 7,122 in · 1,212 out |

An earlier version split every form into requests of eight questions. The same 19 questions took 145 seconds as three requests, because each request has its own fixed cost. That is why a form now goes as one request unless its expected answers pass 1,600 tokens; the estimate for these 19 was 1,422.

Hermes remains the Discord agent harness and the MCP and session layer. Its gateway, its Rove MCP server and its tool policy are unchanged.

### Model work while the worker waits

When the worker has nothing it may start (pacing, the daily cap, holds), its tick does three things:

- Posting text from the board: the next queued job in queue order that has no kept text is read from its board's public API, one read a tick. This only happens when the job's link is a Greenhouse (`boards.greenhouse.io`, `job-boards.greenhouse.io`), Lever (`jobs.lever.co`) or Ashby (`jobs.ashbyhq.com`) posting.
  - The reads are plain GETs to `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>`, `api.lever.co/v0/postings/<company>/<id>` or `api.ashbyhq.com/posting-api/job-board/<board>`.
  - They go through the same HTTP client and destination check as company research: no proxies, no cookies, no redirects followed, nothing about the owner in the request. Responses are capped at 5MB.
  - The visible text, with hidden elements dropped, is kept in `posting.json` with its identity: the board, the job id and a hash of the text.
  - A read that fails is tried again after 12 hours. Any other host is never read and its job is reviewed in its pass.
- Background fit reviews: queued applications whose posting text is kept are reviewed in queue order, one per tick, and stored under the same key a pass reads: the posting hash, the profile snapshot and the fit prompt version. Nothing is posted; the fit card reaches the thread with the first pass that uses it.
  - A pass whose page text hashes the same uses the stored review without a model call.
  - For a posting read from its board, the pass reads the board once more (at most 5 seconds) and uses the review when the board still shows the same posting word for word. The browser's own page text can differ.
  - Otherwise the pass reviews live, as before.
  - Postings captured by an earlier pass are kept too, so a new profile version or a new fit prompt is reviewed before the browser opens again.
- Keepalive: while jobs are queued and nothing has used the model for 8 minutes, a one-token request keeps oMLX from unloading the weights at its 600-second idle limit. `model_keepalive: false` in `config/workflow.json` turns it off. It keeps about 16GB resident while the queue waits.

No review or ping starts while oMLX reports a request in progress or waiting (`/api/status`), whoever sent it, and none of the three starts oMLX. All three wait for the first model request from the state root.

## Erga and QMD

Erga is installed in its own managed tool environment as the `erga-mcp` `uv` tool, from the project's fork branch. The install and upgrade recipe is in [Requirements](requirements.md#which-erga-to-install). The fork commit adds browser-like fetch headers and a `job_text` intake argument. `prepare_resume` passes the posting the recruiting browser captured (`job-reasoning-input.json`) through that argument when it exists, so Erga tailors from the same text the job-fit review saw and does not need to fetch a page that may refuse it.

The synthetic certification calls Erga's MCP interface with an isolated synthetic configuration under the private runtime root and `ERGA_MCP_TOOL_PROFILE=read`. It selects an approved synthetic evidence source and cannot select another database or write Erga facts.

Real career evidence uses separate private Erga state and the bounded `read_career_evidence` adapter described in [Onboarding and jobs](onboarding-and-jobs.md#approved-evidence-and-memory). Upstream's `read` profile withholds managed masters, so that trusted adapter uses `career-private` internally and exposes only approved excerpts to Hermes.

Resume generation and validation use Erga's supported CLI and Tectonic. The fixture master contains only synthetic education and project facts. Erga may correctly return `meaningful_change: false` when no supported tailoring is available. Check `validation.passed`, because the CLI can exit with code zero after a failed layout validation. `resume tailor` appends section content, and repeating an existing project through that command duplicates it. The verified fixture uses `resume tailor-job --job-text` and a local, validated artifact.

The pinned `resume tailor-job --validate` CLI path does not forward the configured multi-line bullet settings to its render validator. For a master allowing two-line bullets, use Erga's supported `validate_tailored_resume` MCP operation on a private package artifact. That path forwards both settings and still enforces page count, page fill and stranded-tail checks. Inspect its `returncode`, `pdf` and `skipped` fields, since a successful MCP transport alone is not a passing render. A base-master validation is separate from tailoring quality and from application approval.

A reviewed fixture PDF can be copied to `synthetic/erga-synthetic-resume.pdf` under the private runtime root, with its SHA-256 in `synthetic/resume-manifest.json`. The manifest cannot choose another filename. The browser fails closed if those PDF bytes change. Without a provisioned manifest, the standalone smoke test creates a synthetic text resume.

QMD is installed under `~/.local/share/rove/qmd`. Certification uses the isolated `rove` index and `rove-synthetic` collection. Real approved-profile retrieval uses the separate `rove-candidate` index and `approved-profile` collection, rebuilt with `rove memory index`. The Node installation works with packaged SQLite and extension support, so a separate Homebrew SQLite installation was unnecessary on the tested machine.

QMD's helper models are embeddinggemma-300M Q8_0, qmd-query-expansion-1.7B Q4_K_M and qwen3-reranker-0.6B Q8_0, about 2.25GB in total. These GGUF helpers belong to QMD, and the main Qwen model remains MLX. The adapter uses one short-lived process per query and never a resident QMD model server. Keyword search is the tool's default, and semantic retrieval is also tested through the adapter. CPU offloading is forced for the helper process because the packaged Metal helper emitted compilation warnings on this Mac. Cached queries can be much faster than first use. An empty semantic result is a legitimate threshold result and gives no permission to invent evidence.

Retrieved snippets are explicitly untrusted and cannot approve facts, credentials, uploads or submission. A high-ranked injection note remains untrusted. No vault-wide content is loaded into every prompt.

## Operations

From the checkout:

```sh
uv sync
uv run patchright install chromium
uv run rove start
uv run rove status
uv run rove stop
```

`start` starts the official oMLX app-managed service, waits for its model inventory, and starts the Hermes gateway. Weights load on the first inference request. `stop` stops Hermes before stopping oMLX.

Individual components:

```sh
uv run rove model start
uv run rove model stop
uv run rove model restart
uv run rove model status
uv run rove model logs
uv run rove gateway start
uv run rove gateway stop
uv run rove gateway restart
uv run rove gateway status
```

Hermes starts the Rove MCP process as needed. `uv run rove mcp` is the foreground MCP entry point and expects a stdio client. `uv run rove smoke` runs the visible browser fixture directly.

For overlapping memory measurements, `uv run rove smoke --hold-seconds 60` keeps the prepared browser open before closing it. The CLI accepts 0 to 120 seconds and reports the hold separately from mechanical work. The MCP tool uses the default hold of zero seconds.

The feed, workflow, browser and mail services are separate from these commands. See [Requirements](requirements.md#services).

## Verification and benchmarks

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/check_staged.py
uv run rove smoke
uv run rove benchmark /private/synthetic-4k.json /private/synthetic-8k.json /private/synthetic-16k.json
```

Prompt files contain arrays of OpenAI-format messages. Keep raw requests, output, measurements and screenshots outside Git. The 16K test must reserve space for generation: 15,872 input tokens plus 512 output tokens reaches 16,384.

`scripts/benchmark_hermes.py` runs in the pinned Hermes managed Python environment with `--hermes-checkout /path/to/hermes-agent`. It records the actual request and replays the identical payload directly. Tool names, token counts, cache hits, API-call count, streaming timings and memory samples are saved privately. The direct replay is normally warmer, so compare cache counts and queue activity before attributing a TTFT difference to Hermes. Each request uses the same model and decoding parameters. The measurement instrumentation exists only inside the benchmark process.

With incoming gateway work paused, `uv run python scripts/benchmark_kv.py /private/synthetic-16k.json` compares the current MTP path and 8-bit TurboQuant KV, then restores the original settings even on failure. Resume the gateway afterward.

### What the synthetic certification covered

The browser fixture verifies ten known fields, a frozen upload, holds for an unknown sensitive question and for a writing question, detection of a field that appears after the batch, and zero submissions. Its SQLite state deduplicates applications, records runs and checkpoints, and completes run and application state atomically. Submission attempts and forged submitted or approved states are rejected. Tests also cover changed resume bytes, hostile labels, unapproved dynamic answers, and denial of external network requests.

The synthetic Discord certification used three Qwen calls and all four certification MCP tools. Routine field filling used zero model calls, and the answer remained an unapproved draft.

That certification covered the local stack with synthetic data. It says nothing about real applicant-tracking sites, accounts, submission or recruiting mail. Those paths are exercised by the offline test suite, and the [product requirements](prd.md) track which behavior a test proves.
