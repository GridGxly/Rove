# Documentation agent notes

These instructions apply to work inside `docs/`.

## Keep the docs tied to reality

Read the current code, config, tests, and relevant docs before changing documentation. If the implementation and an older document disagree, describe what the code actually does and call out anything that is still planned.

Do not turn a roadmap item into a setup instruction before the feature works. When implementation changes behavior, update the docs that explain that behavior in the same branch.

## This is a public repo

Assume anything committed here can be read by anyone.

The maintainer's name may appear where attribution belongs. Applicant examples and runtime data must be synthetic.

Do not put real values from a local setup into documentation, examples, screenshots, fixtures, or sample config. That includes:

- personal email addresses, phone numbers, addresses, or dates of birth
- resumes or application answers
- Discord user, guild, channel, forum, or role IDs
- Zoho account IDs or recruiting email
- passwords, tokens, cookies, OAuth credentials, verification codes, or encryption keys
- browser state, application receipts, traces, logs, or screenshots with personal data

Use obvious synthetic examples such as `Alex Rivera`, `alex@example.com`, `Example University`, and `Example Corp`.

The public repository URL used in docs is `https://github.com/GridGxly/erga-autopilot`.

## Preserve the project boundaries

Do not casually rewrite settled architecture while editing docs. The current design is local-first and uses Qwen, Hermes Agent, Erga, Playwright MCP, Discord, SQLite, and optional Zoho integration. If the implementation later changes one of those decisions, update the docs after verifying the change.

Keep Erga attribution intact. Erga Autopilot is an independent project built on Erga's foundation; it is not an official Erga project.

Security rules are load-bearing. Do not soften requirements around prompt injection, browser isolation, credentials, memory writes, sensitive data, or submission safety just to make the prose shorter.

## Write like a person

Prefer plain, concrete language over marketing copy or agent jargon. Keep useful technical terms when they are the clearest words for the job.

Avoid filler, fake certainty, repetitive summaries, and formulaic phrasing. Do not make experimental behavior sound production-ready.

When a command, version, hardware requirement, path, or configuration value can change, verify it before documenting it.

## Examples and hardware guidance

Hardware advice must account for the whole running stack, not just whether model weights fit in memory. Do not invent benchmark numbers. If performance has not been measured, say so and give the reader a way to test it.

Any example that looks like an application, email, resume, profile, Discord event, or browser trace must be synthetic.

## Commits

Keep documentation commits focused and use short lower-case commit messages that describe what actually changed.

Do not add assistant, model, generated-by, or tool credit to commits or documentation.
