# Getting started

Erga Autopilot is still experimental. Some of the docs describe the system we are building toward, not something that is already ready on every branch. If a command in this guide does not exist yet, check the repo status before assuming your setup is broken.

For now, use synthetic profile data and keep browser tests in prepare-only mode until the earlier pieces are proven.

## Hardware

I am building this on Apple Silicon macOS.

My current development machine is:

- 14-inch MacBook Pro
- Apple M5 Pro with an 18-core CPU and 20-core GPU
- 48GB unified memory
- 1TB SSD

The full Qwen3.8-27B stack is comfortable on 48GB, which is the setup I am tuning against. That does not mean you need the same machine.

If your hardware is different, clone or fork the repo and adjust the runtime for it. The settings most likely to change are:

- model or quantization
- context length
- KV-cache settings
- model concurrency
- headed vs. headless browser mode
- screenshot and trace retention
- application queue concurrency
- memory and disk limits

Lower-memory Apple Silicon machines may still work with smaller context windows, a more aggressive quantization, or a lighter local-model setup. Other platforms may work too, but they are not tested here yet.

If you get a different hardware profile working well, document the exact machine and settings instead of assuming they will carry over to everyone else.

If you are not sure whether your machine has enough headroom for the whole stack, use the [hardware sanity check](hardware-check.md). It gives you a prompt to paste into ChatGPT, Claude, Gemini, Grok, or another capable agent so it can compare your machine with the current repo before you start changing things.

## Software you will need

The current stack expects:

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Node.js 20 or newer
- an MLX-compatible Qwen3.8 runtime
- Hermes Agent
- Playwright MCP
- Discord bot credentials
- optional Zoho OAuth credentials for recruiting-mail tracking

On macOS, install the Xcode command-line tools if you do not already have them:

```bash
xcode-select --install
```

Install `uv` from its official instructions, then check it:

```bash
uv --version
```

Check Git and Node too:

```bash
git --version
node --version
```

Playwright MCP currently needs Node.js 20 or newer.

## Clone the repo

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

If you are changing runtime settings for different hardware, keep those changes in your own clone or fork so they are easy to track.

## Keep live data out of the repo

Your real recruiting state should live outside Git.

A local layout might look like this:

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

The repo is for code, schemas, tests, synthetic fixtures, docs, and versioned skills. Keep your real applicant profile, browser cookies, OAuth tokens, generated passwords, resume output history, application receipts, private screenshots, and live databases out of it.

## Erga

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp).

Erga already handles:

- career evidence
- project and Git evidence
- resume sources
- resume tailoring
- LaTeX generation and validation
- application lifecycle state
- recruiting-mail reconciliation

Autopilot adds the pieces I wanted around it: browser execution, onboarding and profile memory, the Discord UI, field-by-field application logging, and submission orchestration.

Do not bypass Erga by poking at its SQLite tables from browser code. Use its application/domain surface or MCP tools.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), licensed under Apache 2.0.

The plan is to run a 4-bit MLX-compatible build locally and expose it to Hermes through a localhost-only model server.

Do not bind that server to your LAN or the public internet.

Before trusting the model with real applications, test it on the machine you are actually using:

- tool-call formatting
- structured output
- 8K, 16K, 32K, and 64K contexts
- memory pressure and swap
- browser use while the model is loaded
- cancellation and restart behavior
- synthetic prompt-injection cases

Once a local setup is stable, record the exact model revision and runtime versions you used.

## Hermes

Hermes is the agent harness around Qwen.

It connects to the local model and exposes only the tools allowed for the current mode.

Example modes:

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

The model stays the same. The mode changes what it is allowed to read or change.

## Playwright MCP

Browser automation goes through [Playwright MCP](https://playwright.dev/mcp/installation).

Use a dedicated recruiting browser profile. Do not connect Autopilot to your everyday browser profile or give it unrelated logins, banking sessions, personal password-manager extensions, or other private browser state.

Start with a visible browser and prepare-only runs so you can watch what happens. Do not turn on unattended submission until those runs are solid.

## Discord

Discord is the phone-friendly control surface.

A private server can be laid out like this:

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

A third-party source bot should only see the source channels it actually needs. Your Autopilot bot should authorize you by numeric Discord user ID, not just a display name.

The `applications` forum is the readable archive. The local databases remain the machine source of truth.

## Zoho

Zoho is optional, but it is useful for:

- application acknowledgements
- employer-account email verification
- online-assessment invitations
- interview scheduling
- offers
- rejections
- recruiter follow-ups

Use the official Zoho Mail API with narrow read-only scopes where possible instead of scraping the inbox through a browser.

Do not keep verification codes in logs or Discord after they are used.

## First safe run

Before using real applicant data, prove the stack with a fake profile.

A good first end-to-end test is:

1. create a fake job record
2. create a synthetic candidate profile
3. have Qwen classify the form questions
4. open a demo form through Playwright MCP
5. fill it without submitting
6. record every field and click
7. generate a fake Discord application timeline
8. confirm that no secret or private file was exposed

Only then should you connect real resumes, Discord, Zoho, and employer application pages.

## Build order

The project is being built in this order:

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

Do not jump straight to autopilot just because the browser can click Submit.

## Next

- [Hardware sanity check](hardware-check.md)
- [How it works](how-it-works.md)
- [Security](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)
