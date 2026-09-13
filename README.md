<p align="center">
  <img src="docs/assets/erga-autopilot-logo.svg" width="760" alt="Erga Autopilot" />
</p>

<p align="center">
  <strong>A local-first recruiting agent that can research jobs, tailor resumes, fill applications, and keep a complete application history.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-C8792A.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/Status-Experimental-F2A93B.svg" alt="Experimental" />
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-171717.svg" alt="macOS Apple Silicon" />
</p>

## Why this exists

I built Erga Autopilot for my own internship search.

I liked what [Erga](https://github.com/Adr1an04/erga-mcp) already did well: local application tracking, evidence-backed resume tailoring, Git-backed project evidence, and recruiting-mail reconciliation. I wanted one more layer on top of that foundation: a local agent that could actually open an application, fill it out, ask me when it did not know something, and leave behind a complete record of what it submitted.

That is why this repository exists.

Erga Autopilot is not an official Erga project and is not affiliated with its maintainer. It is an independent personal project built on top of ideas and code from Erga under its MIT license. The original work belongs to Adrian and the Erga contributors; this project extends that foundation for browser-based application automation. See [Third-party notices](THIRD_PARTY_NOTICES.md) for details.

## What it does

Erga Autopilot is designed around a local recruiting loop:

1. ingest job leads from Discord or a direct URL;
2. decide whether the role matches your profile and application rules;
3. use Erga to prepare evidence-backed resume material;
4. research the company when a written answer needs real context;
5. use a local Qwen model as the reasoning layer;
6. control a dedicated browser through Playwright MCP;
7. pause and ask you when a fact or written response needs approval;
8. submit only when the application package is complete;
9. archive the exact resume, answers, clicks, screenshots, and receipt;
10. watch recruiting mail for OAs, interviews, offers, and rejections.

The intended interface is Discord. The model, databases, resumes, browser state, application history, and credentials stay on your machine.

## Architecture

```text
Discord
  │
  ▼
Autopilot control plane
  │
  ├── Qwen3.8-27B      reasoning
  ├── Hermes Agent     agent loop / sessions / MCP
  ├── Erga             career evidence / resumes / application state
  ├── Playwright MCP   browser control
  ├── Zoho Mail        recruiting events and verification mail
  └── SQLite           local profile and automation state
```

The main rule is simple:

> local state holds machine truth; Discord shows the human-readable history; Qwen reasons; Hermes orchestrates; Playwright acts; Zoho observes what happens afterward.

Read [How it works](docs/how-it-works.md) for the full design.

## Application archive

Each application gets a Discord forum post that acts like a flight recorder.

The thread is expected to record:

- the source job and official application URL;
- the exact resume file uploaded;
- every form page reached;
- every field label and value entered;
- the source of each answer;
- every checkbox, selection, and meaningful click;
- account creation and email verification events;
- written answers and approval history;
- browser validation errors and retries;
- the final submit action;
- the confirmation page and receipt;
- later lifecycle events such as OA, interview, offer, rejection, or withdrawal.

Lifecycle tags show the current state. Detailed timeline comments explain how the application got there.

## Memory and onboarding

Autopilot does not let the model invent its own personal-memory structure.

The candidate profile is a versioned schema defined in code. Onboarding is conversational, but Qwen only fills predefined fields, policies, and story slots. A separate `introduction.md` captures narrative context such as motivations, interests, proudest projects, leadership stories, and writing style.

The intended source order is:

1. approved candidate profile;
2. approved Erga evidence;
3. `introduction.md`;
4. portfolio and GitHub;
5. external research.

If a new application asks something the profile does not know, Autopilot pauses, asks once, and can save the answer into the existing structure after approval.

## Browser automation

Browser work is handled through [Playwright MCP](https://playwright.dev/mcp/installation) in a dedicated browser profile.

Your personal browser should stay separate. The application agent should not be connected to your everyday browser profile, saved passwords, banking sessions, or unrelated accounts.

The project starts in visible / prepare-only mode. Submission and unattended autopilot are intentionally later stages.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), run locally on Apple Silicon using an MLX-compatible quantization.

The model is not the database and it is not the authorization layer. It handles ambiguity: job fit, unfamiliar form wording, company research, browser recovery, resume selection, and written-response drafting.

Exact facts such as contact information, education, work authorization, dates, and saved application answers come from structured local state.

## Hardware

This project is currently designed and developed for Apple Silicon macOS.

My current development machine is:

- 14-inch MacBook Pro;
- M5 Pro, 18-core CPU / 20-core GPU;
- 48GB unified memory;
- 1TB SSD.

Qwen3.8-27B at 4-bit is a large local model. For the full stack, 48GB unified memory is the comfortable target I am developing against. Lower-memory Apple Silicon systems may still work with a smaller context window, a different quantization, or a lighter model configuration, but they should not be expected to behave exactly like the primary development machine.

The repository is intentionally editable rather than tied to one exact Mac. If your hardware is different, clone or fork the project and adjust the local model, context length, concurrency, browser settings, and other runtime limits for your machine. Those alternate configurations are welcome, but they are not the primary tested target yet.

Other platforms may work eventually, but they are not currently tested.

## Requirements

The planned local stack includes:

- macOS on Apple Silicon;
- Python 3.11+;
- [`uv`](https://docs.astral.sh/uv/);
- Git;
- Node.js 20+ for Playwright MCP;
- a supported MLX / MLX-VLM runtime;
- Hermes Agent;
- Discord bot credentials;
- optional Zoho Mail OAuth for recruiting-mail tracking.

See [Getting started](docs/getting-started.md) before installing anything.

## Status

This repository is experimental and is being built in phases.

The intended rollout is:

- local runtime and model certification;
- candidate onboarding and memory;
- Discord control plane;
- job ingestion and shortlist;
- resume and company-research pipeline;
- Playwright prepare-only automation;
- controlled submission;
- recruiting-mail tracking;
- restricted autopilot.

Do not assume unattended submission is safe merely because the code runs. Each irreversible capability should be enabled only after the earlier stage has been tested against real application flows.

## Security and privacy

This project handles unusually sensitive information: resumes, contact details, browser sessions, employment history, recruiting email, and generated employer-account credentials.

Real user state must stay outside the repository. Do not commit databases, resumes, application receipts, cookies, OAuth tokens, screenshots, private profile data, generated passwords, or logs containing personal information.

Read [SECURITY.md](SECURITY.md) before connecting Discord, Playwright, Zoho, or real application data.

## Getting started

The repository is not yet a one-command consumer product. If you want to experiment with it, start with the setup guide and synthetic data:

```bash
git clone https://github.com/TransferTrack/erga-autopilot.git
cd erga-autopilot
uv sync
```

Then follow [docs/getting-started.md](docs/getting-started.md).

If you are looking for the original local-first recruiting assistant without browser auto-application behavior, use [Erga](https://github.com/Adr1an04/erga-mcp) directly.

## Contributing

This started as a personal tool, so the priority is correctness, auditability, and not lying on job applications.

Issues and focused pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before changing application state, browser permissions, profile memory, resume evidence, or security boundaries.

## Author

Built by [Ralph Clavens Love Noel](https://github.com/GridGxly) for personal use, then opened up in case the same workflow is useful to someone else.

## License

Erga Autopilot is licensed under the [MIT License](LICENSE).

Portions and design foundations derived from Erga remain subject to Erga's MIT license and attribution requirements. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
