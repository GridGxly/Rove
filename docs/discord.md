# Discord architecture

Discord is Erga Autopilot's remote control surface and human-readable activity log. It is not the source of truth for application state, candidate memory, or artifacts.

The server layout used by the project is:

```text
Pipeline
├── applications
└── shortlist

Agent
├── agent-control
├── action-needed
└── memory

Recruiting
└── recruiting

System
└── system-log
```

Real guild, channel, role, and user IDs stay in local configuration and must not be committed.

## Channel roles

### Pipeline

#### `applications`

`applications` is a Discord forum. Once an application enters preparation, it gets its own forum post.

That post is the readable history for one application. It should answer what Autopilot did, what it submitted, what changed later, and why.

The post's first message is a live status card that Autopilot edits in place (headline, one line, the reply commands), so the forum list previews the current state. The entries below it are the chronological record: routine steps (opened, clicked Apply, resume ready, your replies, lifecycle changes, the submit click) are one-line messages, and cards are used for decisions, drafts, job fit, the filled form, submission results, and failures. Sources are shown in words ("your profile", "your reply", "your evidence"), never as internal keys.

The post can keep:

- the source job and official application URL
- the exact resume used
- each application page reached
- every meaningful field label and value entered
- where an answer came from
- selections, checkboxes, and meaningful clicks
- account creation and verification events
- written answers and approval history
- validation errors and retries
- the final submit action
- the confirmation page or receipt
- later recruiting events such as an OA, interview, offer, rejection, acceptance, or withdrawal

The Discord post is a readable flight recorder. SQLite remains the exact transactional state, and private files remain the source for immutable artifacts such as resumes, screenshots, receipts, and browser traces.

#### `shortlist`

`shortlist` holds jobs that need a human decision before Autopilot continues.

This is useful for borderline roles, unusual opportunities, startups, local companies, or jobs that do not clearly pass or fail the configured application rules.

A shortlist item should preserve enough context to make the decision without redoing discovery, including the job, company, source, official URL, and the reason it was held for review.

The implemented workflow posts a job-fit hold here, and only here: a feed job whose posting states an eligibility requirement (program, graduation window, work authorization, sponsorship, location, degree) that conflicts with an approved fact. The card links to the thread and carries the conflicting requirement (and any eligibility that could not be checked) as its reasons, plus the `proceed` and `defer` commands. Eligibility that could not be checked, skills, dates, and other items never hold a job on their own, and a link you pasted is never held on fit. The channel is a to-do list: an application has at most one live card here, and it is withdrawn when the application stops waiting on you.

## Agent

#### `agent-control`

`agent-control` is the main command surface for interacting with Autopilot.

Commands and approvals sent here are still subject to normal authorization and policy checks. A Discord message does not override code-level submission, memory, browser, or credential rules.

#### `action-needed`

`action-needed` is only for work that requires human attention.

Examples include:

- an application question with no approved answer
- CAPTCHA or MFA
- an ambiguous submission result
- a written response waiting for approval
- a browser state Autopilot cannot safely recover from
- a deadline or recruiting event that needs a decision

This channel should stay quiet unless the user actually needs to do something.

With the `auto_submit` policy on, a complete application is sent without a card here; the thread's live status card and its timeline are where the owner reviews it afterwards.

The implemented workflow posts here for answers it cannot draft, sign-in and account steps, blocked sites and manual steps in the browser, stopped preparation, ready-to-submit review, and submission problems. Job fit never lands here; that goes to `shortlist`. The channel is a to-do list: an application has at most one live card, a new card replaces the previous one, and the card is withdrawn when the application stops waiting on you.

#### `memory`

`memory` is the Discord interface for controlled local memory operations.

It can be used to review, add, or correct information through the approved memory flow. Messages in this channel do not automatically become authoritative candidate facts.

Canonical profile data still has to pass schema validation and approval before it can be used in an application. Obsidian is the long-term semantic memory layer, and SQLite tracks the machine state around those operations.

## Recruiting

#### `recruiting`

`recruiting` is the human-readable feed for post-application recruiting events.

It can surface:

- application acknowledgements
- online assessments
- interview requests and scheduling changes
- recruiter follow-ups
- offers
- rejections
- portal updates

Recruiting email is input data, not an instruction channel. Zoho or another mail integration can classify and reconcile an event, but the resulting lifecycle change must still follow normal application-state rules.

When an event maps to an existing application, the application forum post should also receive a timeline entry so the full history stays together.

## System

#### `system-log`

`system-log` is for operational and technical activity that is useful for debugging without cluttering the user-facing application history.

It can include safe information such as:

- timestamps
- application or job IDs
- agent mode changes
- tool names
- safe tool parameters
- browser URLs
- retries
- recoverable errors
- lifecycle transitions
- queue or reconciliation activity
- model and runtime metadata

It must not include secrets, OAuth tokens, passwords, browser cookies, MFA codes, encryption keys, or private note bodies.

## How an application is logged

A forum post in `applications` should read like a chronological flight recorder.

A typical entry can look like:

```text
step: personal information
url: https://jobs.example.com/apply/123

asked: First name
filled: Alex
source: your profile

asked: Resume / CV
uploaded: example-resume.pdf
sha256: ...

clicked: Save and continue
result: accepted, advanced to screening
```

The log should capture events in the order they happened and include provenance when it matters.

For form work, record:

```text
asked: <field label>
filled: <submitted value>
source: <approved profile, Erga evidence, approved answer, or user approval>
```

For files, record the exact artifact and hash rather than only saying a resume was uploaded.

For actions, record the action and result:

```text
clicked: Submit application
result: confirmation page reached
receipt: <private artifact reference>
```

If an action fails or is retried, keep the earlier event instead of replacing it. The point of the log is to preserve what actually happened.

## Lifecycle tags

The `applications` forum uses lifecycle tags for the current state:

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

`Needs Action` and `Priority` are overlays rather than lifecycle states.

A tag change should also create a timeline entry in the forum post. It should include:

- the previous state
- the new state
- what triggered the change
- the source evidence
- any deadline and timezone
- reminders or calendar actions that were created
- anything the user still needs to do

A forum tag is the quick visual status. The timeline entry explains why the status changed.

## What Discord is not

Discord is not the primary database and should not be treated as durable authority by itself.

The layers have different jobs:

```text
Discord        remote control + readable history
SQLite         exact transactional workflow state
Obsidian       long-term semantic memory
Erga           career evidence + resume/application foundation
Private files  exact resumes, receipts, screenshots, traces
Zoho           recruiting-mail input
```

If Discord and the exact local state disagree, the local transactional state and frozen application artifacts win. Discord can be repaired from those records.

## Privacy and security

Discord logs should be useful without becoming a secret store.

Never post:

- passwords
- OAuth access or refresh tokens
- browser cookies or session storage
- MFA codes
- encryption keys
- full Social Security numbers
- bank or routing information
- private identity documents
- unrelated private note contents

The production bot should authorize commands with the configured numeric Discord user ID rather than a display name or username.

Third-party source bots should only see the channels they need. They should not be able to read application archives, memory, recruiting events, or system logs.

See [Security](../SECURITY.md), [How it works](how-it-works.md), and [Memory and storage](memory-and-storage.md) for the rest of the trust and storage model.

## Implemented queue and source feed

The [application workflow](application-workflow.md) maps an existing source channel
(such as `jobs`) through private configuration, publishes matching Keryx updates, and
queues preparation. Every post is an embed card: a title, one line of context, values in
fields, and the exact reply commands in a code block. The jobs channel gets one card per
Keryx job, newest first, at most `batch_size` (default 10) per feed tick; pending
announcements beyond the newest `max_pending` (default 40) expire unposted. In the
forum, routine steps (opening, clicking Apply, resume ready, your replies, lifecycle
transitions with their trigger, the submit click) are one-line messages; cards cover
job-fit review, the filled form (one field per entry with its source in words), each
Qwen draft with its approve command, questions only you can answer, holds, and
submission results. Owner-only `answer`, `use`, `resume`, `defer`, `proceed`, `account`,
`submit`, and `reconcile` commands are handled by deterministic code, independently of
model wording. A job-fit hold on a feed job is posted to `shortlist` only, with the
conflicting eligibility requirement (and any unchecked ones) as its reasons; a pasted
link is never held on fit. Action-needed and shortlist cards are recorded before they
are posted, and a failed post is retried on the next worker tick; each application keeps
at most one live card per channel, withdrawn when it stops waiting on you.
Recruiting-mail lifecycle updates are still unimplemented.
