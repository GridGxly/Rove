# Getting started

Erga Autopilot is still experimental. This guide explains the intended local setup and the order the project is being built in. If a command described here does not exist yet, check the repository status before assuming your machine is misconfigured.

The safest way to try the project today is with synthetic profile data and prepare-only browser workflows.

## Before you start

This project is currently designed around Apple Silicon macOS.

Recommended development target:

- Apple Silicon Mac;
- 48GB unified memory for the full Qwen3.8-27B stack;
- 1TB or more free local storage is comfortable for model files, browser state, traces, resumes, and application archives;
- macOS with current security updates.

The author's primary machine is a 14-inch MacBook Pro with an M5 Pro, 48GB unified memory, and a 1TB SSD.

Lower-memory Apple Silicon machines may work with smaller context windows or different local-model settings, but they are not the main target for this repository.

## Software prerequisites

The planned stack uses:

- Python 3.11 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- Git;
- Node.js 20 or newer;
- an MLX-compatible Qwen3.8 runtime;
- Hermes Agent;
- Playwright MCP;
- Discord bot credentials;
- optional Zoho OAuth credentials for recruiting-mail tracking.

On macOS, make sure the Xcode command-line tools are available:

```bash
xcode-select --install
```

Install `uv` using its official installation instructions, then verify it:

```bash
uv --version
```

Verify Git and Node as well:

```bash
git --version
node --version
```

Playwright MCP currently requires Node.js 20 or newer.

## Clone the repository

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

## Keep runtime data outside Git

Do not put your live recruiting state inside the repository.

The intended local layout is similar to:

```text
~/.config/erga-autopilot/
├── config.toml
├── secrets/
├── state/
├── profile/
├── browser/
├── applications/
├── logs/
└── backups/
```

The repository should contain code, schemas, tests, synthetic fixtures, documentation, and versioned skills. It should not contain your real applicant profile, browser cookies, OAuth tokens, generated passwords, resume output history, application receipts, private screenshots, or live databases.

## Erga

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp).

Erga remains responsible for the parts it already does well:

- career evidence;
- project and Git evidence;
- resume sources;
- resume tailoring;
- LaTeX generation and validation;
- application lifecycle state;
- recruiting-mail reconciliation.

Autopilot adds browser execution, onboarding/profile memory, Discord product UI, form-level auditing, and submission orchestration around that core.

Do not replace Erga's storage by editing its SQLite tables directly from browser code. Use its public application/domain surface or its MCP tools.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), which is Apache-2.0 licensed.

The project is designed to run a 4-bit MLX-compatible build locally and expose it to Hermes through a localhost-only model server.

Do not bind the model server to your LAN or the public internet.

A normal development workflow should certify the model before trusting it with applications:

- confirm tool-call formatting;
- confirm structured output;
- test 8K, 16K, 32K, and 64K contexts;
- measure memory pressure and swap;
- verify browser use while the model is loaded;
- test cancellation and restart behavior;
- run synthetic prompt-injection cases.

The repository should record the exact model revision and runtime versions once the local configuration is certified.

## Hermes

Hermes is the intended production agent harness.

It should connect to the local Qwen endpoint and expose only the tools needed for the current operating mode.

Examples of modes include:

```text
ONBOARDING
JOB_REVIEW
RESEARCH
RESUME
APPLICATION_PREPARE
APPLICATION_SUBMIT
MAIL_REVIEW
MANUAL_TAKEOVER
```

The same Qwen model can be used across those modes. The mode changes permissions and available tools, not the identity of the model.

## Playwright MCP

Browser automation is handled with [Playwright MCP](https://playwright.dev/mcp/installation).

Use a dedicated recruiting browser profile.

Do not connect Autopilot to your everyday browser profile. The browser used for job applications should not contain unrelated logins, saved banking sessions, personal password-manager extensions, or other private browsing state.

Start in headed, prepare-only mode so you can watch the browser work.

Do not enable unattended submission before the prepare-only stage has been tested against real application flows.

## Discord

Discord is the intended remote interface.

A typical private server layout is:

```text
SOURCES
# internship-jobs
# new-grad-jobs

PIPELINE
applications        (forum)
# shortlist

AGENT
# agent-control
# action-needed
# memory

RECRUITING
# recruiting

SYSTEM
# system-log
```

The source bot should have access only to the source channels it needs. Your Autopilot bot should be authorized by your numeric Discord user ID, not only a display name.

The `applications` forum is the human-readable archive. Local state remains authoritative.

## Zoho

Zoho integration is optional, but it is useful for:

- application acknowledgements;
- email verification for employer accounts;
- online assessment invitations;
- interview scheduling;
- offers;
- rejections;
- recruiter follow-ups.

Prefer the official Zoho Mail API with narrow, read-only scopes where possible instead of browser scraping the inbox.

Do not permanently log verification codes.

## First safe run

Before using real applicant data, verify the stack with a synthetic profile.

A safe first end-to-end test should look like:

1. create a fake job record;
2. create a synthetic candidate profile;
3. have Qwen classify form questions;
4. open a demo form through Playwright MCP;
5. fill the form without submitting;
6. record every field and click;
7. generate a fake application forum timeline;
8. confirm no secret or private file was exposed.

Only after that should you connect real resumes, Discord, Zoho, and employer application pages.

## Build stages

The project is intentionally staged:

1. repository and architecture foundation;
2. Mac runtime foundation;
3. Qwen3.8 local-runtime certification;
4. Hermes policy and mode layer;
5. Erga integration and local state;
6. security and prompt-injection tests;
7. Discord control plane;
8. applicant onboarding;
9. job ingestion and shortlist;
10. resume and company research;
11. Playwright prepare-only automation;
12. controlled submission;
13. recruiting-mail tracking;
14. restricted autopilot;
15. operations, backups, and upgrade testing.

Do not skip directly to autopilot.

## Next reading

- [How it works](how-it-works.md)
- [Security](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)
