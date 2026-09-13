# Contributing

Erga Autopilot started as a personal recruiting tool. Contributions are welcome, but changes need to preserve the project's core priorities: correct applicant data, auditable behavior, local-first state, narrow permissions, and no invented resume claims.

## Before opening a pull request

Please read:

- [README.md](README.md)
- [docs/getting-started.md](docs/getting-started.md)
- [docs/how-it-works.md](docs/how-it-works.md)
- [SECURITY.md](SECURITY.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

If your change touches browser submission, candidate memory, credentials, recruiting mail, or MCP permissions, read the security document first.

## Development principles

### Keep business rules outside interfaces

Discord, Hermes, MCP, CLI, and browser code are interfaces around the local core.

Reusable decisions should live in normal typed Python modules rather than being embedded inside Discord callbacks or model prompts.

### The model is not authorization

A prompt or model output cannot grant itself access to a new tool or approve an irreversible action.

Permissions and submission rules belong in code.

### Do not invent candidate facts

Resume and application claims should come from approved profile data or Erga evidence.

If information is unknown, keep it unknown or ask the user.

### Prefer small changes

Keep pull requests focused. Avoid giant refactors mixed with unrelated features.

If a change alters a security boundary or data model, document why.

### Preserve local-first behavior

Do not add a hosted database, telemetry service, or cloud-model dependency as a silent requirement.

Optional integrations should remain optional.

## Setup

The project uses Python and `uv`.

```bash
git clone https://github.com/TransferTrack/erga-autopilot.git
cd erga-autopilot
uv sync
```

As implementation lands, the repository test commands will be documented here and in `docs/getting-started.md`.

The upstream Erga project has a strong verification gate. Changes that retain or modify Erga code should continue to satisfy its relevant formatting, typing, test, packaging, and security checks.

## Test data

Use synthetic data only.

Never commit real:

- resumes;
- applicant profiles;
- addresses or phone numbers;
- employer credentials;
- email contents;
- OAuth tokens;
- browser sessions;
- application databases;
- application receipts;
- screenshots containing personal information.

Synthetic fixtures should look realistic enough to exercise the workflow without representing a real person.

## Browser changes

Browser automation should remain generic first.

Do not add a large ATS-specific framework because one form is inconvenient. If a recurring ATS behavior needs special handling, prefer a small versioned skill or helper with tests.

Prepare-only behavior must remain testable separately from submission.

A change that makes it easier to submit must not make it easier to submit twice.

## Candidate profile changes

The profile schema is versioned code.

Do not let the model create new top-level memory categories at runtime.

Schema changes should include:

- a migration;
- compatibility behavior for old profile versions;
- contradiction handling where relevant;
- tests proving historical application packages keep their original profile version.

## Security-sensitive changes

Changes involving any of the following need explicit tests:

- MCP tools;
- browser permissions;
- uploads;
- navigation/domain rules;
- credentials;
- Discord authorization;
- memory mutation;
- application submission;
- recruiting-mail classification;
- prompt-injection defenses.

If a security control needs to be weakened to make an agent workflow work, stop and explain the tradeoff instead of silently broadening permissions.

## Documentation

Keep documentation plain and specific.

If behavior changes, update the relevant document in the same pull request.

Avoid marketing language that makes experimental behavior sound production-ready.

## Commit style

Use small, readable commits with lower-case messages.

Examples:

```text
docs: explain browser isolation
feat: add profile versioning
fix: prevent duplicate submission retry
test: cover malicious redirect handling
```

Do not include generated-tool or assistant credit in commit messages.

## Pull requests

A good pull request explains:

- what changed;
- why it changed;
- what trust boundary or state model it touches;
- how it was tested;
- what remains intentionally unsupported.

Screenshots are useful for Discord or browser UI changes, but do not include real applicant data.

## Attribution

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp), originally maintained by Adrian (`Adr1an04`) and the Erga contributors under the MIT license.

Keep required upstream notices intact when modifying or redistributing derived code or assets.
