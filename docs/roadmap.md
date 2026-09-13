# Roadmap

Erga Autopilot is intentionally being built in stages. Each stage should have a hard exit gate before the next irreversible capability is enabled.

## 0. Repository foundation

- private development repository;
- Erga attribution and license preservation;
- architecture docs;
- security model;
- synthetic fixtures only;
- dependency and test baseline.

## 1. Mac runtime

- Python / `uv` environment;
- Node.js and Playwright MCP;
- local directories and permissions;
- process health checks;
- launch/restart behavior.

## 2. Qwen3.8 certification

- Qwen3.8-27B local MLX runtime;
- model revision pinning;
- context and memory benchmarks;
- tool-call and structured-output tests;
- browser-plus-model memory-pressure testing;
- cancellation and restart tests.

## 3. Hermes harness

- local Qwen provider;
- recruiting profile;
- sessions and runs;
- MCP tool filtering;
- mode-specific permissions;
- approval and audit hooks.

## 4. Erga + Autopilot state

- Erga integration;
- companion SQLite schema;
- profile versions;
- application execution state;
- Discord bindings;
- idempotent synchronization.

## 5. Security gate

- prompt-injection fixtures;
- browser isolation;
- navigation restrictions;
- upload allowlists;
- credential handling;
- duplicate-submission protections;
- tool-schema change tests.

## 6. Discord control plane

- custom bot;
- application forum archive;
- lifecycle tags;
- detailed timeline comments;
- `#action-needed` escalation;
- `#memory` interface;
- detailed `#system-log` output.

## 7. Applicant onboarding

- section-based Discord onboarding;
- autosave and resume;
- conditional questions;
- contradiction detection;
- candidate profile approval;
- `introduction.md` story interview;
- ask-once answer mappings.

## 8. Job ingestion and shortlist

- Discord source channels;
- source-message backfill;
- official job-page capture;
- duplicate detection;
- hard eligibility rules;
- model fit scoring;
- shortlist routing.

## 9. Resume and research

- Erga evidence catalogue;
- validated resume variants;
- company research subagent;
- humanized written-response drafting;
- evidence checks;
- Discord approval flow.

## 10. Playwright prepare-only

- dedicated headed browser;
- form filling;
- account creation;
- Zoho email verification;
- unknown-question handoff;
- complete form flight recorder;
- no final submission.

## 11. Controlled submission

- frozen application packages;
- final review;
- explicit submit approval;
- exact resume/archive receipt;
- confirmation capture;
- unknown-submission-state recovery.

## 12. Recruiting lifecycle

- Zoho acknowledgement tracking;
- OA deadlines;
- interviews and reschedules;
- offers and rejections;
- email screenshots;
- recruiting reminders and calendar events.

## 13. Restricted autopilot

Autopilot may submit only when all hard conditions are satisfied, including an approved profile, verified job destination, no unresolved fact, no duplicate, no sensitive/manual step, approved free text, validated resume, and a clean security state.

Rollout order:

1. dry run;
2. prepare only;
3. controlled submit;
4. tiny monitored autopilot batch;
5. restricted autopilot.

## 14. Operations

- backups and restore tests;
- log retention;
- browser-profile health;
- model/runtime upgrade evaluation;
- dependency audits;
- rollback support;
- optional future always-on host.

## Non-goals for the first release

- hosted multi-user service;
- LinkedIn/Indeed account automation;
- cloud database requirement;
- automatic OA completion;
- automatic interviews;
- automatic offer acceptance/rejection;
- access to the user's normal browser profile;
- broad shell/filesystem access during application mode.
