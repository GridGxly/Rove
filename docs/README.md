# Documentation

Rove is a personal, local-first recruiting system. It is not a hosted service.

## Understand it

- [How it works](how-it-works.md): the components, the path of one application, and who decides what
- [Application workflow](application-workflow.md): intake, job fit, resume, answers, stops, sending, unattended sending, recruiting mail, limits, and work in progress
- [Discord](discord.md): channels, cards, the replies Rove accepts, the memory channel, the system log and forum tags
- [Browser automation](browser-automation.md): the recruiting Chrome, observation, field matching, pickers, multi-page forms and debugging evidence
- [Memory and storage](memory-and-storage.md): the vault, QMD, SQLite, private files, Erga and credentials
- [Product requirements](prd.md): what "done" means, with the status of each requirement

## Set it up

- [Requirements](requirements.md): software, configuration keys, the Discord bot, Zoho, services and network access
- [Getting started](getting-started.md): the setup steps in order
- [Local runtime](local-runtime.md): tested versions, model and Hermes settings, verification commands
- [Runtime measurements](runtime-benchmarks.md): what was measured on the reference Mac
- [Qwen visual benchmark](visual-benchmark.md): actual game attempts, failures, evidence, and the MVP acceptance gate
- [Will this run on my machine?](hardware-check.md): a prompt for checking other hardware
- [Onboarding and jobs](onboarding-and-jobs.md): the profile interview, approval, the job catalog and the Hermes tool list

## Keep it safe

- [Security](../SECURITY.md): the threat model, credentials, browser isolation and submission safety
- [Prompt injection](prompt-injection.md): why outside content cannot grant permission
- [Contributing](../CONTRIBUTING.md): rules for changes
- [Third-party notices](../THIRD_PARTY_NOTICES.md): Erga attribution and other licenses

## Ground rules for these docs

Code and tests are the source of truth. A page describes behavior that exists, and says so plainly when something is not built. When a change alters behavior, configuration, storage or a security boundary, the matching page changes in the same branch.

Everything committed here is public. Examples use synthetic people, companies, IDs and paths. Real applicant data and the real vault stay outside the checkout.
