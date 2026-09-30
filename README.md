<p align="center">
  <img src="docs/assets/erga-autopilot-logo.svg" width="760" alt="Erga Autopilot" />
</p>

<p align="center">
  <strong>A local-first recruiting agent for finding roles, preparing applications, filling forms, and keeping a complete application history.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-C8792A.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/status-experimental-F2A93B.svg" alt="Experimental" />
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-171717.svg" alt="macOS Apple Silicon" />
</p>

## Why this exists

I built Erga Autopilot for my own internship search.

It’s built on top of [Erga](https://github.com/Adr1an04/erga-mcp), which already handles the stuff I care about like application tracking, resume tailoring, project evidence, and recruiting emails. I wanted to take it a step further and automate the part I was still doing by hand: opening applications, filling out forms, asking me when something is unclear, saving exactly what was submitted, and tracking what happens afterward.

Erga Autopilot is **not** an official Erga project and is not affiliated with its maintainer. Adrian and the Erga contributors did the original Erga work. This project builds on that foundation under the MIT license and adds browser automation, a Discord control surface, local applicant memory, and application orchestration. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution details.

## What it does

A normal run should look roughly like this:

1. pick up a job from Discord or a direct URL;
2. check it against the candidate profile and application rules;
3. use Erga to prepare an evidence-backed resume;
4. research the company when a written answer needs context;
5. use local Qwen3.8-27B for reasoning;
6. work through the application in a dedicated fast local browser runtime;
7. stop when something is unknown, sensitive, or needs approval;
8. submit only when the application package is complete;
9. keep the exact resume, answers, clicks, screenshots, and receipt;
10. watch recruiting mail for OAs, interviews, offers, and rejections.

Discord is the remote interface. The model, memory, databases, resumes, browser state, credentials, and application history stay on the user's machine.

## Architecture

```text
Discord
  │
  ▼
Autopilot
  │
  ├── Qwen3.8-27B      reasoning
  ├── Hermes Agent     sessions, tools, MCP, hot memory
  ├── Erga             career evidence, resumes, application state
  ├── Obsidian         long-term semantic memory
  │     └── QMD        local search/indexing
  ├── SQLite           transactional workflow state
  ├── Browser runtime  fast Playwright/Chromium execution
  └── Zoho Mail        recruiting mail and verification events
```

The split is deliberate. Obsidian holds the human-readable long-term knowledge: candidate profile, preferences, story, company notes, research, and decisions. SQLite stays focused on state that must be exact: queues, checkpoints, browser runs, submission attempts, deduplication, Discord bindings, mail reconciliation, and idempotency. Large artifacts such as resumes, receipts, screenshots, and traces stay as private files.

Hermes' built-in memory remains small and hot. It should point the agent toward the durable local knowledge rather than trying to hold the whole recruiting history in the system prompt.

Read [How it works](docs/how-it-works.md), [Browser automation](docs/browser-automation.md), [Discord architecture](docs/discord.md), and [Memory and storage](docs/memory-and-storage.md) for the longer version.

## Application archive

Every application gets its own Discord forum post. The thread should still be useful months later, not just say "applied."

It can record:

- the source job and official application URL;
- the exact resume that was uploaded;
- each form page reached;
- every field label and value entered;
- where each answer came from;
- checkboxes, selections, and meaningful clicks;
- account creation and verification events;
- written answers and approval history;
- validation errors and retries;
- the final submit action;
- the confirmation page and receipt;
- later events such as an OA, interview, offer, rejection, or withdrawal.

Secrets are excluded from that history.

## Memory

The private Obsidian vault is the long-term semantic memory layer.

It is intended to hold approved profile information, application preferences, narrative context, company notes, research, decisions, and other knowledge the user may want to browse or edit directly.

Canonical profile notes are still validated by code. A webpage, email, research result, or model output cannot write itself into the approved candidate profile just because it appears in the vault.

When an application is prepared, Autopilot freezes the approved profile/version and other inputs it used so later edits to the vault do not rewrite history.

QMD can provide local retrieval over the vault as it grows. QMD is an index, not the source of truth.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), running locally on Apple Silicon with an MLX-compatible quantization.

Qwen handles judgment calls such as job fit, unfamiliar form wording, company research, browser recovery, resume selection, and written-response drafting. Exact applicant facts come from validated local state rather than free-form model memory. Qwen is not used as the low-level driver for every routine field and click. The browser runtime handles compact observation, deterministic field resolution, batching, waits, and verification in normal code.

## Hardware

The primary development machine is:

- 14-inch MacBook Pro;
- M5 Pro, 18-core CPU / 20-core GPU;
- 48GB unified memory;
- 1TB SSD.

Qwen3.8-27B at 4-bit is still a large local model. The full stack is being tuned against 48GB unified memory.

You do not need the same Mac to experiment with the project. If your machine is different, clone or fork the repo and adjust the model, quantization, context size, concurrency, browser mode, retention limits, and local retrieval settings for your hardware. Settings tested on one machine should not be assumed to behave the same everywhere else.

Other platforms may work later, but Apple Silicon macOS is the current development target.

Not sure where your machine lands? Read [Will this run on my machine?](docs/hardware-check.md). It includes a prompt you can give to ChatGPT, Claude, Gemini, Grok, or another capable agent so it can read the current repo, compare the stack with your hardware, and suggest a starting configuration without making up benchmark numbers.

## Requirements

The current reference setup uses:

- macOS on Apple Silicon;
- Python 3.12 through 3.14 for Autopilot;
- [`uv`](https://docs.astral.sh/uv/);
- Git;
- Node.js 22+ for the full reference setup with QMD;
- an MLX / MLX-VLM runtime that supports Qwen3.8;
- Hermes Agent;
- Erga;
- Obsidian or an Obsidian-compatible local Markdown vault;
- QMD for local vault retrieval in the reference full setup;
- Playwright/Chromium browser dependencies for the fast local browser runtime;
- Discord bot credentials;
- optional Zoho Mail OAuth for recruiting-mail tracking.

For the complete software, account, API, secret, local-state, browser, and network checklist, read [Requirements to run](docs/requirements.md).

See [Getting started](docs/getting-started.md) before connecting real data.

## Security and privacy

This project handles sensitive recruiting data. Treat anything committed to this repository as public.

Real profiles, the private Obsidian vault, resumes, browser sessions, credentials, application receipts, recruiting mail, screenshots, logs, and live databases belong outside the Git checkout. Public examples and tests must use synthetic people, companies, messages, and credentials.

Read [SECURITY.md](SECURITY.md), [Browser automation](docs/browser-automation.md), and [Prompt injection](docs/prompt-injection.md) before connecting Discord, the browser runtime, Zoho, or real application data.

## Getting started

This is still experimental and is not a one-command consumer app yet.

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

Then follow [docs/getting-started.md](docs/getting-started.md).

If you want the original local-first recruiting assistant without browser auto-application behavior, use [Erga](https://github.com/Adr1an04/erga-mcp) directly.

## Status

The local Discord/Hermes/oMLX stack, Erga evidence, QMD retrieval and synthetic prepare-only browser workflow have been verified. Real Keryx job intake, reviewed candidate onboarding, approved profile/evidence reads and version-checked QMD profile retrieval are implemented; see [Onboarding and jobs](docs/onboarding-and-jobs.md). The production Hermes tool list uses these real workflows. See [Local runtime](docs/local-runtime.md) for the tested configuration and [Runtime measurements](docs/runtime-benchmarks.md) for results and limits. The [application workflow](docs/application-workflow.md) connects a durable queue, a visible browser, code-checked Qwen job-fit review, deterministic preparation, Erga intake, Qwen/Hermes answer proposals, forum recording, and owner-approved single-attempt submission on public Greenhouse boards with an independent confirmation contract. Other ATS adapters, account-based flows, and mail reconciliation are not implemented yet.

The project is being built in stages: local runtime, model certification, Hermes/Obsidian memory, Erga integration, transactional state, Discord control, onboarding, job intake, resume/research workflows, prepare-only browser automation, controlled submission, recruiting lifecycle tracking, and finally restricted autopilot.

Working code is not the same thing as safe unattended submission. Irreversible behavior stays behind test gates until the earlier pieces have been proven.

See [Getting started](docs/getting-started.md#build-order) for the build order.

## Contributing

This started as a personal tool, but focused issues and pull requests are welcome.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing application state, browser permissions, profile memory, resume evidence, or security boundaries.

## Author

Built by [Ralph Clavens Love Noel](https://github.com/GridGxly) for personal use, then opened up in case the same workflow is useful to someone else.

## License

Erga Autopilot is licensed under the [MIT License](LICENSE).

Parts of the project and some design decisions come from Erga and remain subject to Erga's MIT license and attribution requirements. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
