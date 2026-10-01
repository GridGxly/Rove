# Application workflow

This page follows one application from intake to the mail that arrives after it is sent. Cards and replies are covered in [Discord](discord.md), form mechanics in [Browser automation](browser-automation.md), files and tables in [Memory and storage](memory-and-storage.md), and configuration keys in [Requirements](requirements.md#local-configuration).

## Where jobs come from

Two sources feed one queue in SQLite.

The feed service, `rove feed tick`, runs every 15 minutes. It checks the [Keryx](https://github.com/GodlyDonuts/keryx) repository for a new commit and downloads the job snapshot only when the commit changed. An open internship is a match when its title contains one of the approved title keywords and neither the title nor the company trips an approved exclusion. Each match is queued and announced once in the jobs channel as a card with the title, company, location, cycle and track. Keryx rewrites metadata for known jobs often, and those changes do not post a job again.

A tick posts the newest pending matches first, at most `batch_size` of them (default 10). Before posting, pending announcements beyond the newest `max_pending` (default 40) are expired without being posted, so a long gap between ticks does not flood the channel. When Keryx marks a posting closed, its queued application is parked. An application that is already waiting on the owner gets a line in its thread instead.

The owner can also hand Rove a link. The Hermes tool `start_job_application(url)` and the command `rove workflow enqueue --url` both accept a public HTTPS link, whether or not Keryx lists it. Tracking parameters (`utm_*`, `gh_src`, `lever-source`, `ref`, `refid`, `src`, `source`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`) are stripped, and the queue is unique on the resulting URL, so the same posting reached through two links is one application. The table `job_link_aliases` maps a source link to the employer link it was verified to lead to. Code reads that table and nothing fills it automatically.

Queueing records the hash of the approved profile. If the approved profile changes before or during preparation, the run stops instead of mixing versions.

## Queue order

The worker, `rove workflow tick`, runs every 30 seconds under a file lock and handles one application at a time. Each tick it:

1. reads the owner's new replies in Discord
2. retries Discord posts that failed earlier
3. parks queued feed jobs that now match an approved exclusion, with the reason in the queue record
4. hands back a preparation that has been running for more than fifteen minutes, as a crashed run
5. runs approved submissions, and ends the tick if any ran

Nothing new starts while an application is being prepared, is being submitted, or has an unclear submission. An unclear submission holds the whole queue until the owner settles it.

Otherwise the worker picks, in this order:

1. an application the owner told to continue (`go` or `create account`)
2. a pasted link, oldest first, however many applications are waiting and whatever the daily cap says
3. the newest queued feed job, unless `max_waiting_applications` (default 1) applications are already waiting on the owner, or unattended sending has reached its cap or gap

## Job-fit review

The review runs when the worker reaches the application form, before any applicant data is typed. Qwen reads the posting text captured on the pages before the form and returns the posting's hard requirements as structured items. The form's own labels are passed separately as questions and never count as requirements.

Qwen judges what code cannot: the program type, required skills, location rules and other explicit conditions. Code makes the exact comparisons and can overrule Qwen:

- A graduation window is compared inclusively with the approved graduation month. A window month counts only when the posting or the quoted requirement states it.
- A class standing such as "rising senior" is decided from the approved graduation month and the internship year.
- Work authorization and sponsorship requirements are compared with the approved facts.
- A requirement for computer science or a related field is satisfied when the approved major is a computing degree.
- A location rule is satisfied when the approved preferences say relocate and include onsite work.
- A requirement Qwen files as a graduation window that never mentions graduating, such as an internship term, is reclassified as dates.

Code computes the decision from the requirement statuses. Only a conflict on an eligibility requirement (program, graduation window, work authorization, sponsorship, location, degree) changes it. A conflict that code verified is `not_fit`. A conflict that only Qwen claims is `needs_review`. Everything else is `fit`. Skills, dates and other wishes never change the decision.

Eligibility the posting states but code cannot check against the profile does not hold the job. The job-fit card in the thread lists it, and the ready-to-submit card repeats it under Why.

A feed job that is not `fit` waits with one card in the thread and one in the shortlist channel. The card names the conflicting requirements, and says so plainly when Qwen matched the role to one of the owner's excluded kinds. The replies are `go` and `park it`. A link the owner pasted is never held on fit.

Qwen's raw extraction is saved with the review. When nothing the review reads has changed (the posting text, the profile, the evidence excerpts and the prompt version), a later run re-evaluates the saved extraction with the current code rules and makes no model call.

### How Qwen is called

Every Qwen call from the worker goes through Hermes as one request with no tools, streaming off, thinking off, temperature zero, and a 2,048-token output ceiling. The input is trimmed to about 40,000 characters, longest text first.

For the job-fit review and for drafting, a run that does not finish is retried once, and an answer that is not valid JSON for the expected schema is retried once with the defect named. A second failure is recorded as a failed run and stops preparation with a card. It is never parsed as a draft.

When the local model server is down, the worker starts it once and waits up to a minute. If it stays down, the application goes back to the queue without a card and is tried again later.

## Resume

The resume is prepared once per application. Rove calls Erga's `intake_job_url` and passes the posting text the job-fit review read, so Erga can tailor from it even when the careers site refuses Erga's own fetch.

- If Erga returns a PDF inside its configured output folder, Rove validates it again through Erga's `validate_tailored_resume`. A PDF that passes is used. One that fails, or has no source file beside it, stops the run with a "Resume needs review" card.
- If Erga's intake fails, or Erga returns no PDF because the tailored draft failed its layout check, the approved base PDF from the profile is used and the thread carries the warning.
- If there is no approved base PDF either, the run stops with the same card.

The chosen file is copied into the application's private folder and its SHA-256 is recorded. Only that file is uploaded, and its hash is checked again before sending. The thread gets a line saying which resume it is and the PDF itself. When Erga produced a tailored draft that failed validation, Rove renders that draft with Tectonic and posts it too, marked as not sent, with the reason.

## Filling the form

Applicant data is typed only when the form's host is on the code's list of applicant-tracking hosts and the page belongs to the same job as the queued link. Otherwise the run stops with a card. [Browser automation](browser-automation.md#where-applicant-data-may-be-typed) has the list and the rule.

Each field is resolved in this order:

1. an answer the owner gave for this application, including a draft the owner approved
2. an approved profile fact, matched by the field's label
3. an answer the owner gave to the same question on an earlier form, or, for a voluntary self-identification question (gender, race or ethnicity, veteran or disability status), the form's own decline option when exactly one option reads as a decline
4. a Qwen draft, or a question for the owner

Code chooses an option only when the value matches exactly one option, and it reads every filled value back. A value the site changed stops the run with a card naming the field. A complete page that shows Next or Continue is advanced and the next page is filled, for at most four pages.

An optional field with no fact and no draft is left blank and listed in one line in the thread. Only required questions reach the owner.

### Remembered answers

An answer the owner types in Discord, such as `3: 5 months`, is stored in the `answer_memory` table under a fingerprint of the question's wording (without "(Optional)", "(Required)" and asterisks) and, when the question has options, its options. Any later form that asks the same question is filled from it, with the source shown as "your earlier answer". When the new form offers options, the remembered value is used only if it is one of them. `skip` is never remembered, and an approved Qwen draft is not remembered either.

The owner can list, change, remove and add remembered answers in the `memory` channel, described in [Discord](discord.md#memory-channel). A readable copy is kept in `Answers.md` in the vault. See [Memory and storage](memory-and-storage.md#remembered-answers).

### Drafts from Qwen

The questions still open after the steps above go to Qwen in one call. Qwen sees the frozen profile, approved stories, up to three approved evidence excerpts from Erga, the answers already given for this application, and the text of the form page. For each question it returns either a draft with its sources or a note that only the owner can answer.

Code checks the result before anything is shown:

- Every question is answered exactly once and no unknown question appears.
- A draft has a value and at least one source.
- A draft for a question with options must be one of the options. If it is not, the question goes to the owner with the options listed.
- A draft must fit its field. Qwen is told each field's character limit. An answer over the limit or over 150 words gets one retry, and what still overflows is cut at a sentence boundary and marked as shortened.

Each draft is posted as its own card with the reply that approves it. A question for the owner is posted as one line.

### Draft cleanup

Written drafts follow two public rule sets, [Unslop](https://github.com/theclaymethod/unslop) and [Humanizer](https://github.com/blader/humanizer). The drafting prompt carries a short digest of both.

Every draft longer than 60 characters is scanned. The built-in scan looks for Unslop's hard tells and for a digest of Humanizer's patterns: jargon, "not X but Y", run-ups, hedge stacks, inflated words, borrowed authority, chatbot leftovers, connector dashes, lists of three, repeated sentence openers, questions and curly quotes. When `unslop_path` points at a local clone of Unslop, its phrase and structure scanners run in place of the built-in Unslop list. The Humanizer digest runs either way.

One hard tell, or two findings of any kind, triggers one repair call to Qwen. Structure scores alone never trigger it. The repair is kept only if every number and capitalized name survives, no new number appears, and the text did not grow by more than about 15 percent. Otherwise the original stands. The card shows the final text. The scan summary is saved with the draft and appears in the application's vault note.

### Voice note

If the vault has `Rove/Story/Voice.md`, about 2,500 characters of it go to Qwen as a style sample. The draft follows its sentence rhythm and plain words and does not copy its sentences. The note is never a source of facts, Rove never writes it, and a change to it causes the drafts to be made again on the next preparation.

### Company research

Before Qwen drafts, code gathers company research once per application. It reads at most three public pages from the employer's own site: the home page, the about or company page it links to, and the careers or culture page (`/about` and `/careers` when the home page links to neither).

The employer site is the posting's host when the posting is on the employer's own site, with a leading label such as `careers.`, `jobs.` or `www.` dropped. Otherwise it is the employer host the posting text links to most often. A posting on an applicant-tracking host or a job board that names no employer link gets no research, and the draft can say only what the posting says about the company.

The reads are plain HTTPS with a desktop browser agent string, a ten-second timeout, 400 KB per page and eight requests in total. Only the employer's site is read, and a redirect off the site is not followed. No cookies are kept, and nothing about the applicant is in the request. Scripts, navigation, footers, forms and hidden elements are skipped.

The pages are reduced to the sentences that say what the company does, how big or old it is, what it sells and what it values, about 1,800 characters in total. Any line that reads as an instruction to a model is dropped first, which also drops a few true sentences. Qwen is told to use the text only to say true things about the company, never to claim the applicant did anything with the company, and never as instructions.

The result is cached in the application's folder and copied to a note in the vault marked untrusted. A site that does not answer is tried once more on a later preparation. Research never stops a run.

The thread gets one line with the outcome, for example "Looked up the company before drafting · read 3 pages on acme.example (home, about us, careers)" or "Could not reach the company site · drafting from the posting only". The line names the site and the pages. It never carries links or page text, and a preparation that reuses the cached research adds no second line.

## When Rove stops for the owner

Every stop is one card in the thread and one in an owner channel, with the replies it accepts. When a browser screenshot exists from the last half hour, it is attached to the thread.

| Card | Cause | Replies |
| --- | --- | --- |
| Your call on fit | A feed job conflicts with an approved fact. Posted to the shortlist channel. | `go`, `park it` |
| Answers needed | Required questions have no answer, or drafts wait for approval. | `N: answer`, `use draft N`, then `go` |
| Account needed | The board wants an account and the owner has not allowed one for this application. | `create account`, `park it` |
| Verify the account email | The account was created and the site wants the email verified. | `go` after opening the link |
| Sign-in needs you | A sign-in page with no stored account, or the stored sign-in failed. | `go` after signing in |
| Manual step in the browser | The page has a field labeled social security, passport, bank account or verification code. | `go` after finishing it |
| CAPTCHA needs you | A CAPTCHA challenge is visible. | `go` after solving it |
| Blocked by the employer's site | The site showed a block page twice. | `applied`, `park it` |
| The site says you already applied | The page says an application already exists. | `applied`, `park it` |
| Resume needs review | Erga's PDF failed Rove's validation, or there is no approved base PDF to fall back on. | `go`, `park it` |
| Apply control not found | No Apply control on the page. | `go` after reaching the form |
| Navigation stopped | Six page steps passed without reaching a form. | `go`, `park it` |
| Final step not reached | The last step with its Submit control was never reached, or the site rejected a value on a step. | `go` |
| Browser needs a look | Anything else, including a form on a host or job that does not match the queued link. | `go`, `park it` |
| Preparation stopped | A step raised an error. The card names the step. | `go`, `park it` |
| Preparation interrupted | The worker found a preparation older than fifteen minutes. | `go`, `park it` |
| Ready to submit | The form is complete and an adapter is enabled. Not posted when `auto_submit` is on. | `send it`, `go` |
| Ready · send it yourself | The form is complete, and either submission is off or no enabled adapter matches the site. | `applied`, `park it` |
| Submission not attempted | The check before the click failed. Nothing was sent. | `go` |
| The site rejected the form | After the click the form stayed open with a validation error. Nothing was sent. | `go`, `park it` |
| The site's CAPTCHA rejected the send | Lever's CAPTCHA refused the send. | `applied` after sending by hand, `park it` |
| Submission unclear | One click happened and no confirmation was read. | `applied`, `not sent` |

A posting whose page says it no longer accepts applications is parked with a line in the thread and no card.

Rove never solves a CAPTCHA, never completes MFA, and never types a password it did not generate itself. It uses no proxies.

### Accounts

When a board needs an account, the card offers `create account`. After that reply the browser daemon fills the email from the profile and a generated password, ticks the terms checkbox, fills text fields it can resolve from the profile such as the name, clicks the create control, and stores the credential encrypted on the Mac. Later sign-in pages on that host are completed with the stored account. Email verification, CAPTCHA, MFA and identity checks stay with the owner in the recruiting browser.

## Sending

Three things must be true before a Submit click:

- private `config/workflow.json` sets `submission_enabled`
- an adapter listed in `submit_adapters` matches the page (the first listed match wins)
- an approval exists for the exact package: the owner's `send it` reply in the thread, an explicit `submit` command, or the `auto_submit` policy

The model has no submit tool. Only the browser daemon clicks, when the worker relays an approval.

### The check before the click

The daemon observes the live page again and refuses to click unless all of this holds:

- the package on disk still hashes to the approved hash, and the application is ready for review
- no question is open and the page has exactly one final control, with the same label as reviewed
- the frozen profile is unchanged and is still the approved version
- no CAPTCHA is visible, the URL is the reviewed one, and the page is not a sign-in or identity step
- every field has the same identity, state and value as in the reviewed package
- the frozen resume file has the recorded hash and was uploaded
- no required field is empty and every required dropdown has a committed selection

If the check fails, nothing is sent and the owner gets a "Submission not attempted" card with the reason.

When it passes, the daemon records the attempt in SQLite and marks the application as submitting in one transaction. A second attempt is refused unless the first was recorded as not submitted. It then arms the page's submit guard for one click, clicks once, waits up to 45 seconds for the adapter's signal, and reads the page once.

### Outcomes

Applied: the adapter saw its full confirmation contract. The receipt keeps the confirmation URL and text, screenshots before and after, the status codes of matching POST responses without headers or bodies, the adapter name and the package hash. Erga is told through `confirm_application_submission` when the resume manifest links an Erga application. The tag becomes `Applied` and the tab closes.

Not submitted: the form stayed open and named a new validation error. The application goes back to the owner with the site's message, and a later `go` prepares a new package. When the message names a required field, that label is remembered for the application and treated as required on the next preparation. Only `lever_v1` and `generic_v1` can report this outcome.

Unclear: anything else, including an error after the click. The card says not to click again. The owner checks the browser and their email and replies `applied` or `not sent`. `applied` records the application as sent on the owner's word. `not sent` releases the attempt. With `auto_submit` on the application is then prepared again without another reply. With it off the owner gets a card and replies `go`. Code never infers the outcome and never retries the click. A recruiting mail about the application also settles it, as described under [Recruiting mail](#recruiting-mail).

For any outcome other than applied, the thread gets the screenshot of the form just before the click and the page after it.

### Adapters

`greenhouse_v1` covers public Greenhouse boards at `job-boards.greenhouse.io/{board}/jobs/{id}`. The board's client posts to `boards.greenhouse.io/{board}/jobs/{id}` and, on success, navigates to `/{board}/jobs/{id}/confirmation`, which renders `.confirmation__content`. The attempt is applied only when all of these hold: a 2xx POST to that path, no rejected POST, the confirmation URL, the confirmation block, and no form left. Anything else is unclear. If the board answers the click with a challenge or an emailed security code, no confirmation appears and the attempt is unclear.

`lever_v1` covers public Lever postings at `jobs.lever.co/{company}/{posting}/apply`. The Submit button runs hCaptcha, and the page posts the form itself when the CAPTCHA hands it a token. The attempt is applied only when the page landed on `/{company}/{posting}/thanks` for the same posting, shows the "Application submitted!" heading, and has no form left. The status of the form's POST is kept in the receipt as evidence and confirms nothing on its own. When the page comes back as the form under "There was an error verifying your application", the CAPTCHA rejected the send: the attempt is recorded as not submitted, the tab stays open, and the owner sends it by hand and replies `applied`. A form that stays open with a new validation message is not submitted either.

`generic_v1` matches any HTTPS page and has no request to watch. List it last in `submit_adapters` so the stricter adapters win on their own sites. It clicks the one final control and waits until the page leaves, the form disappears, or a success or validation message appears that was not there before the click. It then compares the page with the observation taken before the click. The attempt is applied only when all three hold:

- at least one new confirmation signal: the URL path or query newly matches the `url` pattern, the page text newly matches the `sentence` pattern, or a visible `[role=alert]`, `[role=status]`, `.confirmation` or `.success` element newly matches the `sentence` pattern
- the form left: no fields and no final control, or a different URL
- no new validation message: visible text in `[role=alert]`, `.error`, or the message beside an `[aria-invalid=true]` field that matches the `error` pattern and differs from what was there before the click

The patterns, all case-insensitive:

```text
url       confirmation|thank|success|submitted|complete|received
sentence  thank you for (applying|your (application|interest))
          |application (has been |was )?(submitted|received|complete)
          |we('ve| have) received your application
          |successfully (submitted|applied)
error     required|invalid|error|could not|try again
```

Wording and URL tokens already present before the click never count. A careers page that opens with "thank you for your interest" cannot confirm itself, and a thank-you sentence under a form that is still open is not a confirmation. A new validation message on a form that stayed on the same URL is the not-submitted outcome. Anything else is unclear.

## Unattended sending

Unattended sending is an owner policy in private `config/workflow.json`. It is off unless set.

`auto_use_drafts` makes Qwen's drafts the answers without a `use draft` reply. The draft cards stay in the thread, each used draft gets a line naming its question number, and a `N: your text` reply before sending replaces it. When the key is absent it takes the value of `auto_submit`.

`auto_submit` sends a complete package on the tick that prepared it. The thread shows "Auto-submit is on · sending it once" and then the result. No card is posted to the action-needed channel for a package that completes.

Two keys pace it. `max_submissions_per_day` (default 10) counts the submission attempts recorded on the current UTC date. `min_minutes_between_submissions` (default 8) is the gap since the last attempt. While either limit applies, the worker starts no new feed job. An application the owner resumed and a link the owner pasted are worked anyway.

Everything in [When Rove stops for the owner](#when-rove-stops-for-the-owner) still stops an unattended run, except the ready-to-submit card.

## Recruiting mail

Mail tracking is optional. `rove mail tick` runs every 15 minutes and does nothing until private `config/mail.json` sets `enabled` and the private env file holds the four Zoho values. [Requirements](requirements.md#zoho-mail) has the setup.

### What is read

Each tick refreshes a Zoho access token, finds the Inbox folder and lists messages newer than the checkpoint. While at least one sent application exists, it reads each new message's body as plain text. The first tick looks back `lookback_days` (default 3). A tick reads at most 500 message headers. The integration only reads. It never sends, moves or deletes mail.

A message is handled once, and the checkpoint advances past each handled message.

### Matching a mail to an application

Only applications that were sent, or whose submission is unclear, can match.

- Strong: the sender's domain is the employer's domain from the posting URL, or the sender is a known recruiting host (Greenhouse, Lever, Ashby, Workday, HackerRank and similar) and the company name appears in the subject or body.
- Weak: the company name appears in the subject, from any other sender. A weak match counts only when the rules below recognise the mail.
- Anything else is ignored. SQLite keeps one row with its id and sender domain so it is not read again.

When two applications match, the role's own words in the mail decide, and a tie goes to the most recently updated one.

### Classification

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

A rejection outranks everything it mentions, and an offer outranks the interviews before it. When a mail matches both interview and assessment wording, the subject line decides. If the subject names neither, the mail is ambiguous.

Only an ambiguous mail from a strong match goes to Qwen. Qwen reads a sanitized excerpt of at most 2,500 characters with links, addresses, markup and any sentence that talks to a model or mentions secrets removed. It may pick one of the six labels (the five above and `other`) and nothing else. A deadline Qwen quotes counts only when the excerpt contains it. An answer that is not a label files the mail as `other`. When the local model is down, the tick stops at that mail and resumes there next time.

### What a classified mail does

- The thread gets a card with the label, the sender's domain, the clipped subject and the deadline as the mail states it. A regex quotes phrases such as "by October 9, 2026 at 11:59 PM PT" or "within 72 hours". Nothing is computed, and the body is never posted.
- The application moves when the label is a step forward: Applied to OA to Interview to Offer, and Rejected from any of them. A label behind the current state is recorded without a move, and nothing moves a rejected application.
- The recruiting channel gets one line with a link to the thread.
- Erga's `update_application_status` is called with `oa`, `interview`, `offer` or `rejected` when the resume manifest links an Erga application. A failure there changes nothing locally.
- A mail with any label except `other` settles an unclear submission. The attempt is recorded as applied with the mail as the receipt's evidence, and an assessment or interview then moves it on.

The states mail can set are OA, Interview, Offer and Rejected. Nothing in a mail can queue, prepare, submit or re-prepare an application, and a sent application is never moved back into preparation.

`rove mail status` shows the switches, the checkpoint and the message counts without any secret.

## Commands

```sh
uv run rove workflow status
uv run rove workflow enqueue --url https://jobs.example.com/internship
uv run rove workflow tick
uv run rove workflow resume --id APPLICATION_ID
uv run rove workflow defer --id APPLICATION_ID
uv run rove feed seed
uv run rove feed tick
uv run rove browser status
uv run rove mail status
uv run rove mail tick
```

`workflow resume` and `workflow defer` are local owner operations equal to the `go` and `park it` replies. `feed seed` queues up to 25 current matches for announcement so the first run has something to post. The services that run the ticks on a schedule are listed in [Requirements](requirements.md#services).

The Hermes agent gets four tools for this workflow, `start_job_application`, `application_workflow_status`, `inspect_application_browser` and `refresh_job_feed`. None of them fills, approves or submits. The full tool list is in [Onboarding and jobs](onboarding-and-jobs.md#hermes-connection).

## Limits

- Forms are filled only on the hosts in the code's applicant-tracking list, and only for the same job as the queued link. `generic_v1` therefore reaches only those hosts.
- `lever_v1`'s handling of a CAPTCHA-rejected send follows Lever's reported wording and has not been observed in a live run. Lever's inline field messages use a class the shared error read does not cover, so a Lever form kept open by a field error without the verification sentence is recorded as unclear.
- Multi-page support advances only on Next, Continue, "Save and continue" and "Next step" controls, after a complete page, for at most four pages.
- Account creation covers email, password, a terms checkbox and text fields the profile resolves. Anything else on a registration page is a stop.
- Code does not yet stop a Qwen draft on a legal or sensitive question from becoming the answer under `auto_use_drafts`. The prompt tells Qwen to leave unknown personal facts to the owner, and a code gate is in progress.
- Mail tracking reads the Inbox of one Zoho account. Mail about a job that was not applied to through Rove is ignored.
- The `Accepted` and `Withdrawn` states are not set by code. There are no reminders or calendar entries.
- Only the replies listed in [Discord](discord.md#replies) are understood. A sentence is not interpreted.
- One application is worked at a time.

## In progress

These are being built now and are not described above. Each will be documented here when it lands.

- Question handling and a gate for sensitive answers
- Timing measurements and `rove bench`
- Adapters for Paylocity, Workable, JazzHR and BambooHR
- Scored intake with a daily digest
- Per-platform pacing
