# How Erga Autopilot works

Erga Autopilot is a local recruiting agent. Qwen handles the parts that need judgment. Normal code owns facts, permissions, state, and irreversible actions.

This is not a hosted job-application service or a multi-user SaaS backend. The current design is for one person running it on their own Mac.

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

## What Qwen is responsible for

Qwen is the reasoning layer. It is useful when the answer is not a simple lookup.

Examples:

- deciding whether a job is worth applying to
- interpreting unfamiliar application wording
- selecting relevant experience
- researching a company
- recovering from a browser validation problem
- drafting a written response
- making sense of an ambiguous recruiting email

Qwen is not the source of truth for applicant facts.

Legal name, contact information, school, graduation date, work authorization, sponsorship status, relocation preferences, and saved application answers come from structured local state.

## What Erga does

This project exists because [Erga](https://github.com/Adr1an04/erga-mcp) already solves several parts of the recruiting workflow well.

Erga remains the foundation for:

- evidence-backed resume material
- project and Git evidence
- application tracking
- LaTeX resume generation and validation
- recruiting-mail reconciliation
- auditable career claims

Autopilot adds the browser/application layer and the personal-agent experience around that work. It does not pretend the Erga foundation is original to this repo.

## Hermes runs the agent loop

Hermes sits between Qwen and the tools.

It provides sessions, MCP integration, skills, subagents, and the loop that lets Qwen reason, call a tool, inspect the result, and continue.

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

The same model can work in all of those modes. The permissions change.

For example:

- onboarding can fill approved profile fields but cannot redesign the profile schema
- research can browse public sources but cannot submit an application
- application preparation can fill the form but cannot use the final submit action until policy allows it

## Playwright runs the browser

Playwright MCP gives the agent its application browser.

The agent receives structured page state and can navigate, fill fields, select options, upload approved files, and work through multi-page forms.

That browser uses a dedicated recruiting profile. It stays separate from your normal browser.

The project does not start with one custom integration for every ATS. Greenhouse, Lever, Workday, Ashby, and custom career sites are treated as websites first. If one of them has a recurring quirk, add a small versioned skill or helper instead of replacing the generic browser loop.

## Candidate onboarding

The applicant profile is versioned and defined by a schema.

Onboarding can feel conversational, but the model cannot create new top-level memory categories. It fills fields, policies, and story slots that are already defined in code.

The onboarding covers things like:

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

A separate story interview produces `introduction.md`. That file stores the less rigid context that helps with writing: motivations, interests, proudest work, teamwork, leadership, failure and learning stories, career direction, and writing voice.

When the agent needs information, it checks sources in this order:

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

If a form asks a question with no approved answer, the application pauses. You answer it once and decide whether that answer should be remembered for future equivalent questions.

## Job intake and shortlist

Jobs can come from a Discord feed or a direct URL.

The intake code keeps the original source message, follows the application link, captures the official posting, and creates a stable local job record.

Hard eligibility rules run before Qwen scores fit. Those rules can cover internship status, graduation requirements, location, work authorization, sponsorship, experience requirements, and recruiting term.

The shortlist is for jobs where you should make the call. Borderline roles, local companies, startups, unusual opportunities, and true new-grad positions can land there instead of being silently rejected or automatically applied to.

## Resume preparation

Erga chooses and validates resume material from approved evidence.

Autopilot stores the exact file that was used for the application.

If a recruiter looks at your resume months later, the archive should show the same PDF they received. It should not regenerate a new copy and treat it as equivalent.

## Company research and written answers

Substantive written questions start with research.

A restricted research agent may read:

- the job description
- official company pages
- careers, values, or culture pages
- engineering or product material
- recent first-party announcements
- relevant sections of `introduction.md`
- your portfolio and approved Erga evidence
- reputable third-party reporting when it adds useful context

The research agent cannot submit applications or access credentials.

The writing step then drafts an answer, runs the configured writing cleanup skill, checks factual claims against approved evidence, and sends the draft to you for approval.

The browser inserts the exact approved text. The archive stores that same version.

## The application flight recorder

Every application gets a Discord forum post.

That thread is the readable history. It is not the machine database.

During an application, the system should leave records like this:

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

The thread should include every meaningful field, selection, click, retry, approval, upload, account-creation event, submit action, and receipt.

Secrets stay out of that history.

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
- anything you still need to do

If Zoho mail caused the change, the thread can also include a rendered screenshot of the email that triggered it.

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

If an employer requires an account, Autopilot can generate a unique password and store it encrypted in the local database.

Discord can record that the account was created and verified, but it must not print the password or verification code.

Normal email verification can go through Zoho. CAPTCHA, SMS MFA, authenticator apps, security keys, and unusual identity checks pause for manual action.

## Submission safety

Submission is irreversible, so the final click has its own state and checks.

Before Submit becomes available, Autopilot freezes an application package with the exact job snapshot, profile version, answer mappings, resume hash, written responses, skipped optional fields, warnings, and browser state.

If the browser crashes after Submit, the system must not blindly retry. The application moves into an unknown-submission state while the confirmation page, employer account, browser/network state, and recruiting mail are checked.

## Prompt injection

Job pages, emails, attachments, scraped research, resumes, and tool output are untrusted inputs.

They cannot authorize new actions.

Only your approved Discord action and stored local policy can grant authority.

The application agent should not have arbitrary shell access, arbitrary file uploads, unrestricted filesystem access, unrelated browser sessions, or secrets it does not need.

Read [SECURITY.md](../SECURITY.md) for the full trust model.

## Why keep it local

Recruiting data sticks around for a long time and gets personal fast.

A full application history can contain contact details, work authorization, school information, resume versions, written answers, employer credentials, email, interview schedules, and offer details.

Keeping the model and state local gives you direct control over that archive and avoids a per-application model bill for the normal workflow.

Local still does not mean automatically safe. Discord, Zoho, external job sites, browser sessions, and third-party MCP tools all cross trust boundaries and need explicit permissions.
