# Contributing

Erga Autopilot started as a personal recruiting tool. Contributions are welcome, but changes should preserve the things the project depends on most: correct applicant data, auditable behavior, local-first state, narrow permissions, and no invented resume claims.

## Before opening a pull request

Read:

- [AGENTS.md](AGENTS.md)
- [README.md](README.md)
- [docs/getting-started.md](docs/getting-started.md)
- [docs/how-it-works.md](docs/how-it-works.md)
- [docs/prompt-injection.md](docs/prompt-injection.md)
- [SECURITY.md](SECURITY.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

If your change touches browser submission, candidate memory, credentials, recruiting mail, or MCP permissions, read the security docs first.

## Keep business rules outside interfaces

Discord, Hermes, MCP, CLI, and browser code are interfaces around the local core.

Reusable decisions should live in typed modules instead of being buried in Discord callbacks or model prompts.

## The model is not authorization

A prompt or model output cannot grant itself access to a new tool or approve an irreversible action.

Permissions and submission rules belong in code.

## Do not invent candidate facts

Resume and application claims should come from approved profile data or Erga evidence.

If information is unknown, keep it unknown or ask the user.

## Keep changes focused

Avoid giant refactors mixed with unrelated features. If a change alters a security boundary or data model, explain why and update the relevant tests and docs.

## Keep local-first behavior

Do not add a hosted database, telemetry service, or cloud-model dependency as a silent requirement.

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

Browser automation should stay generic first.

Do not add a large ATS-specific framework because one form is inconvenient. If a recurring site needs special handling, prefer a small versioned skill or helper with tests.

Prepare-only behavior must remain testable separately from submission.

A change that makes it easier to submit must not make it easier to submit twice.

## Candidate profile changes

The profile schema is versioned code.

Do not let the model create new top-level memory categories at runtime.

Schema changes should include migrations, compatibility behavior for old profiles, contradiction handling where relevant, and tests that preserve the profile version used by historical applications.

## Security-sensitive changes

Changes involving any of the following need explicit tests:

- MCP tools
- browser permissions
- uploads
- navigation or domain rules
- credentials
- Discord authorization
- memory mutation
- application submission
- recruiting-mail classification
- prompt-injection defenses

If a security control needs to be weakened to make a workflow work, explain the tradeoff instead of silently broadening permissions.

## Documentation

When implemented behavior, configuration, Discord layout, security boundaries, or user-facing workflows change, update the relevant docs in the same branch.

Do not document speculative commands as if they already work.

Agents working anywhere in the repo should follow [AGENTS.md](AGENTS.md).

## Commit style

Use small, readable commits with lower-case messages.

Examples:

```text
add profile versioning
prevent duplicate submit retries
cover malicious redirects
explain browser isolation
```

Do not add generated-tool or assistant credit to commit messages.

## Attribution

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp), maintained by Adrian (`Adr1an04`) and the Erga contributors under the MIT license.

Keep required upstream notices intact when modifying or redistributing derived code or assets.
