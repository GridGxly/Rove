# Requirements to run Erga Autopilot

This page is the checklist for a full local Autopilot setup. The project is still being built, so some items describe the target runtime rather than a finished installer. When implementation changes a requirement, update this file in the same branch.

The public repository should show what software, services, APIs, permissions, and configuration the project expects. It must never contain a real user's secret values or applicant data.

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
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 20 or newer
- a supported MLX / MLX-VLM runtime
- Hermes Agent
- Playwright MCP and its browser dependencies
- Erga
- SQLite, normally through Python/runtime dependencies

As implementation lands, pin exact versions where compatibility requires it. Do not guess version requirements in documentation. Verify them against the code and upstream projects.

## Local model

The reference reasoning model is Qwen3.8-27B using an MLX-compatible quantization on Apple Silicon.

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

## Required local services and components

The complete workflow is built around these components:

### Hermes Agent

Hermes is the production harness around the local model. It owns sessions, agent runs, tool access, MCP connections, skills, and operating-mode boundaries.

### Erga

Erga remains the foundation for career evidence, project evidence, resume tailoring, generated resume validation, application state, and recruiting-mail reconciliation.

Autopilot should integrate through Erga's supported interfaces rather than reaching directly into its database from browser code.

### Playwright MCP

Playwright MCP controls a dedicated recruiting browser.

Use a separate browser profile for recruiting. Do not reuse the user's everyday browser profile, password-manager extensions, unrelated sessions, or browser sync.

### Discord

Discord is the remote control surface and human-readable application archive.

A full setup needs:

- a Discord application/bot
- a bot token stored locally
- the owner's numeric Discord user ID stored locally
- the guild/server ID stored locally
- the IDs for the channels/forum/roles used by that installation stored locally

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
```

Every value above is synthetic. Never copy a real local `.env`, OAuth response, Discord ID set, credential file, or token into the repository.

Secrets should come from one of the approved local secret/config mechanisms used by the implementation. Keep the real files outside Git or in ignored local-only paths.

## Local state

Real runtime state belongs outside the repository checkout.

A normal local state root can live under:

```text
~/.config/erga-autopilot/
```

Expected categories include:

```text
config/
secrets/
state/
profile/
browser/
applications/
mail/
logs/
backups/
```

The exact layout should follow the implementation. The important rule is that applicant data, credentials, browser state, receipts, screenshots, logs, and databases are not committed with the source code.

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

## Accounts and external access

Depending on which features are enabled, the user may need:

- a GitHub account for cloning/forking and development
- a Discord account and Discord application/bot
- a Zoho Mail account with API/OAuth access if mail tracking is enabled
- access to the user's normal job-application email account through the supported mail integration

Employer/ATS accounts are created only when an application requires them. Generated employer passwords stay in encrypted local state and never belong in the repository or Discord logs.

## Browser and OS permissions

The local setup may require macOS permissions for the tools that actually need them. Grant the minimum permissions required.

Do not grant broad Full Disk Access, Accessibility, browser profile access, or unrelated filesystem permissions just because an agent asks for them. If a capability genuinely requires a macOS permission, document the exact reason and scope when it is implemented.

## Network requirements

The project is local-first, not offline-only.

A normal run may need outbound network access for:

- Discord
- employer and ATS websites
- public company research
- Zoho APIs when enabled
- GitHub/upstream dependency installation
- model download during setup

The local model server and internal control services should stay bound to localhost unless a reviewed design explicitly says otherwise.

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
- confirm no real applicant data or runtime artifact is staged
- do not bypass secret-scanning or push-protection warnings

Public tests and fixtures must use synthetic people, companies, jobs, email, credentials, Discord IDs, resumes, and application receipts.

`.gitignore` is defense in depth, not permission to keep secrets in the checkout.

## Documentation requirement

This page is part of the implementation contract.

When a change adds, removes, or replaces a runtime dependency, API, credential, environment variable, required account, OS permission, Discord component, model requirement, or setup step, update this page in the same branch.

Keep it general enough for any user to follow. Show public variable names, API names, code paths, and setup concepts. Never document one maintainer's real credentials, IDs, applicant profile, private file paths, or recruiting data.

## Next

- [Getting started](getting-started.md)
- [Will this run on my machine?](hardware-check.md)
- [How it works](how-it-works.md)
- [Security](../SECURITY.md)
- [Prompt injection](prompt-injection.md)
