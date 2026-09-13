# Getting started

Erga Autopilot is still experimental. Some of this guide describes the setup the project is being built toward, not commands that are guaranteed to exist on every branch yet.

Start with synthetic data and prepare-only browser runs. Do not connect real recruiting data until the earlier pieces work and the security checks pass.

## Hardware

The primary development machine is:

- 14-inch MacBook Pro
- Apple M5 Pro with an 18-core CPU and 20-core GPU
- 48GB unified memory
- 1TB SSD

The full Qwen3.8-27B stack is being tuned against 48GB unified memory.

You do not need the same machine. If your hardware is different, clone or fork the repo and tune the runtime for it. The settings most likely to change are:

- model or quantization
- context length
- KV-cache settings
- model concurrency
- headed vs. headless browser mode
- screenshot and trace retention
- application queue concurrency
- memory and disk limits

Lower-memory Apple Silicon machines may work with a smaller context window, a more aggressive quantization, or a lighter local-model setup. Other platforms may work too, but Apple Silicon macOS is the current development target.

If you are not sure whether your machine has enough headroom, use [Will this run on my machine?](hardware-check.md). It includes a prompt that asks another model or coding agent to read the current repo, inspect your hardware safely, and recommend a starting configuration.

## Software

The current plan uses:

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Node.js 20 or newer
- an MLX-compatible Qwen3.8 runtime
- Hermes Agent
- Playwright MCP
- Discord bot credentials
- optional Zoho OAuth credentials for recruiting-mail tracking

On macOS, install the Xcode command-line tools if needed:

```bash
xcode-select --install
```

Then verify the basics:

```bash
uv --version
git --version
node --version
```

## Clone the repo

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

If your hardware needs different runtime settings, keep those changes in your own clone or fork so they are easy to track.

## Keep live data outside Git

The Git checkout is for code, docs, schemas, migrations, tests, and synthetic fixtures.

Live recruiting data should live somewhere outside the repository, for example:

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

Do not put real applicant profiles, browser cookies, OAuth tokens, generated passwords, resume output history, application receipts, recruiting email, private screenshots, or live databases in the repository.

The `.gitignore` is a backup layer, not permission to keep sensitive files inside the checkout.

## Public examples stay synthetic

Anything committed to this project should be safe to publish.

Use fake people, companies, email addresses, Discord IDs, job postings, and application receipts in docs and tests. Do not copy a real production artifact into `tests/fixtures` just because it is convenient.

## Erga

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp).

Erga remains responsible for the parts it already handles well:

- career evidence
- project and Git evidence
- resume sources
- resume tailoring
- LaTeX generation and validation
- application lifecycle state
- recruiting-mail reconciliation

Autopilot adds browser execution, candidate onboarding and profile memory, the Discord interface, field-level application logging, and submission orchestration around that core.

Do not bypass Erga by editing its SQLite tables directly from browser code. Use its application/domain surface or MCP tools.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), run locally through an MLX-compatible runtime.

The model server should listen on localhost only.

Before using it on real applications, test the exact runtime on your machine for:

- tool-call formatting
- structured output
- 8K, 16K, 32K, and 64K contexts
- memory pressure and swap
- browser use while the model is loaded
- cancellation and restart behavior
- synthetic prompt-injection cases

Record the exact model revision and runtime versions once a local setup is stable.

## Hermes

Hermes is the agent harness around Qwen. It connects to the local model and exposes only the tools allowed for the current mode.

Planned modes include:

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

The model can stay the same while the available tools and permissions change.

## Playwright MCP

Browser automation goes through [Playwright MCP](https://playwright.dev/mcp/installation).

Use a dedicated recruiting browser profile. Do not connect Autopilot to your everyday browser profile or give it unrelated logins, banking sessions, password-manager extensions, or other private browser state.

Start with a visible browser and prepare-only runs so you can watch what happens. Do not turn on unattended submission until those runs are reliable.

## Discord

Discord is the phone-friendly control surface.

A private server can use a layout like this:

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

These are logical names, not hard-coded IDs. Real guild, channel, role, and user IDs belong in local configuration.

A third-party source bot should only see the source channels it needs. The Autopilot bot should authorize its owner by numeric Discord user ID rather than display name alone.

## Zoho

Zoho is optional. It can be useful for:

- application acknowledgements
- employer-account email verification
- online assessment invitations
- interview scheduling
- offers
- rejections
- recruiter follow-ups

Use the official Zoho Mail API with narrow scopes where possible. Do not leave verification codes in logs or Discord after they are used.

## First safe run

Before using real applicant data, prove the stack with a fake profile.

A good first test is:

1. create a fake job record
2. create a synthetic candidate profile
3. have Qwen classify the form questions
4. open a demo form through Playwright MCP
5. fill it without submitting
6. record every field and click
7. generate a fake Discord application timeline
8. confirm that no secret or private file was exposed

Only after that should real resumes, Discord, Zoho, and employer application pages be connected.

## Build order

The current build order is:

1. repository and architecture foundation
2. Mac runtime foundation
3. Qwen3.8 local-runtime certification
4. Hermes policy and mode layer
5. Erga integration and local state
6. security and prompt-injection tests
7. Discord control plane
8. applicant onboarding
9. job ingestion and shortlist
10. resume and company research
11. Playwright prepare-only automation
12. controlled submission
13. recruiting-mail tracking
14. restricted autopilot
15. operations, backups, and upgrade testing

Do not jump straight to autopilot because the browser can click Submit.

## Next

- [Will this run on my machine?](hardware-check.md)
- [How it works](how-it-works.md)
- [Prompt injection](prompt-injection.md)
- [Security](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Agent guide](../AGENTS.md)
