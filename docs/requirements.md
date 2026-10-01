# Requirements to run Rove

This page is the checklist for a full local Rove setup. The project is still being built, so some items describe the target runtime rather than a finished installer. When implementation changes a requirement, update this file in the same branch.

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
- Python 3.12 through 3.14 for Rove, as declared in `pyproject.toml`
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 22 or newer for the reference full setup with QMD
- a supported MLX / MLX-VLM runtime
- Hermes Agent
- Erga
- Obsidian or an Obsidian-compatible local Markdown vault
- QMD for the reference local retrieval/indexing layer
- Patchright (a Playwright fork) and Google Chrome, or Patchright's Chrome for Testing, for the recruiting browser
- the `cryptography` package for the encrypted local credential store
- optionally a local clone of [Unslop](https://github.com/theclaymethod/unslop) for draft cleanup scanners (`unslop_path`)
- SQLite for Rove transactional state

Rove dependencies are locked in `uv.lock`. See [Local runtime](local-runtime.md) for the tested oMLX and model revisions and certification commands.

Real candidate onboarding and Keryx discovery use the same Python dependencies. They
require a private Obsidian vault path and local state storage; Keryx refresh reads
only the selected public GitHub source. See [Onboarding and jobs](onboarding-and-jobs.md).

The setup also uses Tectonic 0.17.0 for local Erga resume compilation. Its pinned
Hermes build needs an explicit 16K compatibility patch; stock Hermes in that build
expects at least 64K. Certification used four fixture tools; production uses thirteen
real tools with generic MCP wrappers disabled. Real evidence
requires a separate, reviewed Erga master, and approved profile changes require
`autopilot memory index` to refresh QMD. See the runtime page before updating it.

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

On macOS, that may require a Homebrew SQLite installation specifically for QMD's extension support. QMD 2.8.3's packaged Node SQLite implementation worked on the tested machine without that additional install. This is separate from Rove's normal SQLite database access.

The current Hermes documentation reports that QMD's first run downloads roughly 2GB of local helper models for embeddings, reranking, and query expansion. Treat that number as upstream information that may change; verify it during installation.

QMD is derived state. The vault remains the source of truth. If the QMD index is lost, rebuild it.

### Rove SQLite

Rove SQLite is for transactional machine state, not the main semantic user memory.

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

Erga keeps its own local state and domain model. Rove should use Erga's supported interfaces rather than replacing Erga's storage with Obsidian or writing directly into its database from browser code.

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

Rove should integrate through Erga's supported interfaces rather than reaching directly into its database from browser code.

#### Which Erga to install

The reference setup installs Erga from the project's own fork,
[GridGxly/erga-mcp](https://github.com/GridGxly/erga-mcp), branch
`fetch-headers-and-job-text`, as a `uv` tool. That branch is upstream `main` plus one
commit: Erga's job fetch sends ordinary browser request headers, and `intake_job_url`
accepts a `job_text` argument so Autopilot can hand over the posting its own browser
already captured when a careers site refuses Erga's direct fetch. The fork's `main`
tracks upstream and carries nothing of its own.

```bash
uv tool install --force --python 3.12 \
  "git+https://github.com/GridGxly/erga-mcp@fetch-headers-and-job-text"
erga review --config ~/.config/erga-autopilot/erga/config.toml
```

That installs `erga`, `erga-mcp`, and `erga-tokens` under `~/.local/bin`, which is where
Autopilot's Erga bridge expects `erga-mcp`.

To pull Adrian's updates into the fork and reinstall:

```bash
git clone https://github.com/GridGxly/erga-mcp.git && cd erga-mcp
git remote add upstream https://github.com/Adr1an04/erga-mcp.git
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout fetch-headers-and-job-text && git merge main
# resolve conflicts if any, then run Erga's own gate before pushing:
uv lock --check && uv run ruff format --check && uv run ruff check && uv run mypy src \
  && uv run coverage run -m unittest discover -s tests
git push origin fetch-headers-and-job-text
uv tool install --force --python 3.12 \
  "git+https://github.com/GridGxly/erga-mcp@fetch-headers-and-job-text"
erga review --config ~/.config/erga-autopilot/erga/config.toml
.venv/bin/pytest -q   # Autopilot's suite, from this repository
```

Keep the fork's `main` fast-forward only so later upstream merges stay clean. Record the
installed commit in [Local runtime](local-runtime.md) when it changes.

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

Zoho is optional. When it is configured, `autopilot mail tick` reads the Inbox of one Zoho
Mail account every 15 minutes and classifies recruiting mail about sent applications; see
[Application workflow](application-workflow.md#recruiting-mail).

The integration uses the official Zoho Mail REST API through a Self Client and a refresh
token. It only reads: the folder list, message headers, and one message body at a time.
It never sends, moves, or deletes mail. Setup, once:

1. In the [Zoho API Console](https://api-console.zoho.com/), add a **Self Client** and
   note its client id and client secret.
2. On its **Generate Code** tab, enter the scopes
   `ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ`, a short
   description, and a validity of a few minutes; create the code.
3. Within that time, exchange the code for a refresh token. The US accounts server is
   shown; use the one for your data center (for example `accounts.zoho.eu` or
   `accounts.zoho.in`):

   ```sh
   curl -s -X POST https://accounts.zoho.com/oauth/v2/token \
     -d grant_type=authorization_code -d client_id=... -d client_secret=... -d code=...
   ```

   The response holds `refresh_token`, which does not expire, and an `access_token` that
   lasts an hour; the service refreshes its own access token on every tick.
4. Find the account id with that access token:

   ```sh
   curl -s -H "Authorization: Zoho-oauthtoken ..." https://mail.zoho.com/api/accounts
   ```

   `data[0].accountId` is the value.
5. Put the four values in the private env file the services read, `config/setup.env`
   under the state root (`~/.config/erga-autopilot/` by default), one `KEY=value` per line:

   ```text
   ZOHO_CLIENT_ID
   ZOHO_CLIENT_SECRET
   ZOHO_REFRESH_TOKEN
   ZOHO_ACCOUNT_ID
   ```

   Outside the US data center also set `ZOHO_API_BASE` (for example
   `https://mail.zoho.eu`); the matching accounts server is derived from it, or set
   `ZOHO_ACCOUNTS_BASE` explicitly.
6. Write private `config/mail.json` with `{"enabled": true}` (optional `lookback_days`,
   default 3, for the first tick only) and run `uv run autopilot install-services`, which
   installs the `dev.erga-autopilot.mail` launch agent next to the feed. `uv run autopilot
   mail status` shows whether the values were found, without printing them.

Those names are safe to document. Their real values, the refresh token above all, are
not: never paste the curl output into the repository, the vault, Discord, or an issue.

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

A normal Rove runtime root can live under:

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

The source bot should only have access to the source channels it needs. The Rove bot should have only the permissions required for its workflow.

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

## Visible application workflow configuration

The [application workflow](application-workflow.md) uses private feed/workflow channel
mappings, a macOS launch agent for source refresh, a private Unix-socket browser
service, and the installed Hermes Python for Qwen proposals. Discord needs permission
to send messages and create posts in the configured existing forum. Channel management
is unnecessary when mapping existing channels. No proxy, Camofox service, Docker stack,
or cloud extraction provider is required by the default implementation.

The recruiting browser is launched by the daemon with a localhost-only DevTools port;
the local account is trusted by design and nothing else on the network can reach it.
Employer accounts the workflow creates are stored under the private state root as
ciphertext with a separate owner-only key file.

Final submission is off until the private workflow configuration sets
`submission_enabled` and lists an adapter in `submit_adapters`. The adapters are
`greenhouse_v1` for public Greenhouse job boards, `lever_v1` for public Lever postings, and
`generic_v1` for employer sites without an ATS contract, listed last. Every submission also needs the owner's
`send it` reply in the application's thread, bound to the exact package hash (or an explicit `submit APPLICATION_ID PACKAGE_HASH` in the control channel), unless the owner turns on the `auto_submit` policy in the private workflow config, which sends complete packages with a daily cap and a minimum gap.
