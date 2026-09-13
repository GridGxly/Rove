# Contributing

Erga Autopilot started as a personal recruiting tool. Contributions are welcome, but please keep the things that matter most here intact: correct applicant data, traceable behavior, local state, narrow permissions, and no made-up resume claims.

## Read these first

Before opening a pull request, read:

- [README.md](README.md)
- [docs/getting-started.md](docs/getting-started.md)
- [docs/how-it-works.md](docs/how-it-works.md)
- [SECURITY.md](SECURITY.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

If your change touches browser submission, candidate memory, credentials, recruiting mail, or MCP permissions, read the security doc before you start changing code.

## A few rules that keep the project sane

### Keep business rules out of the UI

Discord, Hermes, MCP, CLI, and browser code are interfaces around the local core.

Reusable decisions belong in normal typed Python modules, not buried in a Discord callback or a model prompt.

### The model does not authorize itself

A prompt or model output cannot grant a new permission or approve an irreversible action.

Permissions and submission rules belong in code.

### Do not invent candidate facts

Resume and application claims should come from approved profile data or Erga evidence.

If something is unknown, leave it unknown or ask the user.

### Keep changes focused

Small pull requests are much easier to reason about here than giant refactors.

If a change alters a security boundary or data model, explain why and add the tests that prove the new behavior.

### Keep local-first behavior local-first

Do not make a hosted database, telemetry service, or cloud model a silent requirement.

Optional integrations should stay optional.

## Setup

The project uses Python and `uv`.

```bash
git clone https://github.com/TransferTrack/erga-autopilot.git
cd erga-autopilot
uv sync
```

As more implementation lands, the test commands will be kept here and in [Getting started](docs/getting-started.md).

Erga already has a strong verification gate. If your change keeps or modifies Erga code, keep the relevant formatting, typing, test, packaging, and security checks passing.

## Test data

Use synthetic data only.

Never commit real:

- resumes
- applicant profiles
- addresses or phone numbers
- employer credentials
- email contents
- OAuth tokens
- browser sessions
- application databases
- application receipts
- screenshots containing personal information

Fake data should still be realistic enough to exercise the workflow.

## Browser changes

Start generic.

Do not build a large ATS-specific framework because one form is annoying. If a recurring ATS behavior needs special handling, add a small versioned skill or helper with tests.

Prepare-only mode must stay independently testable from submission.

Any change that makes submission easier must not make duplicate submission easier too.

## Candidate profile changes

The profile schema is versioned code.

Do not let the model create new top-level memory categories at runtime.

A schema change should include:

- a migration
- compatibility behavior for older profile versions
- contradiction handling when it applies
- tests proving historical application packages keep the profile version they originally used

## Security-sensitive changes

Changes in these areas need explicit tests:

- MCP tools
- browser permissions
- uploads
- navigation and domain rules
- credentials
- Discord authorization
- memory mutation
- application submission
- recruiting-mail classification
- prompt-injection defenses

If a workflow only works after weakening a security control, stop and explain the tradeoff. Do not quietly broaden permissions to make the agent happy.

## Documentation

Keep the docs plain and specific.

If behavior changes, update the relevant doc in the same pull request.

Do not make experimental behavior sound finished before it is.

## Commit style

Use small commits and keep commit messages lower-case.

Good examples:

```text
explain browser isolation
add profile versioning
stop duplicate submission retries
cover malicious redirects
```

Do not add generated-tool or assistant credit to commit messages.

## Pull requests

A useful pull request should answer:

- what changed?
- why?
- which trust boundary or state model does it touch?
- how was it tested?
- what is still intentionally unsupported?

Screenshots help for Discord or browser UI changes, but use synthetic data.

## Attribution

Erga Autopilot builds on [Erga](https://github.com/Adr1an04/erga-mcp), maintained by Adrian (`Adr1an04`) and the Erga contributors under the MIT license.

Keep the required upstream notices when modifying or redistributing derived code or assets.
