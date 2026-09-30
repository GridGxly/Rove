# Application workflow

The local workflow joins Keryx discovery and owner-pasted links in one durable queue,
prepares each application in a background recruiting browser, drafts answers with Qwen
through Hermes, and submits one reviewed package per owner approval on supported boards.
Recruiting-mail reconciliation and unattended submission are not implemented.

## Intake and visibility

`autopilot feed tick` checks the fixed Keryx source. An unchanged revision does not
redownload the snapshot. New matching internships enter a deduplicated notification
outbox and application queue; each job is announced once, and later Keryx metadata
changes to a known job do not re-post it. Each tick publishes at most 25 matches, one
card per job (title, company, location, cycle, track, application id), so a large
backlog drains over several ticks. A posting that closes in Keryx parks its queued
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

The decision is computed by code from the requirement statuses. A code-verified
conflict is `not_fit`; a Qwen doubt or unknown is `needs_review`. Either holds the
application, posts one card to the shortlist channel with the reasons, and waits for
`proceed` or `defer`. Qwen's raw extraction is kept, so improved code rules re-evaluate
old reviews without another model call. Reviews cache on posting text, profile version,
and prompt version.

## Preparation and questions

The worker creates an application forum post before preparation. It opens the posting,
follows bounded observed application-start controls, uses Erga's supported job intake
(falling back to the approved base PDF with a warning when tailoring fails), and fills
deterministic approved fields as a batch. The observer reads associated labels first and
falls back to the nearest label that owns no other control, reports a radio group as one
question with options, and reports a Yes/No button group as one choice. Code selects an
option only when an approved fact matches exactly one option and verifies the selection.
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

Every hold is one card in the thread and one in action-needed: what happened, the open
questions with their keys, and the exact reply commands in a code block.

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
`unslop_path` pointing at a local clone of the Unslop repository.

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
tick. Each tick also re-checks queued feed jobs against the approved exclusion rules and
defers the ones that now match, with the reason in the queue record; a link you pasted
is never pruned that way. An ambiguous forum creation is held for
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

One submission adapter (public Greenhouse boards). Multi-page support advances only on
Next/Continue controls after a complete page. Account creation covers email, password,
terms checkbox and known name fields; anything else on a registration page is a hold.
No CAPTCHA solving, no proxies, no recruiting-mail tracking, no unattended submission.
Natural-language owner replies are not converted into commands; the strict forms above
are required.
