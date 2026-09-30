# Memory and storage

Erga Autopilot uses different storage layers for different jobs. The goal is to keep long-term knowledge readable by the user without giving up the transactional guarantees needed for browser automation and submission safety.

The reference design uses four main layers:

```text
Hermes hot memory
      │
      ▼
Obsidian vault  ──> QMD index
      │
      ▼
Autopilot logic
      │
      ├── SQLite transactional state
      ├── Erga state
      └── private filesystem artifacts
```

Discord sits beside those layers as the remote control surface and human-readable application timeline.

The current implementation validates the canonical candidate note, creates immutable
approved snapshots, and exposes section reads and bounded Erga evidence reads.
`autopilot memory index` creates a rebuildable retrieval copy of the approved profile
inside the vault and indexes only that file in a separate QMD collection. Version and
file-hash checks block stale or edited retrieval copies. Drafts and research are not
included. See [Onboarding and jobs](onboarding-and-jobs.md) for the concrete workflow.

## Hermes hot memory

Hermes keeps a small built-in memory for information that should be available at the start of a session.

Use `MEMORY.md` and `USER.md` for compact, high-value context such as:

- project conventions
- important environment facts
- stable user preferences
- known tool quirks
- pointers to the private Autopilot vault and runtime

Do not try to fit the full candidate profile, company research, application history, or recruiting archive into Hermes hot memory. Those files are intentionally small.

## Obsidian is the long-term semantic memory

The private Obsidian vault is the main human-readable knowledge store for Autopilot.

It holds information that benefits from being readable, searchable, linkable, and editable outside the agent:

- approved candidate profile
- application preferences and policies
- `introduction.md`-style narrative context
- company notes
- research notes
- decisions and lessons learned
- long-term application notes
- career context and working notes

A reference vault can look like this:

```text
Erga Autopilot/
├── Profile/
│   ├── Candidate.md
│   ├── Education.md
│   ├── Eligibility.md
│   ├── Work Authorization.md
│   ├── Locations.md
│   └── Application Defaults.md
├── Story/
│   └── Introduction.md
├── Career/
│   ├── Skills.md
│   ├── Projects.md
│   └── Experience.md
├── Companies/
├── Applications/
├── Research/
├── Decisions/
├── Daily/
└── System/
```

That layout is a starting point, not a reason to create empty folders before the implementation needs them.

The vault is private runtime data. It belongs outside the Git checkout.

The expected public configuration key is:

```text
OBSIDIAN_VAULT_PATH
```

The real path belongs in local configuration.

## Canonical notes still need validation

Obsidian being human-readable does not mean the model gets unrestricted write authority.

Canonical profile and policy notes should use a schema that code can parse and validate. Markdown frontmatter is appropriate for stable metadata such as schema version, profile version, approval state, and timestamps.

A human may edit the vault directly in Obsidian, but Autopilot should validate relevant notes before using them for an application. If a manual edit creates a contradiction or invalid structure, stop and surface the problem instead of guessing.

Writes to authoritative areas such as `Profile/` should go through explicit profile/memory operations. Qwen may propose a change, but it should not turn arbitrary web content, email, or research text into an approved candidate fact.

## Approved profile snapshots

Applications need an exact record of the facts they used even if the live vault changes later.

When an approved candidate profile changes, Autopilot should be able to create a normalized immutable snapshot and hash for that profile version. The snapshot can live as a private file while SQLite stores the version ID, hash, and path/reference.

A frozen application package should point to that approved snapshot rather than reading a changing vault note during submission.

This keeps the vault pleasant to edit without losing historical reproducibility.

## Research is useful but not authoritative

Research notes may live in the vault, but they are not automatically candidate facts.

For example:

- `Research/` can contain company research and source notes
- `Companies/` can contain durable company context and referral notes
- `Applications/` can contain readable notes about an application

Those notes may influence research and writing where policy allows, but they cannot silently overwrite the approved candidate profile or Erga evidence.

When provenance matters, store the source URL, retrieval date, and enough context to understand where a note came from.

## QMD is the retrieval layer

As the vault grows, the agent should not depend on knowing the exact filename for every question.

QMD is the reference local search layer for the vault. It can index Markdown and provide local keyword, semantic, and reranked retrieval.

QMD is an index, not the source of truth.

If the QMD index is deleted, rebuild it from the vault. Do not store irreplaceable applicant state only inside the index.

The reference full setup may use the Hermes QMD skill. Check the current Hermes/QMD requirements before installation because Node.js, SQLite extension support, helper-model downloads, and commands can change.

## SQLite stays for transactional state

Autopilot still needs SQLite, but its role is intentionally narrower.

Use SQLite for state where duplication, ordering, concurrency, crash recovery, or exact transitions matter.

Examples include:

- Discord source-message checkpoints
- normalized job IDs and deduplication
- application run state
- browser-session/run state
- submission attempts
- unknown-submission recovery
- question fingerprints and answer references
- Discord forum/message bindings
- Zoho message/reconciliation IDs
- action-needed items
- reminders and queues
- outbox/idempotency records
- artifact metadata and hashes
- audit-event indexes

SQLite should be able to answer questions such as:

- was this source message already processed?
- was this job already queued or applied to?
- did we already attempt submission?
- is the application currently safe to retry?
- which Discord thread belongs to this application?
- was this recruiting email already reconciled?

Those are database problems, not Markdown problems.

## Onboarding uses both layers

During onboarding, temporary session state and autosave checkpoints may live in SQLite because the flow can be interrupted and resumed.

Once the user approves a section or completes final review, the durable semantic result belongs in the validated Obsidian profile structure.

That gives the user a profile they can actually read while keeping the onboarding workflow recoverable.

## Erga keeps its own state

Erga has its own local storage and domain model. Autopilot should not replace that storage with Obsidian or reach into Erga's database from browser code.

Use Erga's supported interfaces for career evidence, resume generation/validation, application state, and recruiting reconciliation.

Autopilot SQLite may reference Erga application or artifact IDs where needed.

## Filesystem artifacts

Large or immutable artifacts belong in the private filesystem rather than inside Markdown or SQLite blobs.

Examples:

```text
resumes/
application-packages/
receipts/
screenshots/
email-evidence/
traces/
job-snapshots/
backups/
```

SQLite can store paths, metadata, hashes, and relationships to those files.

For a submitted resume, preserve the exact bytes and SHA-256 used for that application.

## Discord is not the database

Discord is the phone-friendly control surface and a useful human-readable timeline.

The application forum can mirror important actions, approvals, lifecycle events, and receipts, but Discord is not machine truth and should not be the only copy of important state.

Secrets never belong there.

## Credentials are separate

Credentials do not belong in the Obsidian vault, normal Markdown notes, Discord, or model context.

Employer-account passwords may be stored as encrypted local ciphertext with the encryption key kept separately in an owner-only local location.

OAuth tokens, cookies, MFA codes, and encryption keys should stay out of the normal semantic memory path.

## Backups

The vault, SQLite state, and private artifacts all need backups eventually, but they have different recovery properties:

- Obsidian vault: durable semantic knowledge; back it up
- QMD index: derived; rebuild it
- SQLite: transactional state; back it up consistently
- application artifacts: preserve exact files and hashes
- credentials: back up only through an approved encrypted method

Do not add cloud sync as a silent requirement. A user may choose Obsidian Sync or another backup system, but the default architecture remains local-first.

## The rule of thumb

Use Obsidian when the question is:

> what does the user know, prefer, remember, or want to read and edit?

Use SQLite when the question is:

> did this machine action happen exactly once, and what state is the workflow in right now?

Use files when the answer needs to preserve exact bytes.

Use Hermes hot memory only for the small amount of context that should be present at session start.

## Application queue implementation

The recruiting SQLite database now also holds canonical URL aliases, application queue
records, forum bindings, delivery events, owner-message checkpoints, and field-bound
answers. Exact observations, profile snapshots, resume PDFs, Erga results, and Qwen
proposals live in private per-application directories. See [Application workflow](application-workflow.md).

## Application notes and credentials

Each application also gets a readable note in the vault under
`Erga Autopilot/Applications/`, rewritten from SQLite and the private artifacts on every
change. It is a mirror for reading and searching, not a candidate fact, and it is not
indexed as profile memory. Employer-account credentials never enter the vault, Discord,
or model context; they live in `credentials/store.enc` under the private state root with
the key in `credentials/key` next to it, both owner-only.
