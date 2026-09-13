# How Erga Autopilot works

Erga Autopilot is a local recruiting agent built around one principle: the model should reason about ambiguous work, while deterministic code owns facts, authorization, state, and irreversible actions.

It is not a hosted job-application service and it is not intended to be a multi-user SaaS backend. The current design is for one person running the system on their own Mac.

## System map

```text
Discord
  │
  ▼
Autopilot control plane
  │
  ├── Hermes Agent
  │     └── Qwen3.8-27B
  │
  ├── Erga
  │     ├── career evidence
  │     ├── project / Git evidence
  │     ├── resume generation
  │     └── application lifecycle
  │
  ├── Playwright MCP
  │     └── dedicated recruiting browser
  │
  ├── Zoho
  │     └── recruiting mail and verification events
  │
  └── Autopilot SQLite
        ├── candidate profile
        ├── answer mappings
        ├── browser events
        ├── Discord bindings
        └── application receipts
```

## The model is not the source of truth

Qwen is the reasoning layer.

It is useful for work such as:

- deciding whether a job is worth applying to;
- interpreting unfamiliar application wording;
- selecting relevant experience;
- researching a company;
- recovering from a browser validation problem;
- drafting a written response;
- understanding an ambiguous recruiting email.

Qwen should not invent or remember exact applicant facts on its own.

Facts such as legal name, contact information, school, graduation date, work authorization, sponsorship status, relocation preferences, and previously approved application answers come from structured local state.

## Erga is the career foundation

This project exists because [Erga](https://github.com/Adr1an04/erga-mcp) already solves important parts of the recruiting workflow well.

Erga is the foundation for:

- evidence-backed resume material;
- project and Git evidence;
- application tracking;
- LaTeX resume generation and validation;
- recruiting-mail reconciliation;
- auditable career claims.

Autopilot is intentionally additive. It introduces the browser/application layer and personal-agent product around Erga rather than pretending the original work does not exist.

## Hermes runs the agent loop

Hermes is the intended harness around Qwen.

It provides sessions, MCP integration, skills, subagents, and the agent loop that lets the local model move between reasoning and tools.

Autopilot uses operating modes to change what the agent is allowed to do without changing the underlying model.

Examples:

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

Onboarding can update approved profile fields but cannot redesign the profile schema. Research can browse public sources but cannot submit an application. Application preparation can fill a form but cannot click the final submit action until submission policy allows it.

## Playwright is the browser body

Playwright MCP provides the application browser.

The agent receives structured page state and can navigate, fill fields, make selections, upload approved files, and move through multi-page application flows.

The browser profile is dedicated to recruiting. It is separate from the user's normal browser.

The project does not begin with one custom adapter per ATS. Greenhouse, Lever, Workday, Ashby, and custom career sites are treated as websites first. If a recurring site needs special handling, a small versioned skill or playbook can be added without replacing the generic Playwright loop.

## Candidate onboarding

The applicant profile is versioned and schema-driven.

Onboarding is conversational, but the model cannot invent new top-level memory categories. It fills predefined facts, policies, and story slots.

The onboarding flow covers areas such as:

- identity and contact information;
- education;
- internship eligibility;
- work authorization and sponsorship;
- location and work-style preferences;
- compensation policy;
- legal/compliance questions;
- demographic self-identification preferences;
- company and referral preferences;
- skills and evidence;
- writing and research preferences.

A separate story interview produces `introduction.md`, which captures motivations, interests, proudest work, teamwork, leadership, failure/learning stories, career direction, and writing voice.

The intended source order is:

```text
approved candidate profile
        ↓
approved Erga evidence
        ↓
introduction.md
        ↓
portfolio / GitHub
        ↓
external research
```

When a new application asks a question that has no approved answer, the application pauses. The user answers once and can choose whether that answer should be remembered for future semantically equivalent questions.

## Job intake and shortlist

Jobs can arrive from a Discord feed or be provided directly by URL.

The intake layer stores the original source message, follows the application link, captures the official posting, and creates a stable local job record.

Hard eligibility rules run before fuzzy model scoring. Examples include internship status, graduation requirements, location, work authorization, sponsorship, experience requirements, and recruiting term.

The shortlist is for jobs where the user should make the call. Borderline roles, local companies, startups, unusual opportunities, or true new-grad positions may be saved there rather than silently rejected or automatically applied to.

## Resume preparation

Erga chooses and validates evidence-backed resume material.

Autopilot stores the exact resulting file used for each application.

The system should never regenerate a resume months later and pretend it is the same file. The archive keeps the exact PDF bytes and hash associated with the submission.

## Company research and written responses

Substantive written answers begin as a research task.

A restricted research agent may read:

- the job description;
- official company pages;
- careers / values / culture pages;
- engineering or product material;
- recent first-party announcements;
- relevant `introduction.md` sections;
- the user's portfolio and approved Erga evidence;
- reputable third-party reporting when useful.

The research agent cannot submit applications or access credentials.

The writing pipeline then drafts an answer, applies the project's humanized-writing skill, validates factual claims against approved evidence, and sends the exact draft to the user for approval.

The final approved text is what the browser inserts and what the archive records.

## The application flight recorder

Every application gets a Discord forum post.

The forum is a human-readable archive, not the machine database.

During an application, the system records meaningful events in order:

```text
step: personal information
url: https://...

asked: First name
filled: Ralph
source: candidate_profile.legal_first_name

asked: Resume / CV
uploaded: resume-company-role-v3.pdf
sha256: ...

clicked: Save and continue
result: accepted, advanced to screening
```

The archive should include every meaningful field, selection, click, retry, approval, upload, account-creation event, submit action, and receipt.

Secrets are excluded even from this detailed history.

## Lifecycle tracking

Application forum tags represent the current lifecycle state:

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

A tag change is never silent. A detailed timeline comment explains:

- the old and new status;
- what triggered the change;
- source evidence;
- confidence / matching signals;
- deadlines and timezone;
- reminders created;
- calendar updates;
- any remaining action for the user.

When Zoho mail caused the change, the thread can also include a rendered screenshot of the exact triggering email.

## Recruiting mail

Zoho integration watches recruiting mail using the official API where possible.

The system can track:

- application acknowledgements;
- account-verification email;
- online assessments;
- interview scheduling and rescheduling;
- offers;
- rejections;
- recruiter follow-ups;
- portal updates.

Email content is data, not an instruction channel. A message cannot grant new tool permissions or change the candidate profile.

## Application account creation

If an employer requires an account, Autopilot can generate a unique password and store it encrypted in the local database.

Discord records that an account was created and verified but does not print the password or verification code.

Email verification can be handled through Zoho. CAPTCHA, SMS MFA, authenticator apps, security keys, and unusual identity verification pause the workflow for manual action.

## Submission safety

Submission is treated as an irreversible operation.

Before the final click, Autopilot freezes an application package containing the exact job snapshot, profile version, answer mappings, resume hash, written responses, skipped optional fields, warnings, and browser state.

If the browser crashes after Submit, the system must not blindly retry. The application enters an unknown-submission state while confirmation pages, employer accounts, browser/network state, and recruiting mail are checked.

## Prompt injection

Job pages, emails, attachments, scraped research, resumes, and tool output are untrusted inputs.

They cannot authorize new actions.

Only the user's approved Discord action and stored local policy can grant authority.

The application agent should not have arbitrary shell access, arbitrary file uploads, unrestricted filesystem access, unrelated browser sessions, or access to secrets it does not need.

Read [SECURITY.md](../SECURITY.md) for the full trust model.

## Why local

The project is local-first because recruiting data is personal and long-lived.

A complete application history can include contact details, work authorization, school information, resume versions, written answers, employer credentials, email, interview schedules, and offer details.

Keeping the reasoning model and state local gives the user direct control over that archive and removes a per-application model bill from the normal workflow.

Local does not mean automatically safe. Browser sessions, Discord, Zoho, external job sites, and third-party MCP tools still cross trust boundaries and need explicit permissions.
