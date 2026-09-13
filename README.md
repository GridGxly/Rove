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

[Erga](https://github.com/Adr1an04/erga-mcp) already handled a lot of the hard parts I cared about: local application tracking, evidence-backed resume tailoring, Git-backed project evidence, and recruiting-mail reconciliation. I wanted to take that foundation one step further and let a local agent actually work through applications for me.

That means opening the form, filling what it knows, stopping when it needs me, keeping the exact resume and answers it submitted, and tracking what happens afterward.

Erga Autopilot is not an official Erga project and is not affiliated with its maintainer. Adrian and the Erga contributors did the original Erga work. This repo builds on that foundation under the MIT license and adds the browser automation and personal-agent layer I wanted for myself. See [Third-party notices](THIRD_PARTY_NOTICES.md) for the attribution details.

## What it does

A normal run looks like this:

1. pick up a job from Discord or a direct URL;
2. check it against your profile and application rules;
3. use Erga to prepare an evidence-backed resume;
4. research the company when a written question needs context;
5. use local Qwen3.8-27B for reasoning;
6. work through the application in a dedicated Playwright browser;
7. stop and ask you when something is unknown or needs approval;
8. submit only after the application package is complete;
9. keep the exact resume, answers, clicks, screenshots, and receipt;
10. watch recruiting mail for OAs, interviews, offers, and rejections.

Discord is the remote interface. The model, databases, resumes, browser state, application history, and credentials stay on your machine.

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

The split is deliberate: local state stores the facts, Discord shows the readable history, Qwen handles ambiguity, Hermes runs the agent loop, Playwright operates the browser, and Zoho catches recruiting events afterward.

Read [How it works](docs/how-it-works.md) for the longer version.

## Application archive

Every application gets its own Discord forum post. I want that thread to be useful months later, not just say "applied."

It should keep:

- the source job and official application URL;
- the exact resume file that was uploaded;
- each form page the agent reached;
- every field label and value it entered;
- where each answer came from;
- every checkbox, selection, and meaningful click;
- account creation and email verification events;
- written answers and approval history;
- browser validation errors and retries;
- the final submit action;
- the confirmation page and receipt;
- later events such as an OA, interview, offer, rejection, or withdrawal.

Tags show the current lifecycle state. Timeline comments show how it got there.

## Memory and onboarding

The model does not get to invent its own memory format.

The candidate profile is a versioned schema in code. Qwen can conduct the onboarding conversation and ask follow-ups, but it only writes into fields, policies, and story slots that already exist. A separate `introduction.md` stores the more human context: motivations, interests, proudest projects, leadership stories, writing style, and similar background.

When the agent needs information, it checks sources in this order:

1. approved candidate profile;
2. approved Erga evidence;
3. `introduction.md`;
4. portfolio and GitHub;
5. external research.

If a form asks something the profile has never seen, Autopilot pauses and asks. You can save the answer afterward so the next equivalent question does not interrupt you again.

## Browser automation

Browser work goes through [Playwright MCP](https://playwright.dev/mcp/installation) in its own recruiting profile.

Keep your everyday browser separate. The application agent should not inherit unrelated logins, banking sessions, saved passwords, or personal browser state.

The project starts in visible, prepare-only mode so you can watch it work. Submission and unattended autopilot come later.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), running locally on Apple Silicon with an MLX-compatible quantization.

Qwen is the part that handles judgment calls: job fit, unfamiliar form wording, company research, browser recovery, resume selection, and written-response drafting.

It is not the database and it is not the authorization layer. Exact facts such as contact information, education, work authorization, dates, and saved application answers come from structured local state.

## Hardware

I am developing and testing this on Apple Silicon macOS.

My current machine is:

- 14-inch MacBook Pro
- M5 Pro, 18-core CPU / 20-core GPU
- 48GB unified memory
- 1TB SSD

Qwen3.8-27B at 4-bit is still a large local model. The full stack is comfortable on 48GB, which is the hardware I am tuning against.

You do not need the same Mac to experiment with the repo. If your machine has less memory or a different GPU/CPU setup, clone or fork it and tune the model, quantization, context size, concurrency, browser mode, and retention limits for your hardware. Just do not assume settings tested on my machine will behave the same everywhere else.

Other platforms may work later, but I am not testing them right now.

Not sure where your machine lands? [Run the hardware sanity check](docs/hardware-check.md). It includes a prompt you can give to ChatGPT, Claude, Gemini, Grok, or another capable agent so it can read the current repo, inspect your specs, and recommend a starting configuration without guessing benchmark numbers.

## Requirements

The local stack currently expects:

- macOS on Apple Silicon
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Node.js 20+ for Playwright MCP
- an MLX / MLX-VLM runtime that supports Qwen3.8
- Hermes Agent
- Discord bot credentials
- optional Zoho Mail OAuth for recruiting-mail tracking

See [Getting started](docs/getting-started.md) before wiring it up to real data.

## Status

This is experimental software and it is being built in stages.

The rough order is:

- local runtime and model certification
- candidate onboarding and memory
- Discord control plane
- job ingestion and shortlist
- resume and company research
- Playwright prepare-only automation
- controlled submission
- recruiting-mail tracking
- restricted autopilot

Working code is not the same thing as safe unattended submission. Irreversible steps stay behind test gates until the earlier pieces have been proven against real application flows.

## Security and privacy

This project handles resumes, contact details, browser sessions, employment history, recruiting email, and employer-account credentials.

Keep real runtime state out of Git. Do not commit databases, resumes, application receipts, cookies, OAuth tokens, screenshots, private profile data, generated passwords, or logs with personal information.

Read [SECURITY.md](SECURITY.md) before connecting Discord, Playwright, Zoho, or real application data.

## Getting started

This is not a one-command consumer app yet. If you want to try it, start with synthetic data:

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

Then follow [docs/getting-started.md](docs/getting-started.md).

If you want the original local-first recruiting assistant without browser auto-application behavior, use [Erga](https://github.com/Adr1an04/erga-mcp) directly.

## Contributing

This started as a personal tool, so I care more about correctness and traceability than clever automation.

Issues and focused pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing application state, browser permissions, profile memory, resume evidence, or security boundaries.

## Author

Built by [Ralph Clavens Love Noel](https://github.com/GridGxly) for personal use, then opened up in case the same workflow is useful to someone else.

## License

Erga Autopilot is licensed under the [MIT License](LICENSE).

Parts of the project and some design decisions come from Erga and remain subject to Erga's MIT license and attribution requirements. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
