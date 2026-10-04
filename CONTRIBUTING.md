# Contributing

Rove started as a personal recruiting tool. Contributions are welcome, but changes should preserve the things the project depends on most: correct applicant data, auditable behavior, local-first state, narrow permissions, and no invented resume claims.

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
git clone https://github.com/GridGxly/Rove.git
cd Rove
uv sync
```

As implementation lands, keep verified test commands here or in [Getting started](docs/getting-started.md).

## Checks

Pushes to `main` and `docs-initial-setup`, and every pull request, run
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). Each job below publishes a
separate check. GitHub branch protection or a ruleset must require those checks before
merging; the workflow file alone does not enforce that. Run `uv sync` first; the browser
checks also need `uv run patchright install chromium` once.

| Check | What it holds | Run it locally |
| --- | --- | --- |
| `lint` | The ruff rules selected in `pyproject.toml`, and formatting. | `uv run ruff check .` and `uv run ruff format --check .` |
| `quality` | Rules the older code still breaks, counted per file and held at the ceilings in `scripts/baselines/quality.json`; a reason on every suppressed blind `except` or security finding (`# noqa: BLE001 -- why`); no block of 8 or more lines copied between modules. | `uv run python scripts/ratchet.py quality` and `uv run pylint src/rove` |
| `types` | mypy findings, counted per file and error code and held at `scripts/baselines/types.json`. | `uv run python scripts/ratchet.py types` |
| `tests` | Every test that needs no browser, in random order, with branch coverage. No browser is installed for it. | `uv run pytest -m "not e2e and not performance"` |
| `e2e` | Every test that drives a fixture Chromium, including the end-to-end scenarios. | `uv run pytest -m "e2e and not performance"` |
| `coverage` | Branch coverage of `tests` and `e2e` together, at or above `fail_under` in `pyproject.toml`. | `uv run pytest -m "not performance" --cov=rove --cov-branch` |
| `smoke` | The sdist and wheel build; the wheel installs into a fresh virtualenv; `rove --help`, every subcommand's `--help`, every module's import and `rove bench fixture` work from it; the app icon ships. | `uv run pytest -m smoke` |
| `performance` | The offline fixture application against the time and call budgets in `tests/test_performance.py`. | `uv run pytest -m performance` |
| `hygiene` | No secret, private file, real Discord ID, personal address or machine path in any tracked file; relative links in the Markdown resolve; every `config/workflow.json` key the code reads is in [Requirements](docs/requirements.md#configworkflowjson). | `uv run python scripts/check_staged.py --all` and `uv run pytest tests/test_repo_hygiene.py` |
| `macos` | The whole suite on macOS, where Rove runs. | `uv run pytest -m "smoke or not smoke"` |

A plain `uv run pytest` runs everything except `smoke`, which builds and installs the package and so runs only when the `-m` expression names it.

What the suite enforces on every run:

- **Isolation.** Real httpx transports may reach only loopback fixtures. An attempted external request fails teardown even if application code caught its exception; external services use explicit mock transports. Private paths and keys are synthetic.
- **Employer receipt.** The performance and installed-wheel fixture sends actual form data and resume bytes to a validating local server. An empty POST, wrong value, missing field, duplicate field or repeated submission must fail. A timing row alone is not a successful application.
- **Crash recovery.** Submission tests interrupt database transactions, fail receipt writes, race conflicting outcomes and abruptly exit a separate process after commit. Recovery must preserve the one recorded result, restore its receipt and avoid resending or replacing a newer question. Observe committed records through an independent connection instead of asserting that an internal helper was called.
- **Order.** Tests run in a random order (pytest-randomly). The seed is printed at the top; `-p randomly --randomly-seed=N` repeats a run and `-p no:randomly` turns shuffling off while you debug. A test that passes only in one order has an isolation bug.
- **Browser tests are found, not remembered.** A test that starts a Chromium, directly or through a fixture or helper under `tests/`, is marked `e2e` when the suite is collected (`tests/ci_marks.py`). A test without the marker that still starts one fails and names itself; add `@pytest.mark.e2e` to it.
- **Hangs fail fast.** Each test has 120 seconds (pytest-timeout).
- **Hidden failures are failures.** An exception raised in a thread or a finalizer, a file or socket left open, and a coroutine never awaited all fail the test. Unknown markers and unknown config keys are errors, and an `xfail` test that passes fails.

Ceilings and floors only move one way:

- **Coverage floor.** When a change raises total coverage, set `fail_under` in `pyproject.toml` to the new total minus two points, in the same pull request. Never lower it.
- **Lint and type ceilings.** When you fix findings, run `uv run python scripts/ratchet.py quality --update` (or `types --update`) and commit the lower numbers; the check fails until you do, so an improvement cannot be spent later. A new finding fails the check. `--update --allow-increase` exists for a reviewed exception and shows in the diff as a larger number. When a rule reaches zero everywhere, remove it from `ignore` and `external` in `[tool.ruff.lint]` so `lint` enforces it.
- **Performance budgets.** Time budgets are generous multiples of measured medians and catch regressions like a fixed sleep per field, not drift. Call counts are exact or ceilings. When a change moves one on purpose, update the table in `tests/test_performance.py` and say why in the commit.
- **Configuration keys.** A new `config/workflow.json` key needs a row in the table in `docs/requirements.md`. Keys that were undocumented when the check was added are listed in `tests/test_repo_hygiene.py`; document one and remove it from that list.

## Chat changes

The owner talks to Rove in `agent-control` however he likes: lowercase, typos, slang. Understanding him is the model's job. Code takes only exact forms (a message that is nothing but links, and the few requests on the pinned help) and otherwise checks facts the model passes to its tools: a link must be in a message he wrote, a name must match one of his applications, an answer must be in his own words. Do not add word lists that decide what he meant.

A change to the persona (`integrations/hermes/SOUL.md`), a chat tool or its description is measured against the real model:

```bash
uv run python scripts/chat_eval.py
uv run python scripts/chat_eval.py --match tesla --verbose
```

It runs every message in `tests/chat_eval/phrases.jsonl` through the Hermes agent as the gateway builds it, with Rove's tools replaced by stubs that only record the call, so no state, Discord or browser is touched. It prints each miss and the pass rate; keep it at 90 % or above. It needs the local model and Hermes, takes several minutes, and is run by hand, not in CI. Add a phrase for every way he was misunderstood. The offline suite checks the tools themselves.

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

Rove SQLite is for transactional machine state, not the main semantic knowledge base.

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

Rove builds on [Erga](https://github.com/Adr1an04/erga-mcp), maintained by Adrian (`Adr1an04`) and the Erga contributors under the MIT license.

Keep required upstream notices intact when modifying or redistributing derived code or assets.
