# Application workflow

The local workflow joins Keryx discovery and owner-pasted links in one durable queue,
prepares each application in a background recruiting browser, drafts answers with Qwen
through Hermes, and submits one reviewed package per owner approval on supported boards.
Recruiting-mail reconciliation is not implemented; unattended submission is an owner policy (see below).

## Intake and visibility

`autopilot feed tick` checks the fixed Keryx source. An unchanged revision does not
redownload the snapshot. New matching internships enter a deduplicated notification
outbox and application queue; each job is announced once, and later Keryx metadata
changes to a known job do not re-post it. Each tick publishes the newest pending
matches first, at most `batch_size` (default 10) of them, one card per job (title,
company, location, cycle, track, application id). Before posting, pending announcements
beyond the newest `max_pending` (default 40) are expired unposted, so a long gap
between ticks does not flood the channel. A posting that closes in Keryx parks its queued
application and leaves a note on any application already in progress. Tracking
parameters (`utm_*`, `gh_src`, `ref`, and similar) are stripped so the same posting
reached through two links is one application.

`start_job_application(url)` accepts owner-requested public HTTPS links independently
of the feed. A Jobright identifier is never treated as a Keryx identifier. Verified
source-to-employer aliases live in SQLite; unknown redirects need review. Queue
uniqueness prevents a second application for the same canonical URL.

## The recruiting browser

The browser daemon launches Google Chrome (or Chrome for Testing) as its own instance
with the dedicated recruiting profile and drives it over a localhost DevTools port using
Patchright, a Playwright fork that removes the control-protocol leaks bot managers
fingerprint. Because the browser is launched by the daemon and not by an automation
library, no automation flag is ever set and `navigator.webdriver` stays false.

- Launch happens once, at service start. Chrome activates itself on launch; the daemon
  watches for a few seconds and hands focus back to whatever the owner was using.
- Every application gets a background tab. Tabs never take focus. The window sits behind
  the owner's work and opens from the Dock (the recruiting Chrome shows its own icon).
- Finished tabs close on applied or deferred, and the window is capped at
  `max_open_tabs` (default 5); a closed tab reopens on `resume`.
- The browser outlives the daemon: a daemon restart reconnects and re-associates the open
  tabs with their applications. Only one client may drive that Chrome; a second
  Playwright client on the same instance stalls it.
- Actions are paced like a person (`human_pacing`, default on): short random pauses,
  mouse travel before clicks, typed short values, and a visit to the site's front page
  before a deep link.
- The destination guard (public HTTPS only, verified employer or ATS, no private
  networks) attaches to a page only while the daemon drives it, so tabs the owner
  browses by hand are never stalled.

A block page ("Access Denied", "Pardon our interruption", a Cloudflare interstitial, or a
reference number) is recognized. The daemon waits, enters through the site's front door,
and tries once more. A second block hands the application to the owner with the direct
link (`MANUAL_TAKEOVER`); `reconcile ... applied` then records a manual application.
CAPTCHAs are never solved by the workflow; the owner completes them in the browser.

## Job-fit review before any applicant data

When the worker reaches an application form it asks Qwen to extract the posting's hard
requirements as structured items. The review reads the posting text captured before the
Apply link was followed; the form's own labels are passed separately as questions, never
as requirements. Qwen judges what code cannot: program type, degree field, required
skills, location rules, explicit conditions. Trusted code performs the exact comparisons
and can overrule Qwen's arithmetic:

- graduation windows are compared inclusively with the approved graduation month, and
  a window month counts only when the posting or the quoted requirement states it
- work-authorization and sponsorship requirements are compared with approved facts
- approved nationwide relocation plus accepted onsite work settles location rules

The decision is computed by code from the requirement statuses, and only a conflict on
an eligibility requirement (program, graduation window, work authorization,
sponsorship, location, degree) changes it. A code-verified conflict is `not_fit`; a
conflict only Qwen claims is `needs_review`; otherwise the job is `fit`. Eligibility
the posting states but code cannot check against the profile does not hold the job:
the thread's job-fit card lists it as stated by the posting, and the ready-to-submit
card repeats it under Why, so you decide once, at submit time. Skills, dates, and
other wishes never affect the decision either; they are left to the resume and the
written answers. A requirement Qwen files as a graduation window that never mentions
graduating (an internship term, for example) is reclassified as dates. Either non-fit
decision holds a feed job with one card in the thread and one in the shortlist channel
(nothing goes to action-needed for a fit hold) and waits for `proceed` or `defer`. The
card's summary and reasons are the conflicting requirements themselves, plus any
unchecked eligibility, not Qwen's prose. A link you pasted is never held on fit. Qwen's
raw extraction is kept, so improved code rules re-evaluate old reviews without another
model call. Reviews cache on posting text, profile version, and prompt version.

## Preparation and questions

The worker creates an application forum post before preparation. It opens the posting,
follows bounded observed application-start controls, uses Erga's supported job intake
(falling back to the approved base PDF with a warning when tailoring fails), and fills
deterministic approved fields as a batch. The observer reads associated labels first and
falls back to the nearest label that owns no other control, reports a radio group as one
question with options, and reports a Yes/No button group as one choice. Code selects an
option only when an approved fact matches exactly one option and verifies the selection.
Typed values are verified too: a site that trims whitespace or prefixes a phone number
with its country code still passes; any other difference stops preparation with a card
that names the field.
A page whose known fields are complete and that shows a Next/Continue control instead of
a final submit is advanced once and filled again, for at most four steps.

Unfamiliar questions go to **Qwen through Hermes**. The request is trimmed to the local
model's window, runs as one non-streamed call with at most one harness continuation, and
is retried once when the answer is malformed; a run that still does not finish is a
`qwen_failure` card, never a draft. A proposed option that is not on the form's list
becomes a question for the owner. Written drafts pass through the Unslop contract: the
drafting prompt carries its rules, every draft is scanned, a flagged draft gets one
bounded repair that must keep every number and name, and the card shows the cleaned text
with a before/after summary.

Boards that need an account are recognized. Your policy asks first: the card offers
`account APPLICATION_ID create`; on approval the daemon fills the application email and a
generated password, accepts the site's terms checkbox, clicks the create control, and
stores the credential encrypted on this Mac. Later sign-in pages on that host are
completed with the stored account. Email verification, CAPTCHA, MFA, and identity checks
stay with the owner in the recruiting browser.

Owner commands are accepted only from the configured numeric owner in the control,
action-needed, and matching forum channels. They are parsed by deterministic code:

```text
answer APPLICATION_ID FIELD_KEY = value      bind an answer to an observed question
answer APPLICATION_ID FIELD_KEY = skip       optional questions only
use APPLICATION_ID FIELD_KEY PROPOSAL_HASH   approve exactly one Qwen draft
resume APPLICATION_ID                        prepare again with the new answers
defer APPLICATION_ID                         park it and let the queue continue
proceed APPLICATION_ID                       override a job-fit hold
account APPLICATION_ID create                allow one account on this board
submit APPLICATION_ID PACKAGE_HASH           send one reviewed package once
reconcile APPLICATION_ID applied             owner applied or confirmed manually
reconcile APPLICATION_ID not-submitted       owner verified nothing was sent
```

`PROPOSAL_HASH` and `PACKAGE_HASH` accept a prefix of 8 to 64 hex characters of the
current hash; cards show the first 12. A prefix that does not match the current draft or
package is rejected.

Every hold is one card in the thread and one in an owner channel (action-needed, or
shortlist for a fit hold): what happened, why, the questions only you can answer, and
the exact reply commands in a code block. The "Answers needed" card lists only the
questions Qwen could not draft (up to six, numbered, with their options) and a bare
`answer APPLICATION_ID FIELD_KEY = ` line for the first four to complete; drafts are
approved from their own cards in the thread.

`action-needed` and `shortlist` are to-do lists. An application has at most one live
card in each; a new card replaces the previous one, and the cards are withdrawn as
soon as the application stops waiting on you (any state other than `NEEDS_USER`,
`READY_FOR_REVIEW`, `MANUAL_TAKEOVER`, or `UNKNOWN_SUBMISSION`). The thread's first
post is edited into a live status card (headline, one line, the reply commands) so the
forum list previews the current state; the entries below it stay the full
chronological record. Routine steps there (opened, clicked Apply, resume ready, your
replies, lifecycle changes, the submit click) are one-line messages; cards are kept for
decisions, drafts, job fit, the filled form, submission results, and failures. Sources
are shown in words ("your profile", "your reply", "your evidence", "the posting"),
never as internal keys.

Optional fields with no approved fact and no Qwen draft are left blank and noted in one
line; only required questions reach the owner. A phone-type field defaults to Mobile and
the form card shows that source as a default. Country selects match the approved country
under its common spellings, and an observed select keeps up to 300 options so long lists
such as countries are not cut off. "(Optional)" and "(Required)" in a label are ignored
when a field is matched to a profile fact.

## Submission

Submission runs only from an authenticated `submit` command whose hash matches the
current package of a `READY_FOR_REVIEW` application, and only when private
`config/workflow.json` sets `submission_enabled` and lists an adapter in
`submit_adapters`. The model has no submit tool.

Before the click, trusted code re-observes the live page and requires the same URL and
job scope, the same form state and final control, the current approved profile version,
the frozen resume bytes present and uploaded, and every required answer or committed
selection. It records the attempt in SQLite, sets `SUBMITTING`, arms the preparation
guard for that single click through a DOM attribute, and clicks once.

`greenhouse_v1` covers the public Greenhouse job board. Its client posts JSON to
`boards.greenhouse.io/{board}/jobs/{id}` and, only on success, navigates to
`/{board}/jobs/{id}/confirmation`, which renders `.confirmation__content`. The attempt is
`APPLIED` only when all hold: a 2xx POST to that path, no rejected POST, the confirmation
URL, the confirmation block, and no form left. The receipt keeps the confirmation URL and
text, screenshots before and after, response statuses without bodies, and the package
hash. The application is confirmed in Erga and the tag becomes `Applied`. Anything else
stays `UNKNOWN_SUBMISSION` until the owner reconciles; nothing retries.

Public Greenhouse boards run an invisible reCAPTCHA on submit. A challenge or an emailed
security code is recorded as an unknown submission for the owner to finish and reconcile.

`generic_v1` covers employer sites without an ATS contract. List it last in
`submit_adapters`: the first listed adapter that matches the page wins, so Greenhouse
boards keep their stricter contract. It clicks the one observed final control and waits,
within the same bound, until the page leaves, the form disappears, or a success or
validation message shows that was not there before the click. It then reads the page
once and compares it with the observation taken before the click. The attempt is
`APPLIED` only when all three hold:

- at least one new confirmation signal: the URL path or query newly matches the `url`
  pattern below; the page text newly matches the `sentence` pattern; or a visible
  `[role=alert]`, `[role=status]`, `.confirmation`, or `.success` element newly matches
  the `sentence` pattern
- the form left: no fields and no final control, or a different URL
- no new validation message: visible text in `[role=alert]`, `.error`, or the message
  beside an `[aria-invalid=true]` field that matches the `error` pattern and differs from
  what was there before the click

The patterns, all case-insensitive:

```text
url       confirmation|thank|success|submitted|complete|received
sentence  thank you for (applying|your (application|interest))
          |application (has been |was )?(submitted|received|complete)
          |we('ve| have) received your application
          |successfully (submitted|applied)
error     required|invalid|error|could not|try again
```

Wording or URL tokens already present before the click never count, so a careers page
that opens with "thank you for your interest" cannot confirm itself, and a thank-you
sentence under a form that is still open is not a confirmation. A new validation
message means the form rejected the attempt: the tab stays open and the application is
`UNKNOWN_SUBMISSION` with the message in the reason. Anything else within the bound is
`UNKNOWN_SUBMISSION` as well. POST responses to the page's host are kept in the receipt
as evidence; they confirm nothing on their own. The receipt carries the same fields as a
Greenhouse receipt, so cards and the Erga confirmation work unchanged.

### Unattended sending as an owner policy

Two private `config/workflow.json` keys turn the review step into an after-the-fact one:

- `auto_use_drafts`: Qwen's drafts become the answers without a per-draft `use` reply.
  The draft cards stay in the thread; an `answer` reply before sending still overrides.
- `auto_submit`: a complete package is sent once, on the tick that prepared it, through
  the enabled adapter for that site. The thread records "auto-submit is on · sending
  once", then the result card. `max_submissions_per_day` (default 10) and
  `min_minutes_between_submissions` (default 8) pace unattended sending the way one
  person would apply; an application the owner resumed or pasted is never capped.

Two defaults keep unattended runs from stalling on routine questions. A voluntary
self-identification question (gender, race or ethnicity, veteran or disability status)
takes the form's own decline option, recorded with the source
`policy.decline_self_identification`; a form without a decline option stays with the
owner. A page that says the applicant already applied stops the run before anything is
sent and asks the owner to mark it applied or park it.

What still stops and asks: a required question only the owner can answer, an
eligibility conflict on a feed job, a sign-in or account wall, a blocked site, a
CAPTCHA or identity step, a site with no enabled adapter, and any unclear submission
result. Nothing is ever sent twice for the same package.

## Records outside Discord

SQLite holds the queue, events, answers, commands, and attempts. Private per-application
directories hold the observation, package, resume, receipts, screenshots, and Qwen input
and output. The vault gets one readable note per application under
`Erga Autopilot/Applications/` (status, links, job fit, filled values with sources, open
questions with drafts, timeline), rewritten on every change; it mirrors local state and is
never a candidate fact. Credentials live only in the encrypted local store.

## Local configuration

Private `config/workflow.json` maps `guild_id`, `forum_channel_id`, `control_channel_id`,
`action_channel_id`, `source_channel_id`, `shortlist_channel_id`, and lifecycle `tags`.
It also supplies `hermes_python`, an explicit `enabled` flag, `submission_enabled`,
`submit_adapters`, `max_waiting_applications` (default 1), `browser_app` (`chrome` or
`chromium`), `max_open_tabs` (default 5), `human_pacing` (default true), and an optional
`unslop_path` pointing at a local clone of the Unslop repository. Private
`config/feed.json` holds the feed's `enabled` flag, the jobs channel's `channel_id`,
`batch_size` (cards per feed tick, default 10), and `max_pending` (pending
announcements kept, default 40).

```sh
uv run autopilot install-services
uv run autopilot feed seed
uv run autopilot workflow status
uv run autopilot workflow enqueue --url https://jobs.example.com/internship
uv run autopilot workflow tick
uv run autopilot workflow resume --id APPLICATION_ID
uv run autopilot browser status
```

`workflow resume` and `workflow defer` are local owner operations equivalent to the
Discord commands. Delivery records are written before Discord mutations. Thread entries and the
action-needed and shortlist cards are stored first and posted after; a failed post is
logged to `logs/delivery-failures.log` under the state root and retried on every worker
tick. A failed card withdrawal or status-card edit is logged there too, but not retried.
Each tick also re-checks queued feed jobs against the approved exclusion rules and
defers the ones that now match, with the reason in the queue record; a link you pasted
is never pruned that way. The worker takes an application you told to `resume`,
`proceed`, or `account ... create` first, then a pasted link before a feed job, and
otherwise the newest queued job. An ambiguous forum creation is held for
reconciliation. The worker holds the only processing lock, so a `PREPARING` application
older than fifteen minutes is a crashed run and is handed back to the owner. When the
local model server is down the worker starts it once and otherwise leaves the queue
waiting instead of failing applications. After updating browser code, restart the
browser service (`launchctl kickstart -k gui/$UID/dev.erga-autopilot.browser`); the
Chrome window and its tabs survive the restart.

## Hermes tools

The production include list contains thirteen narrow tools: the nine onboarding,
discovery and evidence tools from [Onboarding and jobs](onboarding-and-jobs.md) plus
`start_job_application`, `application_workflow_status`, `inspect_application_browser`,
and `refresh_job_feed`. None of them fills, approves, or submits.

## Limits

Two submission adapters: `greenhouse_v1`, with a request-level contract for public
Greenhouse boards, and `generic_v1`, which reads only the page and confirms nothing
without new confirmation wording. Multi-page support advances only on
Next/Continue controls after a complete page. Account creation covers email, password,
terms checkbox and known name fields; anything else on a registration page is a hold.
No CAPTCHA solving (a visible challenge stops and asks), no proxies, no recruiting-mail tracking. Unattended submission is an owner policy with a daily cap and a minimum gap.
Natural-language owner replies are not converted into commands; the strict forms above
are required.
