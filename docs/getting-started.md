# Getting started

Rove is still experimental. Some of this guide describes the setup the project is being built toward, not commands that are guaranteed to exist on every branch yet.

Start with synthetic data and prepare-only browser runs. Do not connect real recruiting data until the earlier pieces work and the security checks pass.

See [Local runtime](local-runtime.md) for the implemented certification commands and pinned model. `uv run rove smoke` launches the dedicated browser against a local synthetic form. It does not accept production application URLs.

Before installing the full stack, read [Requirements to run](requirements.md). It tracks the software, services, accounts, APIs, memory/storage layers, local configuration, browser setup, and network access a complete installation needs.

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
- QMD indexing overhead
- memory and disk limits

Lower-memory Apple Silicon machines may work with a smaller context window, a more aggressive quantization, or a lighter local-model setup. Other platforms may work too, but Apple Silicon macOS is the current development target.

If you are not sure whether your machine has enough headroom, use [Will this run on my machine?](hardware-check.md). It includes a prompt that asks another model or coding agent to read the current repo, inspect your hardware safely, and recommend a starting configuration.

## Software

The reference full setup uses:

- Python 3.12 through 3.14 for Rove (Erga itself supports 3.11+)
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Node.js 22 or newer when using the reference QMD retrieval setup
- an MLX-compatible Qwen3.8 runtime
- Hermes Agent
- Erga
- Obsidian or an Obsidian-compatible local Markdown vault
- QMD for local vault retrieval
- Playwright/Chromium browser dependencies for the fast local browser runtime
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

If QMD is part of the setup, check the current Hermes QMD requirements before installing it. The current reference path requires Node.js 22+ and extension-capable SQLite on macOS.

## Clone the repo

```bash
git clone https://github.com/GridGxly/Rove.git
cd Rove
uv sync
```

If your hardware needs different runtime settings, keep those changes in your own clone or fork so they are easy to track.

## Keep live data outside Git

The Git checkout is for code, docs, schemas, migrations, tests, and synthetic fixtures.

Live runtime data should live somewhere outside the repository, for example:

```text
~/.config/rove/
├── config/
├── secrets/
├── state/
├── browser/
├── applications/
├── mail/
├── artifacts/
├── logs/
└── backups/
```

The private Obsidian vault can live elsewhere. Point to it with local configuration such as `OBSIDIAN_VAULT_PATH`.

Do not put real applicant profiles, vault notes, browser cookies, OAuth tokens, generated passwords, resume output history, application receipts, recruiting email, private screenshots, QMD indexes, or live databases in the repository.

The `.gitignore` is a backup layer, not permission to keep sensitive files inside the checkout.

## Public examples stay synthetic

Anything committed to this project should be safe to publish.

Use fake people, companies, email addresses, Discord IDs, job postings, vault notes, and application receipts in docs and tests. Do not copy a real production artifact into `tests/fixtures` just because it is convenient.

## Erga

Rove builds on [Erga](https://github.com/Adr1an04/erga-mcp).

Erga remains responsible for the parts it already handles well:

- career evidence
- project and Git evidence
- resume sources
- resume tailoring
- LaTeX generation and validation
- application lifecycle state
- recruiting-mail reconciliation

Rove adds browser execution, candidate onboarding/profile memory, the Discord interface, field-level application logging, long-term semantic memory, and submission orchestration around that core.

Do not bypass Erga by editing its SQLite tables directly from browser code. Use its application/domain surface or MCP tools.

## Memory and storage

Read [Memory and storage](memory-and-storage.md) before wiring real data into the project.

The short version is:

- Hermes `MEMORY.md` / `USER.md`: small hot memory for session-start context
- Obsidian vault: long-term semantic memory and human-readable candidate knowledge
- QMD: local retrieval/index over the vault
- SQLite: transactional state for jobs, browser/application runs, submissions, bindings, queues, checkpoints, and idempotency
- private files: exact resumes, application packages, receipts, screenshots, traces, and other artifacts
- Erga: its own career/resume/application domain state

Do not use Markdown as the submission state machine, and do not put the entire candidate knowledge base into SQLite.

## Set up the private vault

The reference setup uses a private Obsidian vault outside the repository.

A starting layout can be:

```text
Rove/
├── Profile/
├── Story/
├── Career/
├── Companies/
├── Applications/
├── Research/
├── Decisions/
├── Daily/
└── System/
```

Do not create folders just to match the diagram if the implementation does not need them yet.

The public config key is expected to be:

```text
OBSIDIAN_VAULT_PATH
```

The real path stays in local configuration.

The vault is not a free-form authority surface. Candidate/profile writes still go through schema validation and approval rules.

## QMD

QMD is the reference local retrieval layer for the vault once the note set is large enough that filename-based lookup becomes brittle.

Treat the QMD index as rebuildable derived state.

Do not store credentials or irreplaceable state only inside the index.

Verify current Hermes QMD setup instructions before installing. At the time this guide was updated, the reference setup required Node.js 22+, extension-capable SQLite on macOS, and additional local helper-model downloads.

## Local model

The target model is [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B), run locally through an MLX-compatible runtime.

The model server should listen on localhost only.

Before using it on real applications, test the exact runtime on your machine for:

- tool-call formatting
- structured output
- 4K, 8K, and 16K contexts on the reference 48GB Mac; increase only after measuring headroom
- memory pressure and swap
- browser use while the model is loaded
- vault/QMD retrieval while the model is loaded
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

The reference setup uses Hermes' Obsidian skill for vault operations. Canonical profile writes should still be mediated by Rove's validated profile/memory operations rather than unrestricted model edits.

## Fast browser runtime

The production browser path uses a dedicated Playwright/Chromium recruiting browser.

Qwen3.8-27B remains the local reasoning model. The browser layer should make routine form work fast in normal code by using compact observations, deterministic field resolution, safe batching, targeted waits, and post-fill verification. Qwen should be called when the form is ambiguous or needs real judgment, not for every known field.

Playwright MCP can still be useful while developing or debugging the browser layer, but it is not required as the per-action production loop.

Use a dedicated recruiting browser profile. Do not connect Rove to your everyday browser profile or give it unrelated logins, banking sessions, password-manager extensions, or other private browser state.

Start with a visible browser and prepare-only runs so you can watch what happens. Measure browser round trips, Qwen calls, fill accuracy, retries, manual takeovers, and memory pressure before turning on unattended submission.

Read [Browser automation](browser-automation.md) for the full design.

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

A third-party source bot should only see the source channels it needs. The Rove bot should authorize its owner by numeric Discord user ID rather than display name alone.

`#memory` is a conversational interface to the local memory system. It is not the database or the vault itself.

## Zoho

Zoho is optional. It can be useful for:

- application acknowledgements
- employer-account email verification
- online assessment invitations
- interview scheduling
- offers
- rejections
- recruiter follow-ups

Use the official Zoho Mail API with narrow scopes where possible. Do not leave verification codes in logs, the vault, or Discord after they are used.

The implemented mail service reads the Inbox and tracks acknowledgements, online assessments, interviews, offers and rejections; the Self Client setup, the three read scopes and where the four values go are in [Requirements](requirements.md#zoho-mail).

## First safe run

Before using real applicant data, prove the stack with fake data.

A good first test is:

1. create a synthetic Obsidian vault/profile
2. create a fake job record
3. have Qwen retrieve the right synthetic profile context
4. verify QMD retrieval if QMD is enabled
5. open a demo form through the fast browser runtime
6. inspect and normalize the form as a group
7. fill deterministic fields in a safe batch without submitting
8. verify every filled value and record each field/action
9. generate a fake Discord application timeline
10. confirm that no secret or private file was exposed
11. confirm a frozen profile/application snapshot is not affected by later edits to the synthetic vault

Only after that should real resumes, the private vault, Discord, Zoho, and employer application pages be connected.

## Build order

The current build order is:

1. repository and architecture foundation
2. Mac runtime foundation
3. Qwen3.8 local-runtime certification
4. Hermes policy, hot memory, and Obsidian/QMD foundation
5. Erga integration and transactional Rove state
6. security and prompt-injection tests
7. Discord control plane
8. applicant onboarding and validated vault profile
9. job ingestion and shortlist
10. resume and company research
11. fast browser runtime, compact observation, form normalization, and deterministic batch filling
12. prepare-only browser reliability and ATS adapter testing
13. controlled submission
14. recruiting-mail tracking
15. restricted unattended operation
16. operations, backups, and upgrade testing

Do not jump straight to unattended operation because the browser can click Submit.

## Next

- [Requirements to run](requirements.md)
- [Memory and storage](memory-and-storage.md)
- [Will this run on my machine?](hardware-check.md)
- [How it works](how-it-works.md)
- [Prompt injection](prompt-injection.md)
- [Security](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
