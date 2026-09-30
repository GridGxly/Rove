# Application workflow

The local workflow joins Keryx discovery and owner-pasted links in one durable queue,
prepares each application in a visible browser, drafts answers with Qwen through Hermes,
and submits one reviewed package per owner approval on supported boards. Recruiting-mail
reconciliation and unattended submission are not implemented.

## Intake and visibility

`autopilot feed tick` checks the fixed Keryx source. An unchanged revision does not
redownload the snapshot. New matching internships enter a deduplicated notification
outbox and application queue; each job is announced once, and later Keryx metadata
changes to a known job do not re-post it. Each tick publishes at most 25 matches in
small grouped messages, so a large backlog drains over several ticks. `autopilot feed
seed` publishes the first 25 matches. All recruiting terms are included when the
approved profile selects any term. The installation maps the logical source channel to
an existing Discord channel in private `config/feed.json`.

`start_job_application(url)` accepts owner-requested public HTTPS links independently
of the feed. A Jobright identifier is never treated as a Keryx identifier. Verified
source-to-employer aliases live in SQLite; unknown redirects or account requirements
need review. Queue uniqueness prevents a second application for the same canonical URL.

The browser is a visible, persistent, dedicated Chromium profile. Its control interface
is a private Unix socket, not a network listener. The model cannot select arbitrary
files, execute JavaScript, use a shell, choose arbitrary click selectors, or submit.

## Job-fit review before any applicant data

When the worker reaches an application form it asks Qwen to extract the posting's hard
requirements as structured items. Qwen judges what code cannot: program type, degree
field, required skills, location rules, and explicit conditions. Trusted code performs
the exact comparisons and can overrule Qwen's arithmetic:

- graduation windows are compared inclusively with the approved graduation month
- work-authorization and sponsorship requirements are compared with approved facts
- approved nationwide relocation never becomes a location conflict on its own

The final decision is computed by code. A code-verified conflict is `not_fit`; a Qwen
doubt or unknown is `needs_review`. Either holds the application, posts the reasons to
the shortlist channel, and waits for `proceed APPLICATION_ID` or `defer APPLICATION_ID`.
Reviews are cached per posting text, profile version, and prompt version, so a prompt
change never reuses an older answer.

## Preparation and questions

The worker creates an application forum post before preparation. It opens the posting,
follows bounded observed application-start controls, uses Erga's supported job intake,
and fills deterministic approved fields as a batch. Exact profile and resume hashes,
field values, provenance, unresolved questions, and screenshots remain private artifacts.
The forum mirrors meaningful events. A failed tailored resume is never reported as
validated: an approved base PDF can be preserved with an explicit review warning.

Unfamiliar questions go to **Qwen through Hermes**. The reasoning step receives bounded
approved context, the observed question keys and their options, and exposes no mutation
tools. It runs as one non-streamed request with at most one harness continuation; a run
that does not finish is recorded as a `qwen_failure` event and never presented as a
draft. Its JSON output is schema-validated and saved as an unapproved proposal. It cannot
approve facts, change the profile, upload a file, or submit. Unknown personal facts
return to the owner. Known profile fields are handled by code; Qwen is not called for
every keystroke.

Owner commands are accepted only from the configured numeric owner in the control,
action-needed, and matching forum channels. They are parsed by deterministic code:

```text
answer APPLICATION_ID FIELD_KEY = value      bind an answer to an observed question
answer APPLICATION_ID FIELD_KEY = skip       optional questions only
use APPLICATION_ID FIELD_KEY PROPOSAL_HASH   approve exactly one Qwen draft
resume APPLICATION_ID                        prepare again with the new answers
defer APPLICATION_ID                         park it and let the queue continue
proceed APPLICATION_ID                       override a job-fit hold
submit APPLICATION_ID PACKAGE_HASH           send one reviewed package once
reconcile APPLICATION_ID applied             owner verified the employer confirmation
reconcile APPLICATION_ID not-submitted       owner verified nothing was sent
```

Secrets and identity checks remain manual. A required field cannot be skipped. The owner
can inspect and take over the visible page at any time.

When every question is resolved, preparation ends in `READY_FOR_REVIEW` with a package
hash covering the URL, profile version, resume hash, every filled value and its source,
the observed form state, and the single final control. The action-needed message quotes
the exact `submit` command for that hash.

## Submission

Submission runs only from an authenticated `submit` command whose hash matches the
current package of a `READY_FOR_REVIEW` application, and only when private
`config/workflow.json` sets `submission_enabled` and lists an adapter in
`submit_adapters`. The model has no submit tool.

Before the click, trusted code re-observes the live page and requires the same URL and
job scope, the same form state and final control, the current approved profile version,
the frozen resume bytes present and uploaded, and every required answer or committed
selection. It then records the attempt in SQLite (`live_submission_attempts`) and sets
`SUBMITTING`, arms the preparation guard for that single click, and clicks once.

`greenhouse_v1` covers the public Greenhouse job board (`job-boards.greenhouse.io`). Its
client posts JSON to `boards.greenhouse.io/{board}/jobs/{id}` and, only on success,
navigates to `/{board}/jobs/{id}/confirmation`, which renders the employer's message in
`.confirmation__content`. The attempt is `APPLIED` only when all of these hold: a 2xx
POST to that path was observed, no rejected POST was observed, the page is on the
confirmation URL, the confirmation block exists, and no application fields or final
controls remain. The receipt keeps the confirmation URL and text, screenshots before and
after, the response statuses without bodies, and the package hash. The application is
then confirmed in Erga through its supported operation and the forum tag becomes
`Applied` with a timeline entry.

Anything else, including a timeout, a crash, or an ATS rejection, leaves the application
in `UNKNOWN_SUBMISSION`. Nothing retries. The owner checks the visible browser and
employer email, then replies `reconcile ... applied` or `reconcile ... not-submitted`;
the latter returns the application to review and allows one new attempt after a fresh
package hash is approved.

Public Greenhouse boards run an invisible reCAPTCHA assessment on submit. A low score
makes the board reject the post and ask for an emailed security code or a retry. Autopilot
never solves a CAPTCHA or enters a code: that outcome is recorded as an unknown
submission, the owner completes the step in the visible browser if they choose, and then
reconciles the result.

## Local configuration

Private `config/workflow.json` maps `guild_id`, `forum_channel_id`, `control_channel_id`,
`action_channel_id`, `source_channel_id`, `shortlist_channel_id`, and lifecycle `tags`.
It also supplies the installed `hermes_python`, an explicit `enabled` flag,
`submission_enabled`, `submit_adapters` (for example `["greenhouse_v1"]`), and
`max_waiting_applications` (default 1: how many applications may wait on the owner
before the queue holds; an explicit `resume` or `proceed` always runs). Credentials are
read from private setup/Hermes configuration and never returned by MCP.

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
Discord commands. A paused application retains the visible session. Delivery records are
written before Discord mutations. An ambiguous forum creation or message delivery is held
for reconciliation instead of automatically creating duplicate records. The current owner
command bootstrap starts at the current channel cursor; it does not replay old approvals.

The worker holds the only processing lock, so a `PREPARING` application older than
fifteen minutes is a crashed run. It is moved to `NEEDS_USER` with an explanation and the
visible browser is left for inspection; nothing is retried automatically.

## Hermes tools

The production include list contains thirteen narrow tools: the nine onboarding,
discovery and evidence tools from [Onboarding and jobs](onboarding-and-jobs.md) plus
`start_job_application`, `application_workflow_status`, `inspect_application_browser`,
and `refresh_job_feed`. None of them fills, approves, or submits; the queue worker owns
the browser and the owner commands above own approvals.

## Browser comparison and limits

A direct-link access denial is not an application rejection. Never label it Applied.
Compare the same posting and navigation path before replacing the browser runtime.

The [referenced browser article](https://ashu.io/blog/gave-my-ai-unblockable-internet/)
combines search/extraction services, Camofox, and a residential proxy. These solve
different problems and are not a single drop-in application workflow. The underlying
[Camoufox Python engine](https://camoufox.com/python/usage/) supports visible windows
and Playwright-compatible code. The Camofox server has a broader REST surface and
optional telemetry; neither is silently enabled in production.

A local comparison found both dedicated Chromium and Camoufox could receive an access
denial on the same employer, while an existing ordinary browser session could load it.
This does not prove an IP block or that a proxy is needed. The default remains the
isolated visible Chromium session. The comparison did not copy personal-browser cookies,
add a proxy, solve a CAPTCHA, disable TLS checks, or send a production application.

Current limits: one submission adapter (public Greenhouse boards), no multi-page or
account-based ATS flows, no CAPTCHA or MFA handling, no recruiting-mail tracking, and no
unattended submission. Natural-language owner replies are not converted into commands;
the strict forms above are required. The synthetic tests prove the guard, the contract,
and the reconciliation path; a real submission still needs the owner's `submit` command.
