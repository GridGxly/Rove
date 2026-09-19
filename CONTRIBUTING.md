# Contributing

Erga Autopilot started as a personal recruiting tool. Contributions are welcome, but changes should preserve the things the project depends on most: correct applicant data, auditable behavior, local-first state, narrow permissions, and no invented resume claims.

## Before opening a pull request

Read:

- [AGENTS.md](AGENTS.md)
- [README.md](README.md)
- [docs/getting-started.md](docs/getting-started.md)
- [docs/requirements.md](docs/requirements.md)
- [docs/memory-and-storage.md](docs/memory-and-storage.md)
- [docs/how-it-works.md](docs/how-it-works.md)
- [docs/discord.md](docs/discord.md)
- [docs/prompt-injection.md](docs/prompt-injection.md)
- [SECURITY.md](SECURITY.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

If your change touches browser submission, candidate memory, Obsidian/QMD, credentials, recruiting mail, or MCP permissions, read the security docs first.

## Keep business rules outside interfaces

Discord, Hermes, MCP, CLI, Obsidian, and browser code are interfaces around the local core.

Reusable decisions should live in typed modules instead of being buried in Discord callbacks, model prompts, or arbitrary Markdown parsing.

## The model is not authorization

A prompt, note, retrieval result, or model output cannot grant itself access to a new tool or approve an irreversible action.

Permissions and submission rules belong in code.

## Do not invent candidate facts

Resume and application claims should come from approved profile data or Erga evidence.

If information is unknown, keep it unknown or ask the user.

Research notes and QMD results are not candidate facts unless they point back to an approved authoritative source.

## Keep changes focused

Avoid giant refactors mixed with unrelated features. If a change alters a security boundary, storage role, or data model, explain why and update the relevant tests and docs.

## Keep local-first behavior

Do not add a hosted database, telemetry service, cloud-model dependency, or cloud sync product as a silent requirement.

Optional integrations should stay optional.

## Setup

```bash
git clone https://github.com/GridGxly/erga-autopilot.git
cd erga-autopilot
uv sync
```

As implementation lands, keep verified test commands here or in [Getting started](docs/getting-started.md).

## Public repo and test data

Treat anything committed here as public.

The author's name can appear where attribution belongs, such as the README, license, and notices. Applicant examples should remain synthetic.

Never commit real:

- resumes or cover letters
- applicant profiles
- Obsidian vault contents or exports from a real setup
- QMD indexes containing private content
- addresses, phone numbers, or personal email addresses
- dates of birth or demographic answers
- work-authorization or sponsorship answers
- Discord guild, channel, role, or user IDs
- recruiting email
- employer credentials
- OAuth tokens
- browser sessions
- application databases or receipts
- screenshots containing personal information

Use synthetic fixtures that look realistic enough to exercise the workflow without representing a real person.

## Secrets

Do not rely on `.gitignore` alone.

Before committing, inspect the diff and run the repository's secret checks. Do not bypass secret-scanning or push-protection warnings just to make a push succeed.

If a real secret is committed, revoke or rotate it before cleaning up the Git history.

## Browser changes

Read [docs/browser-automation.md](docs/browser-automation.md) before changing browser observation, execution, field resolution, batching, waits, ATS adapters, or submission verification.

Browser automation should stay generic first, but generic does not mean model-driven one field at a time.

Prefer changes that reduce unnecessary browser/model round trips while preserving observable state and verification:

- compact structured form observation
- deterministic field resolution from the frozen application package
- safe batch filling
- targeted event/state waits
- post-fill value verification
- narrow versioned ATS adapters when repeated evidence justifies them

Do not add a large ATS-specific framework because one form is inconvenient. If a recurring site needs special handling, prefer a small versioned adapter or helper with tests.

Do not add a required cloud browser-decision model to solve latency. Qwen3.8-27B remains the reference local reasoning model, and routine browser mechanics belong in normal code.

Prepare-only behavior must remain testable separately from submission.

A change that makes it easier to submit must not make it easier to submit twice.

## Candidate profile and memory changes

The approved profile is represented in the private semantic memory layer but remains schema-driven and versioned.

Do not let the model create new top-level memory categories at runtime.

Canonical profile writes should go through explicit profile/memory operations. Research agents should not get generic write access to authoritative profile areas.

Manual Obsidian edits are allowed, but code must validate relevant notes before using them in an application.

A profile/storage change should include the pieces relevant to it, such as:

- schema/version changes
- compatibility behavior for older notes/snapshots
- contradiction handling
- tests for manual-edit validation
- tests proving research/QMD results cannot silently become candidate facts
- tests that historical application packages keep the exact approved profile snapshot/hash they used

## SQLite changes

Autopilot SQLite is for transactional machine state, not the main semantic knowledge base.

Good reasons to add a table/field include deduplication, exact workflow state, crash recovery, queues, idempotency, bindings, submission attempts, or artifact metadata.

Do not move readable long-term user knowledge back into SQLite merely because adding a table is convenient.

## Obsidian and QMD changes

Treat the vault as private runtime data.

QMD is an index over the vault, not the source of truth.

Changes to vault layout, canonical note schemas, write permissions, retrieval behavior, or QMD setup should update:

- [Memory and storage](docs/memory-and-storage.md)
- [Requirements to run](docs/requirements.md) when setup/dependencies change
- security tests when authority/write boundaries change

If a contribution broadens what an agent can write inside the vault, explain the new trust boundary explicitly.

## Security-sensitive changes

Changes involving any of the following need explicit tests:

- MCP tools
- browser permissions
- uploads
- navigation or domain rules
- credentials
- Discord authorization
- memory/vault mutation
- QMD/retrieval behavior
- application submission
- recruiting-mail classification
- prompt-injection defenses

If a security control needs to be weakened to make a workflow work, explain the tradeoff instead of silently broadening permissions.

## Documentation

When implemented behavior, configuration, Discord layout, memory/storage architecture, security boundaries, dependencies, or user-facing workflows change, update the relevant docs in the same branch.

Do not document speculative commands as if they already work.

Agents working anywhere in the repo should follow [AGENTS.md](AGENTS.md).

## Commit style

Use small, readable commits with lower-case messages.

Examples:

```text
add profile snapshot validation
prevent duplicate submit retries
cover malicious redirects
explain browser isolation
```

Do not add generated-tool or assistant credit to commit messages.

## Attribution

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp), maintained by Adrian (`Adr1an04`) and the Erga contributors under the MIT license.

Keep required upstream notices intact when modifying or redistributing derived code or assets.
