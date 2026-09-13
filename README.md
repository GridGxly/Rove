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

[Erga](https://github.com/Adr1an04/erga-mcp) already handled several parts I cared about: local application tracking, evidence-backed resume tailoring, Git-backed project evidence, and recruiting-mail reconciliation. I wanted to keep that foundation and add the part I was still doing by hand: opening applications, working through forms, asking me when something was unknown, preserving exactly what was submitted, and tracking what happened afterward.

Erga Autopilot is **not** an official Erga project and is not affiliated with its maintainer. Adrian and the Erga contributors did the original Erga work. This project builds on that foundation under the MIT license and adds browser automation, a Discord control surface, local applicant memory, and application orchestration. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution details.

## What it does

A normal run should look roughly like this:

1. pick up a job from Discord or a direct URL;
2. check it against the candidate profile and application rules;
3. use Erga to prepare an evidence-backed resume;
4. research the company when a written answer needs context;
5. use local Qwen3.8-27B for reasoning;
6. work through the application in a dedicated Playwright browser;
7. stop when something is unknown, sensitive, or needs approval;
8. submit only when the application package is complete;
9. keep the exact resume, answers, clicks, screenshots, and receipt;
10. watch recruiting mail for OAs, interviews, offers, and rejections.

Discord is the remote interface. The model, databases, resumes, browser state, credentials, and application history stay on the user's machine.

## Architecture

```text
Discord
  │
  ▼
Autopilot
  │
  ├── Qwen3.8-27B      reasoning
  ├── Hermes Agent     sessions, tools, MCP, agent loop
  ├── Erga             career evidence, resumes, application state
  ├── Playwright MCP   browser control
  ├── Zoho Mail        recruiting mail and verification events
  └── SQLite           local profile and automation state
```

The split is deliberate: code owns facts, permissions, and irreversible actions; Qwen handles ambiguity; Hermes runs the agent loop; Playwright operates the browser; Erga handles career evidence and resume work; Discord shows the readable history.

Read [How it works](docs/how-it-works.md) for the longer version.

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

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), running locally on Apple Silicon with an MLX-compatible quantization.

Qwen handles judgment calls such as job fit, unfamiliar form wording, company research, browser recovery, resume selection, and written-response drafting. Exact applicant facts come from structured local state rather than model memory.

## Hardware

The primary development machine is:

- 14-inch MacBook Pro;
- M5 Pro, 18-core CPU / 20-core GPU;
- 48GB unified memory;
- 1TB SSD.

Qwen3.8-27B at 4-bit is still a large local model. The full stack is being tuned against 48GB unified memory.

You do not need the same Mac to experiment with the project. If your machine is different, clone or fork the repo and adjust the model, quantization, context size, concurrency, browser mode, and retention limits for your hardware. Settings tested on one machine should not be assumed to behave the same everywhere else.

Other platforms may work later, but Apple Silicon macOS is the current development target.

Not sure where your machine lands? Read [Will this run on my machine?](docs/hardware-check.md). It includes a prompt you can give to ChatGPT, Claude, Gemini, Grok, or another capable agent so it can read the current repo, compare the stack with your hardware, and suggest a starting configuration without making up benchmark numbers.

## Requirements

The current plan uses:

- macOS on Apple Silicon;
- Python 3.11+;
- [`uv`](https://docs.astral.sh/uv/);
- Git;
- Node.js 20+;
- an MLX / MLX-VLM runtime that supports Qwen3.8;
- Hermes Agent;
- Playwright MCP;
- Discord bot credentials;
- optional Zoho Mail OAuth for recruiting-mail tracking.

For the complete software, account, API, secret, local-state, browser, and network checklist, read [Requirements to run](docs/requirements.md).

See [Getting started](docs/getting-started.md) before connecting real data.

## Security and privacy

This project handles sensitive recruiting data. Treat anything committed to this repository as public.

Real profiles, resumes, browser sessions, credentials, application receipts, recruiting mail, screenshots, logs, and live databases belong outside the Git checkout. Public examples and tests must use synthetic people, companies, messages, and credentials.

Read [SECURITY.md](SECURITY.md) and [Prompt injection](docs/prompt-injection.md) before connecting Discord, Playwright, Zoho, or real application data.

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

The project is being built in stages: local runtime, model certification, Erga integration, Discord control, onboarding, job intake, resume/research workflows, prepare-only browser automation, controlled submission, recruiting lifecycle tracking, and finally restricted autopilot.

Working code is not the same thing as safe unattended submission. Irreversible behavior stays behind test gates until the earlier pieces have been proven.

See [Roadmap](docs/roadmap.md) for the current build order.

## Contributing

This started as a personal tool, but focused issues and pull requests are welcome.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing application state, browser permissions, profile memory, resume evidence, or security boundaries.

## Author

Built by [Ralph Clavens Love Noel](https://github.com/GridGxly) for personal use, then opened up in case the same workflow is useful to someone else.

## License

Erga Autopilot is licensed under the [MIT License](LICENSE).

Parts of the project and some design decisions come from Erga and remain subject to Erga's MIT license and attribution requirements. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
