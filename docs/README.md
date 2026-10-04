# Documentation

Erga Autopilot is a personal local-first recruiting system, not a hosted service.

If you're new here, start with these:

- [Getting started](getting-started.md) for local setup, requirements, and the first safe run
- [Hardware sanity check](hardware-check.md) for checking whether your machine can run the current stack and what to tune if it cannot run the defaults comfortably
- [How it works](how-it-works.md) for the architecture and day-to-day behavior
- [Security](../SECURITY.md) for permissions, credentials, prompt injection, and submission safety
- [Prompt injection](prompt-injection.md) for concrete attack examples, tool boundaries, and the tests that should block autopilot when those boundaries fail
- [Roadmap](roadmap.md) for the staged path from local setup to restricted autopilot
- [Contributing](../CONTRIBUTING.md) for development rules and pull-request expectations
- [Third-party notices](../THIRD_PARTY_NOTICES.md) for Erga attribution and other dependency notes

## Current status

The project is experimental. Some docs describe behavior that is planned but not implemented on `main` yet.

If the docs and code disagree, the code and current tests win. Please open an issue if a document has gone stale.

## Writing docs

Be clear about what works now, what is still planned, and what is not safe to automate yet.

Do not describe future autopilot behavior as ready before prepare-only and controlled submission have passed their test gates.
