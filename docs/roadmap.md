# Roadmap

Erga Autopilot is being built in stages. Each stage has to prove itself before the next irreversible capability is turned on.

## 0. Repository foundation

- private development repository
- Erga attribution and license preservation
- architecture docs
- security model
- synthetic fixtures only
- dependency and test baseline

## 1. Mac runtime

- Python / `uv` environment
- Node.js and Playwright MCP
- local directories and permissions
- process health checks
- launch and restart behavior

## 2. Qwen3.8 certification

- Qwen3.8-27B local MLX runtime
- model revision pinning
- context and memory benchmarks
- tool-call and structured-output tests
- browser-plus-model memory-pressure testing
- cancellation and restart tests

## 3. Hermes harness

- local Qwen provider
- recruiting profile
- sessions and runs
- MCP tool filtering
- mode-specific permissions
- approval and audit hooks

## 4. Erga + Autopilot state

- Erga integration
- companion SQLite schema
- profile versions
- application execution state
- Discord bindings
- idempotent synchronization

## 5. Security gate

- prompt-injection fixtures
- browser isolation
- navigation restrictions
- upload allowlists
- credential handling
- duplicate-submission protections
- tool-schema change tests

## 6. Discord control plane

- custom bot
- application forum archive
- lifecycle tags
- detailed timeline comments
- `#action-needed` escalation
- `#memory` interface
- detailed `#system-log` output

## 7. Applicant onboarding

- section-based Discord onboarding
- autosave and resume
- conditional questions
- contradiction detection
- candidate profile approval
- `introduction.md` story interview
- ask-once answer mappings

## 8. Job ingestion and shortlist

- Discord source channels
- source-message backfill
- official job-page capture
- duplicate detection
- hard eligibility rules
- model fit scoring
- shortlist routing

## 9. Resume and research

- Erga evidence catalogue
- validated resume variants
- company research subagent
- cleaner written-response drafting
- evidence checks
- Discord approval flow

## 10. Playwright prepare-only

- dedicated headed browser
- form filling
- account creation
- Zoho email verification
- unknown-question handoff
- complete form flight recorder
- no final submission

## 11. Controlled submission

- frozen application packages
- final review
- explicit submit approval
- exact resume and application receipt
- confirmation capture
- unknown-submission-state recovery

## 12. Recruiting lifecycle

- Zoho acknowledgement tracking
- OA deadlines
- interviews and reschedules
- offers and rejections
- email screenshots
- recruiting reminders and calendar events

## 13. Restricted autopilot

Autopilot only gets to submit when every hard condition is satisfied: an approved profile, a verified job destination, no unresolved facts, no duplicate, no sensitive manual step, approved free text, a validated resume, and a clean security state.

Rollout order:

1. dry run
2. prepare only
3. controlled submit
4. a small monitored autopilot batch
5. restricted autopilot

## 14. Operations

- backups and restore tests
- log retention
- browser-profile health
- model/runtime upgrade evaluation
- dependency audits
- rollback support
- optional future always-on host

## Not part of the first release

- hosted multi-user service
- LinkedIn or Indeed account automation
- required cloud database
- automatic OA completion
- automatic interviews
- automatic offer acceptance or rejection
- access to the user's normal browser profile
- broad shell or filesystem access during application mode
