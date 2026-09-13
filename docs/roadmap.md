# Roadmap

The project is staged on purpose. Each step needs a clear exit gate before the next irreversible capability is turned on.

## 0. Repository foundation

- preserve Erga attribution and license notices
- write the architecture and security docs
- keep real runtime data outside Git
- use synthetic fixtures only
- add secret scanning and a defensive `.gitignore`

## 1. Mac runtime

- Python and `uv`
- Node.js and Playwright MCP
- local runtime directories and permissions
- process health checks
- launch and restart behavior

## 2. Qwen3.8 certification

- local Qwen3.8-27B runtime
- model revision pinning
- context and memory benchmarks
- tool-call and structured-output tests
- browser-plus-model memory testing
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
- secret scanning
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
- company research agent
- written-response drafting
- evidence checks
- Discord approval flow

## 10. Playwright prepare-only

- dedicated headed browser
- form filling
- account creation
- email verification
- unknown-question handoff
- complete form flight recorder
- no final submission

## 11. Controlled submission

- frozen application packages
- final review
- explicit submit approval
- exact resume and answer archive
- confirmation capture
- unknown-submission-state recovery

## 12. Recruiting lifecycle

- application acknowledgements
- OA deadlines
- interviews and reschedules
- offers and rejections
- email evidence
- recruiting reminders and calendar events

## 13. Restricted autopilot

Autopilot may submit only when the hard conditions are satisfied: approved profile, verified destination, no unresolved fact, no duplicate, no sensitive/manual step, approved free text, validated resume, and clean security state.

Rollout order:

1. dry run
2. prepare only
3. controlled submit
4. tiny monitored autopilot batch
5. restricted autopilot

## 14. Operations

- backups and restore tests
- log retention
- browser-profile health
- model/runtime upgrade evaluation
- dependency audits
- rollback support
- optional future always-on host

## Not in the first release

- hosted multi-user service
- LinkedIn or Indeed account automation
- cloud database requirement
- automatic OA completion
- automatic interviews
- automatic offer acceptance or rejection
- access to the user's normal browser profile
- broad shell or filesystem access during application mode
