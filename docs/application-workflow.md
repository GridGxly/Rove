# Application workflow

The local workflow joins Keryx discovery and owner-pasted links in one durable queue,
prepares each application in a background recruiting browser, drafts answers with Qwen
through Hermes, and submits one reviewed package per owner approval on supported boards.
Recruiting mail from Zoho moves a sent application through OA, interview, offer and
rejected (see [Recruiting mail](#recruiting-mail)); unattended submission is an owner policy (see below).

## Intake and visibility

`rove feed tick` checks the fixed Keryx source. An unchanged revision does not
redownload the snapshot. New matching internships enter a deduplicated notification
outbox and application queue; each job is announced once, and later Keryx metadata
changes to a known job do not re-post it. Each tick publishes the newest pending
matches first, at most `batch_size` (default 10) of them, one card per job (title,
company, location, cycle, track, and a "Queued" footer). Before posting, pending announcements
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
  `max_open_tabs` (default 5); a closed tab reopens on `go`.
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
link (`MANUAL_TAKEOVER`); replying `applied` in the thread then records a manual application.
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
(nothing goes to action-needed for a fit hold) and waits for `go` or `park it`. The
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
becomes a question for the owner.

Written drafts follow two public rule sets, [Unslop](https://github.com/theclaymethod/unslop)
and [Humanizer](https://github.com/blader/humanizer). The drafting prompt carries their
rules in a few sentences. Every draft is scanned for their tells: jargon, "not X but Y",
run-ups, closers that restate the answer, hedge stacks, lists of three for rhythm, dashes
as connectors, inflated words, borrowed authority, chatbot leftovers, curly quotes. A hard
tell, or two soft ones, gets one bounded Qwen repair that must keep every number and
name; the card shows the cleaned text with a before/after summary. The Unslop scanners run
from a local clone when `unslop_path` is set; the Humanizer digest is built in and runs
either way. A phrase from the posting goes into a draft only when the approved evidence
shows it, so the words an applicant-tracking system matches on stay true.

If the vault has `Rove/Story/Voice.md`, about 2,500 characters of it go to Qwen
as `owner_voice`: the draft follows its sentence rhythm, plain words, first person and
concrete detail without copying its sentences, and the note is never a source of facts.
Rove reads that note and never writes it; the draft is re-made when it changes.

Before Qwen drafts, trusted code gathers company research once per application. It reads
at most three public pages from the employer's own site: the home page, the about or
company page it links to, and the careers or culture page (`/about` and `/careers` when
the home page links to neither). Plain HTTPS with a browser-like agent string, a
ten-second timeout, 400 KB per page, the same site only (`www.` counts), no sign-in, no
cookies kept, and nothing about the applicant in the request; a redirect off the site is
not followed. The employer site is the posting's host when the posting is on the
employer's own site (a `careers.`, `jobs.` or `www.` label dropped), otherwise the
employer host the posting text links to most often. A posting on an ATS host or a job
board that names no employer link gets no research, and the draft says only what the
posting says about the company. Scripts, navigation, footers and hidden elements are
never read. The pages are reduced to the sentences that say what the company does, how
big or old it is, what it sells and what it values, about 1,800 characters, and go to Qwen
as `company_research`. Any line that reads as an instruction to a model (ignore,
instruction, you are, system prompt, assistant and the like) is dropped first, which also
drops a few true sentences. Qwen is told to use the text only to say true things about
the company and to tie the approved evidence to them, never to claim the applicant did
anything with the company, and never as instructions. The result is cached under the
application (`research.json`, with the URLs and the fetch time) and copied to
`Rove/Research/<employer host>.md` in the vault, marked untrusted. A site that
does not answer is noted privately and tried once more on a later preparation; the draft
goes without research, and research never stops a run.

Boards that need an account are recognized. Your policy asks first: the card offers
`create account`; on approval the daemon fills the application email and a
generated password, accepts the site's terms checkbox, clicks the create control, and
stores the credential encrypted on this Mac. Later sign-in pages on that host are
completed with the stored account. Email verification, CAPTCHA, MFA, and identity checks
stay with the owner in the recruiting browser.

## Replying in the thread

You reply inside the application's own forum thread, where the bot already knows which
application is meant, so a reply is a word or a number. Replies are accepted only from
the configured numeric owner; they are matched by deterministic code, case-insensitively,
as the whole message, with trailing punctuation ignored:

```text
go · proceed · continue · resume    carry on: prepare again, or accept a job-fit hold
park it · park · defer · later · skip   park it and let the queue continue
send it · send · submit · apply · apply now   send the reviewed package once
use draft 2 · draft 2 · use 2       approve Qwen's second draft, exactly as shown
2: value · 2 = value · answer 2: value   answer question 2 from the hold card
2: skip                              leave an optional question blank
applied · i applied · done · sent it myself   you applied or confirmed it yourself
not sent · not submitted · nothing sent       you verified nothing was sent
create account · make an account · account    allow one account on this board
```

Questions are numbered on the hold card; the numbers count every question the form
asked beyond your approved facts, in form order, whether it is open, drafted by Qwen, or
already answered with a draft under your policy. Drafts are numbered separately, in the
order Qwen wrote them; each draft card names its own `use draft N` reply. `send it` binds
to the package that is ready right now and is refused while nothing is ready. Any other
text in a thread is ignored. A reply that cannot apply (`send it` when nothing is ready,
`use draft 3` when there are two) gets one plain line in the thread saying why.

The control channel has no implied application, so the explicit forms work there and in
any thread: `resume|defer|proceed APPLICATION_ID`, `account APPLICATION_ID create`,
`submit APPLICATION_ID PACKAGE_HASH`, `reconcile APPLICATION_ID applied|not-submitted`,
`use APPLICATION_ID FIELD_KEY PROPOSAL_HASH`, and `answer APPLICATION_ID FIELD_KEY = value`.
The hashes accept a prefix of 8 to 64 hex characters of the current value and are rejected
when they do not match the current draft or package. The ids live in `system-log`.

Every hold is one card in the thread and one in an owner channel (action-needed, or
shortlist for a fit hold): what happened, why, the numbered questions, and the replies
in a code block. The "Answers needed" card lists the questions only you can answer under
"Only you can answer" and the ones Qwen drafted under "Qwen drafted" (with the `use draft
N` reply that approves each, or the `N: your text` reply that replaces a draft already
used), then a bare `N: ` line for the first four open questions, `go`, and `park it`.

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

A label is matched to a profile fact by its meaning when it is short and plain: "Profile
Link (Optional)" is the portfolio, "LinkedIn Profile URL" is LinkedIn, "Mobile Number" is
the phone. A label that carries instructions or names another person is never matched.
A US phone is typed as ten national digits first, because sites with their own country
selector reject a repeated code; if the site still rejects it, the international form is
tried once. A draft must fit its field: the observation records each field's character
limit, Qwen is told it, one retry asks for a shorter answer, and a remaining overflow is
cut at a sentence boundary and marked as shortened. A "rising senior" style requirement
is decided by code from the approved graduation month and the internship year.

A typeahead (a text input with an autocomplete hint or a "start typing" placeholder) is
treated like a combobox: the value is typed and the matching suggestion is picked; when
the suggestion list has no ARIA roles, the first suggestion is taken and accepted only if
the committed text still names the approved city. When a site rejects a form naming a
"required field", that label is remembered for the application and treated as required on
the next preparation. A text the site silently truncates is cut to what the field keeps,
at a sentence boundary, and the form card says so.

A field counts as required when the input says so or when its label carries a
"required" class or a trailing asterisk, which is how Ashby and Lever mark it. A place
typeahead (location, city) is typed into and the one suggestion that starts with the
approved "City, State" is chosen. After a Next control the runtime waits for the next
step's fields before reading it, and a form whose last step with its Submit control was
never reached is handed back with "Final step not reached", never called ready. A visible
CAPTCHA challenge stops the run and asks the owner to solve it in the recruiting browser.

Optional fields with no approved fact and no Qwen draft are left blank and noted in one
line; only required questions reach the owner. A phone-type field defaults to Mobile and
the form card shows that source as a default. Country selects match the approved country
under its common spellings, and an observed select keeps up to 300 options so long lists
such as countries are not cut off. "(Optional)" and "(Required)" in a label are ignored
when a field is matched to a profile fact.

## Submission

Submission runs only from an authenticated `send it` reply (or an explicit `submit`
command) bound to the current package hash of a `READY_FOR_REVIEW` application, and only when private
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

`lever_v1` covers public Lever postings (`jobs.lever.co/{company}/{posting}/apply`). The
apply page is one multipart form. Its "Submit application" button runs hCaptcha first;
when the CAPTCHA hands the page a token, the page posts the form itself. The attempt is
`APPLIED` only when all hold: the page landed on `/{company}/{posting}/thanks` for the
same posting, it shows the "Application submitted!" heading, and no form is left. The
status of the form's POST (a redirect to the thanks page on success) is kept in the
receipt as evidence; it confirms nothing on its own. When the page comes back as the
emptied form under "There was an error verifying your application", Lever's CAPTCHA
rejected the send and nothing was stored: the attempt is recorded as not submitted, the
tab stays open on the form, and the application is handed to you (`MANUAL_TAKEOVER`)
with one card: solve the CAPTCHA in the recruiting browser, press Submit yourself, then
reply `applied` (`park it` also works). A form that stays open with a new validation
message is not submitted either and goes back to "needs you" like any rejected form.
Anything else is an unknown submission. The form, the submit button and the thanks page
were checked against the live public DOM; what Lever shows when hCaptcha rejects a send
has not been observed in a live run, so that path follows Lever's reported wording until
one confirms it. Employers can refuse repeat applications; a page that says you already
applied stops the run before anything is sent.

`generic_v1` covers employer sites without an ATS contract. List it last in
`submit_adapters`: the first listed adapter that matches the page wins, so Greenhouse
boards and Lever postings keep their stricter contracts. It clicks the one observed final
control and waits, within the same bound, until the page leaves, the form disappears, or
a success or validation message shows that was not there before the click. It then reads
the page once and compares it with the observation taken before the click. The attempt is
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

### Remembered answers

A fact the owner answers once in Discord (`3: 5 months`) is stored under a fingerprint of
the question's wording without qualifiers plus its options, and filled on any later form
that asks the same question, with the source "your earlier answer". When the new form
offers options, the remembered value is used only if it is one of them. `skip` is never
remembered. The exact store is the `answer_memory` table in SQLite; a readable copy is
`Answers.md` in the vault's `Rove` folder. Approved profile facts come first,
remembered answers second, Qwen drafts third; only what none of them covers reaches the
owner.

### Unattended sending as an owner policy

Two private `config/workflow.json` keys turn the review step into an after-the-fact one:

- `auto_use_drafts`: Qwen's drafts become the answers without a per-draft `use draft`
  reply. The draft cards stay in the thread, each used draft gets a line naming the
  question's number, and a `N: your text` reply before sending still overrides.
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

When the site keeps the form open and names a validation error after the single click,
nothing was sent: the attempt is recorded as not submitted, the application goes back to
"needs you" with the site's message, and a later `go` prepares a new package. A send that
Lever's CAPTCHA rejected is not submitted either, but it is handed to you instead, since
only a person can satisfy the CAPTCHA. Only an
outcome the page cannot settle (navigation without a confirmation, a timeout, a crash)
becomes "unclear" and blocks all sending until the owner reconciles it.

### Debugging a stop

Every stop is visible without opening the Mac. The thread gets the browser screenshot
taken when the run stopped, the resume PDF as it was sent, and, when Erga tailored a
resume that failed its layout check, that rejected draft rendered to PDF with the reason
(for example the share of the page it fills). A send that did not confirm posts the form
just before the click and the page after it. Identifiers, adapters and package hashes go
to `system-log`. Privately, under the application's folder in the state root, the daemon
keeps `failure.png` for any action that raised, and for a picker that refused a value a
`picker-<field>.json` with what was typed, the options it listed and what it kept, plus a
screenshot.

## Recruiting mail

`rove mail tick` runs every 15 minutes as its own launchd service and is off until
private `config/mail.json` sets `enabled` and the private env holds the four Zoho values
(see [Requirements](requirements.md#zoho-mail)). Each tick refreshes a Zoho access token,
lists the Inbox messages newer than the checkpoint (`mail_checkpoints` in SQLite; the first
tick looks back `lookback_days`, default 3), and reads each message's body as plain text.
A message is handled once (`mail_messages`), and the checkpoint advances past each one.

Mail is matched to a sent application (`APPLIED` or later, or an unclear submission)
before anything else happens:

- strong: the sender's domain is the employer's domain from the posting URL, or the sender
  is a known recruiting host (Greenhouse, Lever, Ashby, Workday, HackerRank, and the like)
  and the company name appears in the subject or body
- weak: the company name appears in the subject only, from any other sender; this counts
  only when the rules below recognise the mail
- anything else is ignored and leaves no trace but its message id

When two applications match (two roles at one company), the role's own words in the mail
decide; a tie goes to the most recently updated one.

The label comes from fixed rules, in this order of precedence:

```text
rejection        not moving forward · other candidates · not selected · unable to offer ·
                 regret to inform · no longer under consideration · position has been filled ·
                 "unfortunately … not / unable / other"
offer            offer letter · pleased to extend an offer · offer of employment · job offer
interview        interview · phone screen · schedule a call / time / chat · your availability ·
                 book a time · calendly.com · meet the team
oa               online assessment · HackerRank · CodeSignal · Codility · coding challenge ·
                 take-home · technical assessment · complete the assessment
acknowledgement  thank you for applying · we received your application · application
                 submitted / received / under review · we will be in touch
```

A rejection outranks everything it mentions; an offer outranks the interviews before it.
When a mail matches both interview and assessment wording (an invitation that mentions the
assessment it followed, or an assessment that promises interviews), the subject line
decides; when the subject names neither, the mail is ambiguous. Only an ambiguous mail
from a strong match goes to Qwen, with a sanitized excerpt (links, addresses, markup and
any sentence that talks to a model or about secrets removed, 2,500 characters at most) and
a prompt that lets it pick one of the six labels and nothing else; a deadline Qwen quotes
counts only when the text contains it. When the local model is down, the tick stops at
that mail and resumes there next time. A Qwen answer that is not a label files the mail as
"other", from the sender alone.

What a classified mail does:

- the thread gets a `recruiting_mail` card: the label, the sender's domain, the subject
  clipped, and the deadline as the mail states it (a regex quotes "by October 9, 2026 at
  11:59 PM PT" or "within 72 hours"; nothing is computed), never the body
- the application moves forward when the label is a step forward: `APPLIED → OA →
  INTERVIEW → OFFER`, and `REJECTED` from any of them; the lifecycle line names the trigger
  `recruiting mail: <label>`; a label behind the current state (an assessment reminder
  during interviews) is recorded without a move, and nothing moves a `REJECTED` application
- the `recruiting` channel gets one line: the label in words, the application, the
  sender's domain, the subject and a link to the thread
- Erga is asked to `update_application_status` with its own word (`oa`, `interview`,
  `offer`, `rejected`) when the resume manifest links an Erga application; the result or
  the error type is kept in the card's data, and a failure changes nothing locally
- a mail of any of the five kinds for an `UNKNOWN_SUBMISSION` application settles it: the
  attempt is confirmed `APPLIED` with the mail (its id, sender, subject and the private
  copy) as the receipt's evidence, and an assessment or interview then moves it on

Nothing in a mail can queue, prepare, submit or re-prepare an application, and an
application that was sent is never moved back into preparation, whatever a thread reply
says. The full text of each handled mail is kept privately under `mail/messages/<id>/` in
the state root, with Qwen's input and output beside it when it ran.

`rove mail status` shows the switches, the checkpoint and the message counts
without any secret.

## Records outside Discord

SQLite holds the queue, events, answers, commands, and attempts. Private per-application
directories hold the observation, package, resume, receipts, screenshots, the company
research cache, and Qwen input and output. The vault gets one readable note per application under
`Rove/Applications/` (status, links, job fit, filled values with sources, open
questions with drafts, timeline), rewritten on every change; it mirrors local state and is
never a candidate fact. Company research goes to one note per employer site under
`Rove/Research/`, marked untrusted. Credentials live only in the encrypted local
store.

## Local configuration

Private `config/workflow.json` maps `guild_id`, `forum_channel_id`, `control_channel_id`,
`action_channel_id`, `source_channel_id`, `shortlist_channel_id`, `system_channel_id`,
and lifecycle `tags`. `system_channel_id` and `recruiting_channel_id` are optional: when
one is missing, the worker (or the mail service) looks the `system-log` or `recruiting`
channel up by name once and writes the id into the file; without such a channel those
lines stay off. It also supplies `hermes_python`, an explicit `enabled` flag, `submission_enabled`,
`submit_adapters`, `max_waiting_applications` (default 1), `browser_app` (`chrome` or
`chromium`), `max_open_tabs` (default 5), `human_pacing` (default true), and an optional
`unslop_path` pointing at a local clone of the Unslop repository. Private
`config/feed.json` holds the feed's `enabled` flag, the jobs channel's `channel_id`,
`batch_size` (cards per feed tick, default 10), and `max_pending` (pending
announcements kept, default 40). Private `config/mail.json` holds the mail service's
`enabled` flag and `lookback_days` (default 3, used by the first tick only); the four Zoho
values live in the private env file, never in JSON.

```sh
uv run rove install-services
uv run rove feed seed
uv run rove workflow status
uv run rove workflow enqueue --url https://jobs.example.com/internship
uv run rove workflow tick
uv run rove workflow resume --id APPLICATION_ID
uv run rove browser status
uv run rove mail status
uv run rove mail tick
```

`workflow resume` and `workflow defer` are local owner operations equivalent to the
Discord commands. Delivery records are written before Discord mutations. Thread entries and the
action-needed and shortlist cards are stored first and posted after; a failed post is
logged to `logs/delivery-failures.log` under the state root and retried on every worker
tick. A failed card withdrawal or status-card edit is logged there too, but not retried.
Each tick also re-checks queued feed jobs against the approved exclusion rules and
defers the ones that now match, with the reason in the queue record; a link you pasted
is never pruned that way. The worker takes an application you told to go on (`go` or
`create account`) first, then a pasted link before a feed job, and
otherwise the newest queued job. An ambiguous forum creation is held for
reconciliation. The worker holds the only processing lock, so a `PREPARING` application
older than fifteen minutes is a crashed run and is handed back to the owner. When the
local model server is down the worker starts it once and otherwise leaves the queue
waiting instead of failing applications. After updating browser code, restart the
browser service (`launchctl kickstart -k gui/$UID/dev.rove.browser`); the
Chrome window and its tabs survive the restart.

## Hermes tools

The production include list contains thirteen narrow tools: the nine onboarding,
discovery and evidence tools from [Onboarding and jobs](onboarding-and-jobs.md) plus
`start_job_application`, `application_workflow_status`, `inspect_application_browser`,
and `refresh_job_feed`. None of them fills, approves, or submits.

## Limits

Three submission adapters: `greenhouse_v1`, with a request-level contract for public
Greenhouse boards; `lever_v1`, which needs Lever's thanks page and its heading and whose
handling of a CAPTCHA-rejected send is untested on the live site; and `generic_v1`, which
reads only the page and confirms nothing without new confirmation wording. Lever's own
inline field messages use a class the shared error read does not cover, so a Lever form
kept open by a field error without the verification sentence is "unclear", not "not
sent", until a live run shows what Lever renders. Multi-page support advances only on
Next/Continue controls after a complete page. Account creation covers email, password,
terms checkbox and known name fields; anything else on a registration page is a hold.
No CAPTCHA solving (a visible challenge stops and asks), no proxies. Recruiting mail covers
the Inbox of one Zoho account and the five labels above; mail about a job that was never
applied to through Rove is ignored, and `Accepted` and `Withdrawn` are not set by
code yet. Unattended submission is an owner policy with a daily cap and a minimum gap.
Only the word, number, and explicit replies above are understood; a sentence in a thread
is ignored, never interpreted.
