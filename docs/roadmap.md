# Roadmap

The project is staged on purpose. Each step needs a clear exit gate before the next irreversible capability is turned on.

## 0. Repository foundation

- preserve Erga attribution and license notices
- write the architecture and security docs
- document the memory/storage split
- keep real runtime data and the private Obsidian vault outside Git
- use synthetic fixtures only
- add secret scanning and a defensive `.gitignore`

## 1. Mac runtime

- Python and `uv`
- Node.js and Playwright MCP
- local runtime directories and permissions
- private Obsidian vault path/config
- process health checks
- launch and restart behavior

## 2. Qwen3.8 certification

- local Qwen3.8-27B runtime
- model revision pinning
- context and memory benchmarks
- tool-call and structured-output tests
- browser-plus-model memory testing
- cancellation and restart tests

## 3. Hermes + memory foundation

- local Qwen provider
- recruiting profile
- sessions and runs
- MCP tool filtering
- mode-specific permissions
- approval and audit hooks
- bounded Hermes hot memory
- Obsidian vault integration
- QMD local retrieval/indexing
- controlled canonical profile/memory writes

## 4. Erga + transactional Autopilot state

- Erga integration
- companion SQLite schema for transactional state only
- source/job checkpoints and deduplication
- onboarding/session checkpoints
- application execution state
- browser runs
- submission attempts and unknown-submit recovery
- Discord/Zoho bindings
- queues/outbox/idempotent synchronization
- frozen profile snapshot references and hashes

## 5. Security gate

- prompt-injection fixtures
- browser isolation
- navigation restrictions
- upload allowlists
- credential handling
- vault write boundaries
- research-vs-authoritative-memory separation
- duplicate-submission protections
- secret scanning
- tool-schema change tests

## 6. Discord control plane

- custom bot
- application forum archive
- lifecycle tags
- detailed timeline comments
- `#action-needed` escalation
- `#memory` interface backed by validated local memory operations
- detailed `#system-log` output

## 7. Applicant onboarding

- section-based Discord onboarding
- SQLite-backed autosave and resume for in-progress sessions
- conditional questions
- contradiction detection
- candidate profile approval
- validated writes into the private Obsidian profile
- immutable approved profile snapshots/hashes
- story interview under the vault's story area
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
- research notes in non-authoritative vault areas
- QMD retrieval over approved memory/research
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
- frozen application package built from approved profile/evidence
- no final submission

## 11. Controlled submission

- frozen application packages
- final review
- explicit submit approval
- exact resume and answer archive
- confirmation capture
- transactional submission attempt tracking
- unknown-submission-state recovery

## 12. Recruiting lifecycle

- application acknowledgements
- OA deadlines
- interviews and reschedules
- offers and rejections
- email evidence
- recruiting reminders and calendar events
- durable company/application notes where useful

## 13. Restricted autopilot

Autopilot may submit only when the hard conditions are satisfied: approved profile snapshot, verified destination, no unresolved fact, no duplicate, no sensitive/manual step, approved free text, validated resume, and clean security state.

Rollout order:

1. dry run
2. prepare only
3. controlled submit
4. tiny monitored autopilot batch
5. restricted autopilot

## 14. Operations

- backups and restore tests
- Obsidian vault backup policy
- SQLite backup/restore tests
- QMD reindex/rebuild test
- artifact integrity/hash checks
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
- cloud sync requirement for the Obsidian vault
- automatic OA completion
- automatic interviews
- automatic offer acceptance or rejection
- access to the user's normal browser profile
- broad shell or filesystem access during application mode
- using Markdown/Obsidian as the submission transaction engine
