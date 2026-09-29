# Requirements to run Erga Autopilot

This page is the checklist for a full local Autopilot setup. The project is still being built, so some items describe the target runtime rather than a finished installer. When implementation changes a requirement, update this file in the same branch.

The public repository should show what software, services, APIs, permissions, memory layers, and configuration the project expects. It must never contain a real user's secret values or applicant data.

## Supported development target

The primary development target is Apple Silicon macOS.

Reference hardware:

- 14-inch MacBook Pro
- Apple M5 Pro, 18-core CPU / 20-core GPU
- 48GB unified memory
- 1TB SSD

That is the machine the default local-model setup is tuned around, not a hard requirement for every user. Different hardware may need a smaller context window, different quantization, lower concurrency, a different browser mode, or another compatible local model/runtime profile.

See [Will this run on my machine?](hardware-check.md) before downloading a large model on different hardware.

## Base software

A full local setup is expected to need:

- macOS on Apple Silicon for the primary tested path
- Xcode Command Line Tools
- Git
- Python 3.12 through 3.14 for Autopilot, as declared in `pyproject.toml`
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 22 or newer for the reference full setup with QMD
- a supported MLX / MLX-VLM runtime
- Hermes Agent
- Erga
- Obsidian or an Obsidian-compatible local Markdown vault
- QMD for the reference local retrieval/indexing layer
- Playwright/Chromium and the dependencies used by the fast local browser runtime
- SQLite for Autopilot transactional state

Autopilot dependencies are locked in `uv.lock`. See [Local runtime](local-runtime.md) for the tested oMLX and model revisions and certification commands.

The certified synthetic setup also uses Tectonic 0.17.0 for local Erga resume compilation. Its pinned Hermes build needs an explicit 16K compatibility patch and a four-tool allowlist; stock Hermes in that build expects at least 64K. See the runtime page before updating it.

If QMD is disabled, the minimum Node.js version may be lower and should follow the current requirements of the remaining Node-based tools. The reference full setup uses Node.js 22+ because the current Hermes QMD skill requires it.

## Local model

The reference reasoning model is Qwen3.8-27B using an MLX-compatible quantization on Apple Silicon. Qwen remains the only required reasoning model in the reference architecture. Browser performance should come from a more efficient runtime, deterministic form handling, batching, and fewer model calls rather than a required cloud browser-decision model.

The reference setup is expected to use:

- a local Qwen3.8-27B model
- 4-bit quantization as the default starting point on the reference Mac
- a localhost-only model server
- one active model request at a time until concurrency has been tested
- a practical context limit chosen from measured memory pressure rather than the model's theoretical maximum

Before connecting real applicant data, test:

- model load and restart
- tool-call formatting
- structured output
- cancellation
- memory pressure and swap
- target context sizes
- browser usability while the model is loaded
- prompt-injection fixtures

Do not publish guessed tokens-per-second numbers as requirements.

## Memory and storage

The reference architecture deliberately separates semantic memory from transactional state.

### Hermes hot memory

Hermes built-in `MEMORY.md` and `USER.md` are for compact session-start context such as stable preferences, environment facts, project conventions, and tool quirks.

They are not the full candidate profile or application archive.

### Obsidian vault

The private Obsidian vault is the long-term semantic memory layer.

The reference public configuration key is:

```text
OBSIDIAN_VAULT_PATH
```

The real vault path is local-only.

The vault may contain validated profile notes, story/narrative context, company notes, research, decisions, and readable application notes. It must live outside the Git checkout.

The Obsidian desktop app is the reference human interface for that vault. The underlying storage is ordinary local Markdown, so the agent should not depend on a cloud sync service to function.

### QMD

QMD is the reference local indexing/retrieval layer for the vault as it grows.

At the time this page was updated, the Hermes QMD skill required:

- Node.js 22 or newer
- extension-capable SQLite on macOS rather than the system SQLite
- local helper-model downloads on first setup

On macOS, that may require a Homebrew SQLite installation specifically for QMD's extension support. QMD 2.8.3's packaged Node SQLite implementation worked on the tested machine without that additional install. This is separate from Autopilot's normal SQLite database access.

The current Hermes documentation reports that QMD's first run downloads roughly 2GB of local helper models for embeddings, reranking, and query expansion. Treat that number as upstream information that may change; verify it during installation.

QMD is derived state. The vault remains the source of truth. If the QMD index is lost, rebuild it.

### Autopilot SQLite

Autopilot SQLite is for transactional machine state, not the main semantic user memory.

It is expected to hold things such as:

- source-message checkpoints
- normalized job IDs and deduplication state
- onboarding/session checkpoints
- application runs
- browser runs
- submission attempts
- unknown-submission recovery
- Discord bindings
- Zoho reconciliation IDs
- reminders and action items
- outbox/idempotency state
- artifact metadata and hashes
- question fingerprints and answer references
- audit-event indexes

### Erga storage

Erga keeps its own local state and domain model. Autopilot should use Erga's supported interfaces rather than replacing Erga's storage with Obsidian or writing directly into its database from browser code.

### Private artifacts

Exact files such as resumes, frozen application packages, receipts, screenshots, traces, job snapshots, and rendered email evidence belong in private filesystem storage outside Git.

See [Memory and storage](memory-and-storage.md) for the full split.

## Required local services and components

The complete workflow is built around these components:

### Hermes Agent

Hermes is the production harness around the local model. It owns sessions, agent runs, tool access, MCP connections, skills, and operating-mode boundaries.

The reference setup uses Hermes' bundled Obsidian skill for vault access and may install the official QMD skill for local retrieval.

### Erga

Erga remains the foundation for career evidence, project evidence, resume tailoring, generated resume validation, application state, and recruiting-mail reconciliation.

Autopilot should integrate through Erga's supported interfaces rather than reaching directly into its database from browser code.

### Fast browser runtime

The production browser path uses a dedicated Playwright/Chromium recruiting browser.

The runtime should support compact structured observation, deterministic field resolution, batch form execution, targeted waits, post-action verification, controlled uploads, multi-page navigation, and the coverage needed for real ATS flows.

Playwright MCP may be installed for development, debugging, manual inspection, or fallback use. The production design should not require a general MCP round trip for every routine field and click.

No Jev/TypeSafe or other cloud browser-decision model is required by the reference setup.

Use a separate browser profile for recruiting. Do not reuse the user's everyday browser profile, password-manager extensions, unrelated sessions, or browser sync.

Read [Browser automation](browser-automation.md) before implementing or replacing this layer.

### Discord

Discord is the remote control surface and human-readable application archive.

A full setup needs:

- a Discord application/bot
- a bot token stored locally
- the owner's numeric Discord user ID stored locally
- the guild/server ID stored locally
- the IDs for the channels/forum/roles used by that installation stored locally
- Message Content Intent enabled for the Hermes bot in the Discord Developer Portal

The repository should refer to logical names in code and docs. Real IDs belong in local configuration.

### Zoho Mail

Zoho is optional for the first local setup but is part of the intended recruiting workflow for mail reconciliation and verification messages.

When enabled, use the official Zoho Mail API and OAuth flow with the narrowest scopes that support the required behavior.

The implementation may need values such as:

```text
ZOHO_CLIENT_ID
ZOHO_CLIENT_SECRET
ZOHO_REFRESH_TOKEN
ZOHO_ACCOUNT_ID
```

Those names are safe to document. Their real values are not.

If the implementation later uses different names or credentials, update this page to match the code.

## Local configuration and secrets

Public code may reference environment variables or local config keys. It must never contain the user's actual values.

A public example may look like:

```python
zoho_client_id = os.environ["ZOHO_CLIENT_ID"]
discord_token = os.environ["DISCORD_BOT_TOKEN"]
obsidian_vault = os.environ["OBSIDIAN_VAULT_PATH"]
```

A public sample config may look like:

```env
DISCORD_BOT_TOKEN=replace-me
DISCORD_OWNER_USER_ID=123456789012345678
DISCORD_GUILD_ID=123456789012345678
ZOHO_CLIENT_ID=replace-me
ZOHO_CLIENT_SECRET=replace-me
ZOHO_REFRESH_TOKEN=replace-me
ZOHO_ACCOUNT_ID=replace-me
OBSIDIAN_VAULT_PATH=/path/to/private/autopilot-vault
```

Every value above is synthetic. Never copy a real local `.env`, OAuth response, Discord ID set, credential file, vault path, or token into the repository.

Secrets should come from one of the approved local secret/config mechanisms used by the implementation. Keep the real files outside Git or in ignored local-only paths.

## Local state

Real runtime state belongs outside the repository checkout.

A normal Autopilot runtime root can live under:

```text
~/.config/erga-autopilot/
```

Expected categories may include:

```text
config/
secrets/
state/
browser/
applications/
mail/
artifacts/
logs/
backups/
```

The private Obsidian vault can live elsewhere and is pointed to by local configuration.

The exact layout should follow the implementation. The important rule is that applicant data, vault notes, credentials, browser state, receipts, screenshots, logs, databases, and indexes are not committed with the source code.

## Discord layout

The default logical layout is:

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

A local installation maps those logical destinations to real Discord IDs in private configuration.

The source bot should only have access to the source channels it needs. The Autopilot bot should have only the permissions required for its workflow.

`#memory` is an interface to the local memory system; it is not the memory database itself.

## Accounts and external access

Depending on which features are enabled, the user may need:

- a GitHub account for cloning/forking and development
- a Discord account and Discord application/bot
- Obsidian desktop if they want the reference human vault interface
- a Zoho Mail account with API/OAuth access if mail tracking is enabled
- access to the user's normal job-application email account through the supported mail integration

Employer/ATS accounts are created only when an application requires them. Generated employer passwords stay in encrypted local state and never belong in the repository, Obsidian vault, or Discord logs.

## Browser and OS permissions

The local setup may require macOS permissions for the tools that actually need them. Grant the minimum permissions required.

Do not grant broad Full Disk Access, Accessibility, normal browser profile access, or unrelated filesystem permissions just because an agent asks for them. If a capability genuinely requires a macOS permission, document the exact reason and scope when it is implemented.

Obsidian/vault access should be scoped to the configured vault path rather than treated as permission to read the user's whole home directory.

## Network requirements

The project is local-first, not offline-only.

A normal run may need outbound network access for:

- Discord
- employer and ATS websites
- public company research
- Zoho APIs when enabled
- GitHub/upstream dependency installation
- model download during setup
- QMD helper-model download during setup when QMD is enabled

The local model server, QMD index, vault, and internal control services should stay local. Internal services should stay bound to localhost unless a reviewed design explicitly says otherwise.

## Data the normal automation does not require

A normal setup should not store or automate:

- full Social Security numbers
- bank or routing information
- passport or driver's-license numbers/images
- SMS/authenticator/security-key MFA secrets

Those steps remain manual unless a later reviewed design adds explicit support.

## Repository safety requirements

Before committing or pushing:

- inspect `git status`
- inspect the diff and staged diff
- run the repository's secret checks
- confirm no real applicant data, vault content, or runtime artifact is staged
- do not bypass secret-scanning or push-protection warnings

Public tests and fixtures must use synthetic people, companies, jobs, email, credentials, Discord IDs, resumes, vault notes, and application receipts.

`.gitignore` is defense in depth, not permission to keep secrets or the real vault in the checkout.

## Documentation requirement

This page is part of the implementation contract.

When a change adds, removes, or replaces a runtime dependency, API, credential, environment variable, required account, OS permission, Discord component, memory/storage component, model requirement, or setup step, update this page in the same branch.

Keep it general enough for any user to follow. Show public variable names, API names, code paths, storage roles, and setup concepts. Never document one maintainer's real credentials, IDs, applicant profile, Obsidian vault contents/path, private file paths, or recruiting data.

## Next

- [Getting started](getting-started.md)
- [Memory and storage](memory-and-storage.md)
- [Will this run on my machine?](hardware-check.md)
- [How it works](how-it-works.md)
- [Security](../SECURITY.md)
- [Prompt injection](prompt-injection.md)
