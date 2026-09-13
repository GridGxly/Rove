# Documentation

Erga Autopilot is being documented as a personal local-first recruiting system rather than a hosted service.

Start here:

- [Getting started](getting-started.md) — local setup, requirements, and safe first run.
- [How it works](how-it-works.md) — architecture and product behavior.
- [Security](../SECURITY.md) — trust boundaries, credentials, prompt injection, and submission safety.
- [Roadmap](roadmap.md) — phased rollout from local runtime to restricted autopilot.
- [Contributing](../CONTRIBUTING.md) — development rules and pull-request expectations.
- [Third-party notices](../THIRD_PARTY_NOTICES.md) — Erga attribution and dependency notices.

## Current status

The repository is experimental. Documentation may describe target behavior that is not yet implemented on `main`.

When code and documentation disagree, treat the code and current tests as the actual behavior and open an issue for stale documentation.

## Documentation rules

Documentation should stay specific about what is implemented, what is planned, and what remains unsafe to automate.

Avoid presenting future autopilot behavior as ready before the prepare-only and controlled-submission stages have passed their test gates.
