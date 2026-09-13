# Documentation

Erga Autopilot is a personal, local-first recruiting system. It is not a hosted service.

If you're new here, start with these:

- [Getting started](getting-started.md) for local setup and the first safe run
- [Requirements to run](requirements.md) for the full software, service, API, account, secret, browser, and network checklist
- [Will this run on my machine?](hardware-check.md) for checking your hardware and tuning a clone or fork
- [How it works](how-it-works.md) for the architecture and application flow
- [Prompt injection](prompt-injection.md) for the trust model around untrusted pages, email, and tool output
- [Security](../SECURITY.md) for permissions, credentials, browser isolation, and submission safety
- [Roadmap](roadmap.md) for the build order
- [Contributing](../CONTRIBUTING.md) for development rules
- [Third-party notices](../THIRD_PARTY_NOTICES.md) for Erga attribution

## Current status

The project is experimental. Some documents describe the system being built toward rather than behavior already available on every branch.

If the docs and code disagree, the code and current tests win. When implemented behavior, configuration, security boundaries, dependencies, APIs, or user-facing workflows change, update the relevant docs in the same change.

## Public examples

Treat everything committed to the repository as public.

Docs, examples, screenshots, fixtures, sample resumes, sample email, and application receipts must use synthetic people and synthetic data. Real applicant data belongs outside the Git checkout.
