# How Erga Autopilot works

Erga Autopilot is a local recruiting agent. Qwen handles the parts that need judgment. Normal code owns permissions, validation, state transitions, and irreversible actions.

It is built for one person running it on their own machine, not as a hosted job-application service or multi-user backend.

## System map

```text
Discord
  │
  ▼
Autopilot
  │
  ├── Hermes Agent
  │     ├── Qwen3.8-27B
  │     └── small hot memory
  │
  ├── Obsidian vault
  │     ├── profile / preferences
  │     ├── story / narrative context
  │     ├── companies / research
  │     └── decisions / long-term notes
  │          └── QMD local retrieval index
  │
  ├── Erga
  │     ├── career evidence
  │     ├── project / Git evidence
  │     ├── resume generation
  │     └── application lifecycle
  │
  ├── Autopilot SQLite
  │     ├── job + source checkpoints
  │     ├── browser/application runs
  │     ├── submission attempts
  │     ├── Discord / Zoho bindings
  │     └── queues / idempotency
  │
  ├── private files
  │     ├── resumes
  │     ├── frozen application packages
  │     ├── receipts
  │     ├── screenshots
  │     └── traces
  │
  ├── fast browser runtime
  │     ├── compact form/DOM observation
  │     ├── deterministic field resolution + batching
  │     └── dedicated Playwright/Chromium recruiting browser
  │
  └── Zoho
        └── recruiting mail and verification events
```

## Qwen handles ambiguity

Qwen is useful when the answer is not a simple lookup.

Examples:

- deciding whether a job is worth applying to
- interpreting unfamiliar application wording
- selecting relevant experience
- researching a company
- recovering from a browser validation problem
- drafting a written response
- making sense of an ambiguous recruiting email

Qwen is not the source of truth for applicant facts and it is not the authority for irreversible actions.

Exact applicant facts come from validated local memory/evidence. Submission permission comes from code and approved policy.

## Hermes runs the agent loop

Hermes sits between Qwen and the tools.

It provides sessions, MCP integration, skills, subagents, and the loop that lets Qwen reason, call a tool, inspect the result, and continue.

Hermes built-in memory is intentionally small. `MEMORY.md` and `USER.md` are useful for compact session-start context such as stable preferences, project conventions, environment facts, tool quirks, and pointers to deeper local knowledge.

They are not the full recruiting knowledge base.

Autopilot changes the available tools depending on what the agent is doing.

```text
ONBOARDING
JOB_REVIEW
RESEARCH
RESUME
APPLICATION_PREPARE
APPLICATION_SUBMIT
MAIL_REVIEW
MANUAL_TAKEOVER
```

The same model can work in each mode. The permissions change.

For example:

- onboarding can collect answers and write approved values through the validated profile flow but cannot redesign the profile schema
- research can browse public sources and write non-authoritative research notes where allowed but cannot submit an application or mutate canonical profile facts
- application preparation can fill a form using a frozen approved profile snapshot but cannot use final submission tools until policy allows it
- submission can act only on the validated frozen application package it was given

## Obsidian is the long-term semantic memory

The private Obsidian vault is the main human-readable memory layer.

It stores knowledge that benefits from being readable, searchable, linkable, and editable by the user:

- approved candidate profile
- application preferences and policies
- story/narrative context
- company notes
- research
- decisions and lessons learned
- long-term application notes
- career context

A reference layout may look like:

```text
Erga Autopilot/
├── Profile/
├── Story/
├── Career/
├── Companies/
├── Applications/
├── Research/
├── Decisions/
├── Daily/
└── System/
```

The vault lives outside the Git checkout.

The public config may reference `OBSIDIAN_VAULT_PATH`; the user's real path stays local.

## Obsidian is readable, not permissionless

A Markdown file becoming easy to edit does not make arbitrary text authoritative.

Canonical candidate/profile notes should use a schema that code can validate. Frontmatter can carry stable metadata such as schema version, profile version, approval state, and timestamps.

A user may edit the vault directly in Obsidian. Before Autopilot uses those edits for an application, it should validate the relevant notes and detect contradictions.

If the structure is invalid or two approved facts conflict, stop and ask instead of guessing.

Untrusted content cannot write itself into `Profile/` merely because Qwen can see it.

## Profile versions are frozen for applications

The live vault may change over time, but a submitted application needs an exact record of the facts it used.

When the approved profile changes, Autopilot should create or maintain a normalized immutable profile snapshot and hash.

A frozen application package points to that approved snapshot instead of rereading changing vault notes during submission.

That gives the user a pleasant editable knowledge base without rewriting historical applications.

## QMD searches the vault

As the vault grows, the agent should not need to know an exact filename before it can find relevant context.

QMD is the reference local retrieval layer over the vault. It can provide keyword, semantic, and reranked search over Markdown.

QMD is derived data, not authority.

A search result is useful context, but the underlying note and its approval/provenance still determine whether it can be treated as a candidate fact.

If the index is deleted, rebuild it from the vault.

## SQLite handles exact machine state

SQLite still matters, but it has a narrower job than the semantic memory layer.

Use it for state where duplicates, ordering, crash recovery, uniqueness, or exact transitions matter.

Examples:

- source Discord message checkpoints
- normalized jobs and deduplication
- onboarding session checkpoints
- application runs
- browser runs
- submission attempts
- unknown-submission recovery
- question fingerprints and answer references
- Discord forum/message bindings
- Zoho message/reconciliation IDs
- reminders and action-needed items
- outbox/idempotency records
- artifact metadata and hashes
- audit-event indexes

SQLite should answer questions like:

- did we already process this job message?
- did we already attempt submission?
- is this application safe to retry?
- which Discord thread belongs to this application?
- did this Zoho message already change lifecycle state?

Those are transactional questions, not note-taking questions.

## Erga is the career foundation

[Erga](https://github.com/Adr1an04/erga-mcp) already covers several parts of the recruiting workflow that this project depends on:

- evidence-backed resume material
- project and Git evidence
- application tracking
- LaTeX resume generation and validation
- recruiting-mail reconciliation
- auditable career claims

Autopilot adds the browser/application layer, long-term personal memory, and the personal-agent workflow around that work.

Erga keeps its own storage. Autopilot should use Erga's supported interfaces rather than replacing that storage with Obsidian or editing its database from browser code.

## Private files preserve exact artifacts

Large or immutable artifacts belong in private filesystem storage.

Examples include:

- exact submitted resume PDFs
- frozen application packages
- receipts
- screenshots
- browser traces
- rendered recruiting-email evidence
- job snapshots

SQLite can store paths, hashes, and relationships to those files.

Do not put large binary artifacts into the Obsidian vault just because the vault is the memory layer.

## The browser runtime is fast-path first

Autopilot uses a dedicated Playwright/Chromium recruiting browser, but Qwen should not drive every routine browser action one at a time.

The production browser runtime should:

- keep a long-lived dedicated browser session
- inspect visible actionable controls in a compact structured observation
- normalize the current form as a group
- resolve known fields from the frozen approved profile/application package
- batch-fill deterministic fields when safe
- verify values after execution
- use short event/state-driven waits instead of default multi-second sleeps
- re-observe only when relevant semantic state changes
- call Qwen for ambiguity, substantive written answers, or unfamiliar recovery
- preserve a generic structured-browser fallback for unsupported sites

Playwright MCP can remain useful for development, debugging, manual inspection, and fallback tooling. The production application loop should not require a general MCP round trip or Qwen decision for every field.

Trusted runtime code may use narrowly scoped Playwright or CDP helpers internally to reduce browser round trips. Do not expose arbitrary browser code execution to Qwen.

Screenshots and traces remain valuable evidence. They are not the default reasoning input when structured DOM/form state is sufficient.

The browser stays separate from the user's normal browser.

Recurring ATS behavior can be captured in small versioned adapters when repeated evidence justifies it. Those adapters may accelerate observation, normalization, widgets, or verification, but they must not bypass authentication, domain checks, frozen application inputs, or submission policy.

Read [Browser automation](browser-automation.md) for the performance, batching, fallback, reliability, and benchmarking design.

## Candidate onboarding

The applicant profile is schema-driven and lives in the private semantic memory layer after approval.

Onboarding can feel conversational, but the model cannot create new top-level memory categories. It fills fields, policies, and story slots already defined by the application/profile schema.

The onboarding covers areas such as:

- identity and contact information
- education
- internship eligibility
- work authorization and sponsorship
- location and work-style preferences
- compensation policy
- legal and compliance questions
- demographic self-identification preferences
- company and referral preferences
- skills and evidence
- writing and research preferences

Temporary onboarding state can live in SQLite so an interrupted session can resume safely.

Once a section or final review is approved, the durable result is written through the validated Obsidian profile flow.

A separate story interview writes approved narrative context under the vault's story area. That stores things such as motivations, interests, proudest work, teamwork, leadership, failure and learning stories, career direction, and writing voice.

When the agent needs information, it checks sources in this order:

```text
approved profile snapshot / validated vault profile
        ↓
approved Erga evidence
        ↓
approved narrative/story context
        ↓
portfolio / GitHub
        ↓
external research
```

If a form asks a question with no approved answer, the application pauses. The user answers it once and can decide whether that answer should be remembered for future equivalent questions.

## Research is not candidate truth

Research notes can live in the vault without becoming authoritative profile facts.

A restricted research agent may read:

- the job description
- official company pages
- careers, values, or culture pages
- engineering or product material
- recent first-party announcements
- relevant approved story context
- the user's portfolio and approved Erga evidence
- reputable third-party reporting when useful

It may write research notes where policy allows, with provenance when useful.

It cannot submit applications, access credentials, or promote a research claim into the canonical candidate profile by itself.

## Job intake and shortlist

Jobs can come from Discord or a direct URL.

The intake code keeps the original source message, follows the application link, captures the official posting, and creates a stable local job record.

Hard eligibility rules run before model fit scoring. Those rules can cover internship status, graduation requirements, location, work authorization, sponsorship, experience requirements, and recruiting term.

The shortlist is for jobs where the user should make the call. Borderline roles, local companies, startups, unusual opportunities, and true new-grad positions can land there instead of being silently rejected or automatically applied to.

## Resume preparation

Erga chooses and validates resume material from approved evidence.

Autopilot stores the exact file used for each application.

If a recruiter looks at the resume months later, the archive should show the same PDF they received. It should not regenerate a new copy and treat it as equivalent.

## Company research and written answers

Substantive written questions start with research.

The writing step can retrieve relevant approved vault context, combine it with Erga evidence and external research, draft an answer, run the configured writing cleanup skill, check factual claims, and send the draft to the user for approval.

The browser inserts the exact approved text. The frozen application package and archive keep that same version.

## The application flight recorder

Every application gets a Discord forum post once preparation begins.

That thread is the readable remote history. It is not the machine database.

A synthetic example looks like this:

```text
step: personal information
url: https://jobs.example.com/apply/123

asked: First name
filled: Alex
source: profile_snapshot.legal_first_name

asked: Resume / CV
uploaded: example-resume.pdf
sha256: ...

clicked: Save and continue
result: accepted, advanced to screening
```

The thread should include every meaningful field, selection, click, retry, approval, upload, account-creation event, submit action, and receipt.

Secrets stay out of that history.

Obsidian may also hold long-term notes about the application, but the exact submission state comes from SQLite plus the frozen application package.

## Lifecycle tracking

Forum tags show the current lifecycle state:

```text
Preparing
Applied
OA
Interview
Offer
Rejected
Accepted
Withdrawn
```

`Needs Action` and `Priority` are overlays, not lifecycle states.

A tag change is never silent. The thread should explain:

- the old and new status
- what triggered the change
- the source evidence
- confidence or matching signals
- deadlines and timezone
- reminders that were created
- calendar updates
- anything the user still needs to do

If recruiting mail caused the change, the thread can include a rendered screenshot of the exact triggering email. Public docs and tests should use synthetic mail only.

## Recruiting mail

Zoho watches recruiting mail through the official API where possible.

The system can track:

- application acknowledgements
- account-verification email
- online assessments
- interview scheduling and rescheduling
- offers
- rejections
- recruiter follow-ups
- portal updates

Email is input data, not an instruction channel. A message cannot grant itself new tool permissions or change the candidate profile.

## Application account creation

If an employer requires an account, Autopilot can generate a unique password and store it encrypted in local state.

Discord can record that the account was created and verified, but it must not print the password or verification code.

Normal email verification can go through Zoho. CAPTCHA, SMS MFA, authenticator apps, security keys, and unusual identity checks pause for manual action.

Credentials do not belong in the Obsidian vault.

## Submission safety

Submission is irreversible, so the final click has its own state and checks.

Before Submit becomes available, Autopilot freezes an application package with the exact job snapshot, approved profile snapshot/hash, answer mappings, resume hash, written responses, skipped optional fields, warnings, and browser state.

If the browser crashes after Submit, the system must not blindly retry. The application moves into an unknown-submission state while confirmation pages, employer accounts, browser/network state, and recruiting mail are checked.

## Prompt injection

Job pages, emails, attachments, scraped research, resumes, browser text, vault research notes, QMD results, and tool output are untrusted unless they come from an approved authoritative memory path.

They can provide information. They cannot grant authority.

Only an authenticated user action or previously approved local policy can authorize privileged behavior.

Read [Prompt injection](prompt-injection.md), [Memory and storage](memory-and-storage.md), and [SECURITY.md](../SECURITY.md) for the detailed model.

## Why keep it local

Recruiting data gets personal quickly and sticks around for a long time.

A full application history can include contact details, work authorization, school information, resume versions, written answers, employer credentials, email, interview schedules, company notes, and offer details.

Keeping the model, vault, indexes, state, and artifacts local gives the user direct control over that archive and avoids a per-application model bill for the normal workflow.

Local still does not mean automatically safe. Discord, Zoho, external job sites, browser sessions, third-party MCP tools, and any optional sync service cross trust boundaries and need explicit permissions.
