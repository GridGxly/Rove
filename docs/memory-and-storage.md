# Memory and storage

Rove keeps each kind of data in the layer that suits it. Knowledge the owner reads and edits lives in an Obsidian vault. State that must be exact lives in SQLite. Files whose bytes matter are kept as private files. Erga keeps its own state, and Hermes keeps a small hot memory.

```text
Hermes hot memory        small session-start context
Obsidian vault           approved profile, voice sample, readable notes
  └── QMD index          search over the approved profile copy, rebuildable
SQLite                   queue, attempts, bindings, answers, checkpoints
Private files            profile snapshots, resumes, packages, receipts, screenshots, mail
Erga                     career evidence, resume generation, application status
```

Everything here except the vault lives under the state root, `~/.config/rove` by default. `ROVE_STATE_DIR` moves it. The vault and the state root both stay outside the Git checkout.

## Hermes hot memory

Hermes' `MEMORY.md` and `USER.md` hold a small amount of context for the Discord agent: stable preferences, environment facts, tool quirks and pointers to the vault. The full profile, application history and research do not belong there.

The worker's own Qwen calls for job fit, drafting, cleanup and mail labels skip Hermes memory and context files. They see only the input the worker builds.

## The Obsidian vault

The vault path comes from `OBSIDIAN_VAULT_PATH` in the process environment, or from `obsidian_vault_path` in private `config/recruiting.json`. The launchd services receive only `PATH` and `ROVE_STATE_DIR`, so a service install needs the path in `config/recruiting.json`. The real path is never committed.

Rove reads and writes inside one folder of the vault:

```text
Rove/
├── Profile/
│   └── Candidate.md              the approved profile
├── Retrieval/
│   └── Approved profile.md       copy indexed by QMD
├── Story/
│   └── Voice.md                  optional, written only by the owner
├── Applications/
│   └── <job title> · <id>.md     one note per application
├── Research/
│   └── <employer site>.md        company research, marked untrusted
└── Answers.md                    copy of remembered answers
```

### The approved profile

`Profile/Candidate.md` is the canonical profile. Its front matter holds eight fixed sections: identity, education, eligibility, availability, preferences, evidence, stories and application policy. `rove onboarding approve` writes it after the owner reviews the exact draft. The model cannot approve a profile and cannot add a section.

Every read validates the note against the approved hash. If the owner edits the note by hand, the hash no longer matches and Rove refuses to use the profile until it is reviewed and approved again. Prose below the front matter is not an application fact. [Onboarding and jobs](onboarding-and-jobs.md) describes the interview and approval commands.

### Voice note

`Story/Voice.md` is optional. Put a few paragraphs you wrote without help in it, such as notes, emails or an old essay. Qwen gets up to about 2,500 characters of it as a style sample for written answers. Rove reads the note and never writes it. Nothing in it reaches an application as a fact unless the approved profile or evidence shows the same fact.

### Application notes

Each application has a readable note under `Applications/` with its status, links, job fit, filled values with sources, open questions with drafts, and a timeline. It is rewritten from SQLite and the private files on every change. It is a mirror for reading and searching. Rove never reads it back and it is never a candidate fact.

### Research notes

When company research finds text, a copy goes to `Research/<employer site>.md` with the source URLs and the fetch time, marked untrusted at the top. The next application to the same employer overwrites it. Qwen sees the private `research.json` in the application's folder, so editing the note changes no draft.

### Remembered answers

The exact store of remembered answers is the `answer_memory` table in SQLite. `Answers.md` is a readable table of the question, the answer and the date, rewritten whenever an answer is remembered, changed or removed. Rove does not read it back, so editing the note does not change what Rove fills. To change or remove an answer, use the `memory` channel described in [Discord](discord.md#memory-channel).

The copy is written only when `OBSIDIAN_VAULT_PATH` is set in the environment of the process that records the change. With the vault configured only through `config/recruiting.json`, the answer is still stored in SQLite and the note is skipped.

### What the vault never holds

Credentials, tokens, cookies, verification codes, resume PDFs, receipts and screenshots do not go in the vault.

## QMD

QMD indexes one file, `Retrieval/Approved profile.md`, in the `rove-candidate` index and the `approved-profile` collection. `rove memory index` writes that file from the approved profile and rebuilds the index. Run it again after each approved change.

Retrieval refuses to run when the copy is stale, was edited, or the canonical profile is invalid. Drafts, research notes and application notes are not indexed. A search result is a pointer for the agent, and `read_candidate_section` is what returns an authoritative answer.

The index is derived data. If it is lost, rebuild it.

## SQLite

Rove uses one database, `recruiting.sqlite3` in the state root, readable only by the owner. It holds state where duplicates, ordering, crash recovery or exact transitions matter.

| Area | Tables |
| --- | --- |
| Job catalog | `job_sources`, `jobs`, `job_events` |
| Feed announcements | `feed_cursor`, `feed_outbox` |
| Onboarding and profile versions | `onboarding`, `onboarding_events`, `onboarding_issues`, `profile_versions`, `active_profile` |
| Application queue and timeline | `application_queue`, `application_events`, `job_link_aliases` |
| Owner replies and cards | `owner_commands`, `workflow_checkpoints`, `owner_notices` |
| Answers | `application_answers` for one application, `answer_memory` across applications |
| Memory channel | `memory_outbox` for replies and "Saved" lines, `memory_listing` for the last numbered list shown, `memory_announced` for a hash of the last announced value of each answer |
| Submission | `live_submission_attempts` |
| Recruiting mail | `mail_checkpoints`, `mail_messages` |

It answers questions such as whether a job was already queued, whether a Discord message was already applied, whether a submission was already attempted, which thread belongs to an application, and whether a mail was already handled.

Readable long-term knowledge does not go here. Remembered answers are the one place SQLite holds facts the owner gave, because filling a form needs an exact lookup by question. The text of a memory-channel reply is blanked in `memory_outbox` once Discord has it.

## Private files

The state root is created with owner-only permissions. Its layout as the code uses it:

```text
~/.config/rove/
├── config/                 workflow.json, feed.json, mail.json, recruiting.json, setup.env
├── recruiting.sqlite3
├── applications/<id>/      everything about one application
├── profiles/snapshots/     one immutable JSON file per approved profile hash
├── onboarding/             the current draft
├── jobs/                   Keryx snapshots and sync status
├── browser/                the recruiting Chrome profile and session file
├── credentials/            encrypted employer accounts and their key
├── erga/                   Erga's config and state
├── mail/                   handled recruiting mail
├── memory/                 state of the QMD index
├── logs/                   service logs and the delivery-failure log
└── synthetic/              certification fixtures
```

An application's folder holds:

- `profile.json`, the approved profile as frozen for this application
- the posting text and Qwen's input and output for the job-fit review, and the reviewed result
- `resume.pdf`, its manifest with the SHA-256, and Erga's result or error
- `research.json`, the company research cache
- Qwen's input and output for drafting and cleanup, and `answer-proposals.json`
- `observation.json` and `browser.png` from the latest observation
- `package.json`, the reviewed form state and its hash
- `receipt.json` after a submission attempt, with any earlier receipt kept beside it
- debugging evidence listed in [Browser automation](browser-automation.md#evidence-kept-for-debugging)

For a submitted resume, the exact bytes and hash are what the application used. Rove does not regenerate a resume and present it as the original.

## Frozen profile versions

Approving a profile writes an immutable snapshot named by its hash and records it in SQLite as the active version. An application records that hash when it is queued and keeps its own copy of the profile from the first time its page is opened.

Preparation checks that the copy still hashes to the recorded value and that it is still the approved version. Sending checks both again. If the approved profile changed in between, the run stops. There is no automatic rebuild of an application onto a new profile version.

## Erga

Erga keeps its own configuration and storage under `erga/` in the state root. Rove never writes Erga's database. It calls Erga through its MCP interface for four operations: `intake_job_url`, `validate_tailored_resume`, `confirm_application_submission` and `update_application_status`. Evidence reads go through `list_evidence` and return at most three approved excerpts.

SQLite and the resume manifest keep the Erga application ID where one exists.

## Credentials

Employer accounts that Rove creates are stored in `credentials/store.enc`, encrypted with Fernet, with the key in `credentials/key` beside it. Both are readable only by the owner. Only the browser daemon reads the store, to fill a sign-in form on the same host. Passwords never enter Discord, the vault, model context or logs.

The Discord bot token and the Zoho values live in a private env file, `config/setup.env` in the state root or `~/.hermes/.env`. When both files define a key, the value in `~/.hermes/.env` is used.

## Recruiting mail

Each mail the mail service applies to an application is kept as plain text under `mail/messages/<message id>/`, with Qwen's input and output beside it when Qwen classified it. A mail that settles an unclear submission is cited from the receipt by that path.

SQLite remembers which message IDs were handled and the newest received time per account, so a mail is applied once and a restart resumes where it stopped. Discord shows the sender's domain, the subject and the label. Nothing in a mail becomes a candidate fact.

## Discord

Discord is the control surface and a readable timeline. It is not a database. Thread entries and cards are generated from SQLite and can be posted again from it.

## Backups

Rove has no backup command yet. What to protect differs by layer:

- the vault: back it up
- `recruiting.sqlite3`: back it up while the services are stopped, or with SQLite's own backup tools
- `applications/`, `profiles/` and `mail/`: keep the files as they are, since their hashes are recorded
- `credentials/`: back up the key and the store together, through an encrypted method only
- the QMD index and `browser/session.json`: rebuildable

Cloud sync is not required. If you use Obsidian Sync or another product for the vault, that is your choice and the rest of the stack does not depend on it.
