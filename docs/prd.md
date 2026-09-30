# Erga Autopilot product requirements

Draft, 2026-09-30. A living document: when it disagrees with the code and tests, the code wins and this file is corrected.

## Purpose and problem

Applying to internships by hand means opening the same forms hundreds of times, retyping approved facts, writing short answers under time pressure, and losing track of what went where. Erga Autopilot does that work on the owner's Mac: it takes in a job, checks the posting against approved facts, prepares an evidence-backed resume, fills the form, drafts written answers with a local model, sends the application, and keeps an exact record. The owner reads the record on a phone afterwards and is interrupted only for facts nobody else knows.

## Who it is for

One owner: a student applying to internships, running the stack on his own Mac and reading Discord on his phone. He wants applications sent while he is in class and a record he can trust months later. Secondarily, a developer who forks the repo and gets synthetic data with the same boundaries.

## Goals

1. Applications go out without a per-application reply; `submit` approval is a policy the owner can turn on, not the normal path.
2. The owner is asked only for facts only he knows and for steps a site forces into the browser.
3. Every application has one readable record: what was sent, from where, and what happened next.
4. Nothing is sent twice; nothing unclear is retried.

## Non-goals

Applying to everything. A hosted service, a second user, cloud models, CAPTCHA solving, or automating identity documents, bank details and MFA.

## The scenario, on a phone

Morning. `#internship-jobs` shows six new feed cards, each already queued. He does nothing.

The worker takes the newest, opens it in the background recruiting Chrome, and asks Qwen to list the posting's hard requirements; code checks them against approved facts. It fits. Erga tailors the resume, or the approved base PDF is used when the posting is bot-protected. The form is filled from the frozen profile and verified; two written questions are drafted and cleaned. With `auto_submit` on, the package is sent once and the confirmation is checked. The forum post's first message now reads **Applied ✅**; below it, one line per step, a form card with every value and its source, the drafts, and the receipt.

`#shortlist` holds one card: "Your call on fit · Conflicts with your approved facts: must graduate by December 2026." He replies `defer 3f9c2a7b1e04`. The card disappears.

`#action-needed` holds one card: "Answers needed · 1 question only you can answer: Which office do you prefer? (Austin / Remote)". He replies `answer 3f9c2a7b1e04 8b1d0e4f2a6c = Austin`; the card disappears and the application is sent on the next tick.

He pastes a link in `#agent-control`. It goes to the front of the queue and is never held on fit.

Evening. A thread's first post says "Submission unclear · do not click again". He finds the employer's acknowledgement in his inbox and replies `reconcile 9a71c04e2b8d applied`.

## Functional requirements

**Done**: code and a test prove it. **Partial**: code exists; the gap is named. **Not started**: nothing in code. Proof names a test in `tests/` or a function in `src/erga_autopilot/`.

### Job intake

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-1|Each feed tick posts every new matching internship once as one card, newest first, at most `batch_size` per tick, expiring backlog beyond `max_pending`.|Done|`test_feed_announces_each_job_once_and_supersedes_stale_duplicates`, `test_feed_tick_expires_the_backlog_beyond_max_pending`|
|FR-2|A pasted public HTTPS link is queued ahead of feed jobs and deduplicated by canonical URL with tracking parameters stripped.|Done|`test_tracking_parameters_do_not_create_duplicate_applications`|
|FR-3|A posting that closes in the feed parks its queued application, and a queued feed job matching an exclusion rule is parked with the reason; pasted links are never parked this way.|Done|`test_closed_keryx_posting_parks_the_queued_application`, `test_queued_feed_jobs_are_deferred_when_approved_rules_exclude_them`|

### Fit decision

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-4|Code decides fit from Qwen's extraction: a code-verified conflict on program, graduation window, work authorization, sponsorship, location or degree is `not_fit`; a conflict only Qwen claims is `needs_review`; skills, dates and wishes never change it.|Done|`reasoning.decide`; `test_only_conflicts_on_eligibility_requirements_change_the_decision`|
|FR-5|Eligibility the posting states but code cannot check is listed on the job-fit and ready cards, never asked.|Done|`test_evaluate_review_lists_unverified_eligibility_instead_of_holding`|
|FR-6|A non-fit feed job gets one card in the thread and one in `#shortlist` with `proceed`/`defer`; a pasted link is never held on fit.|Done|`test_owner_links_skip_the_fit_hold_that_sends_feed_jobs_to_the_shortlist`|
|FR-7|Reviews are cached on posting text, profile and prompt version, and old extractions are re-judged by current rules without a model call.|Done|`test_cached_review_is_re_evaluated_by_current_code_rules`|

### Resume

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-8|Erga intake yields a validated tailored PDF; when intake or validation fails (including a bot-protected posting), the approved base PDF is used with a warning, and the exact bytes and SHA-256 are frozen.|Done|`test_failed_erga_intake_keeps_the_approved_base_resume_with_a_warning`|
|FR-9|The browser uploads only the frozen resume and verifies the upload even when the widget removes the input.|Done|`test_uploaded_file_can_be_verified_after_widget_removes_input`|

### Form preparation

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-10|The worker follows only observed Apply controls, retries a block page once through the site's front door, and hands a second block to the owner.|Done|`worker.process`; `test_block_pages_are_recognized_and_never_treated_as_forms`|
|FR-11|Before reading a page the runtime waits for fields, declines cookie banners (never Accept) and waits out loading indicators, each bounded to ten seconds.|Done|`test_late_rendered_forms_wait_out_the_spinner_and_decline_cookies`|
|FR-12|Known fields, radio groups, Yes/No buttons and country selects are resolved from the frozen snapshot, filled as one batch and verified; an option is chosen only on exactly one match; a changed value stops with a card naming the field.|Done|`live_browser._verify_batch`; `test_unassociated_labels_radio_groups_and_button_choices_resolve_from_approved_facts`|
|FR-13|A complete page with a Next/Continue control is advanced and filled again, at most four steps, until a final control appears.|Done|`test_multi_page_forms_are_filled_step_by_step_until_the_final_control`|
|FR-14|An optional field with no fact and no draft is left blank and noted in one line; only required questions reach the owner.|Partial|`worker.skip_optional`; untested|
|FR-15|An account wall offers `account ID create`; on approval the daemon fills email, a generated password and the terms box, stores the credential encrypted and completes later sign-ins; email verification stays with the owner.|Done|`test_account_creation_and_sign_in_use_the_encrypted_store_and_never_leak`|
|FR-16|A page with password fields or SSN, passport, bank or verification-code labels forces manual takeover, with values kept out of screenshots and model context.|Done|`live_browser.observe`; `test_missing_sensitive_fact_never_guessed`|
|FR-17|A CAPTCHA or MFA challenge is recognised and handed to the owner in the recruiting browser.|Partial|verification-code pages and post-submit challenges (as `UNKNOWN_SUBMISSION`) are handled; `live_browser.py` has no CAPTCHA detection, so a pre-submit challenge reads as "Apply control not found"|
|FR-18|`generic_v1` prepares and confirms applications on employer sites without an ATS contract.|Partial|`submission.GenericV1` and `test_generic_adapter_confirms_a_plain_thank_you_page` are in the working tree, uncommitted; no real-site confirmation yet|

### Written answers

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-19|Unfamiliar required questions go to Qwen through Hermes as one bounded call with one retry; a harness stop or malformed output becomes a failure card, never a draft.|Done|`test_harness_stop_is_not_a_model_answer`, `test_generate_retries_once_then_reports_the_harness_reason`|
|FR-20|Drafts use the approved profile, stories, career evidence, prior owner answers and posting text trimmed to the model window; an option not on the form becomes an owner question; keys cannot be omitted or invented.|Done|`test_proposed_value_outside_the_options_becomes_a_question`, `test_qwen_review_cannot_omit_or_invent_question_keys`|
|FR-21|Written drafts pass the Unslop scan with one bounded repair that keeps every number and name; the card shows the cleaned text and a summary.|Done|`test_drafts_get_one_bounded_cleanup_and_are_hashed_after_it`|
|FR-22|With `auto_use_drafts` (or `auto_submit`) drafts become answers without a `use` reply; draft cards stay and a later `answer` overrides.|Done|`test_auto_policy_uses_qwen_drafts_and_queues_exactly_one_submission`|
|FR-23|A required question with no fact and no draft stops with one "Answers needed" card: up to six questions with options and copy-ready `answer` lines.|Done|`test_hold_fields_render_reasons_questions_and_commands_for_the_owner`|
|FR-24|Substantive answers are preceded by company research in a restricted context whose notes land in the vault.|Not started|no research code|

### Submission

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-25|With `auto_submit` a complete package is sent once on the tick that prepared it, with no owner card; `max_submissions_per_day` and `min_minutes_between_submissions` pace feed jobs; resumed or pasted applications are never capped.|Partial|`worker.queue_auto_submit`, `worker.next_queued`; cap and gap are untested, and `next_queued` applies both to pasted links, against the docs|
|FR-26|Otherwise submission runs only from an owner `submit ID HASH` whose 8–64-hex prefix matches the current package of a `READY_FOR_REVIEW` application.|Done|`test_claim_needs_exact_owner_approval_and_never_repeats`|
|FR-27|Before the click, code re-observes and requires the same URL and job scope, form state and final control, the current approved profile version, the frozen resume uploaded, and every required answer.|Done|`test_preflight_accepts_only_the_reviewed_unchanged_form`|
|FR-28|The attempt is recorded in SQLite before one click; the page guard blocks native submits until armed for that click; a package is never sent twice.|Done|`test_guard_blocks_native_submit_until_one_approved_attempt_is_armed`|
|FR-29|Greenhouse is `APPLIED` only with a 2xx POST, no rejected POST, the confirmation URL and block, and no form left; anything else is `UNKNOWN_SUBMISSION` with `reconcile` commands and no retry.|Done|`test_greenhouse_confirmation_requires_every_signal`, `test_rejected_submission_stays_unknown_until_the_owner_reconciles`|
|FR-30|A confirmed submission is mirrored to Erga and the tag becomes Applied.|Partial|`submission.erga_confirm`; tests stub it|

### Record and archive

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-31|One forum post per application is created before preparation; an uncertain creation is held, not retried; entries and cards are stored before posting and retried each tick, so a Discord outage never fails a run.|Done|`test_ambiguous_forum_creation_is_not_retried`, `test_owner_cards_are_durable_and_retried_on_the_next_tick`|
|FR-32|The form card lists every filled field with value and source; the receipt keeps confirmation URL and text, before/after screenshots, response statuses without bodies, and the package hash.|Done|`workflow.event_embeds`, `submission._submit`|
|FR-33|A private per-application directory holds observation, package, resume, receipt, screenshots and Qwen input/output; a readable vault note mirrors them on every change.|Done|`test_application_note_is_written_to_the_vault`|

### Recruiting follow-up

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-34|Recruiting mail is classified into acknowledgement, OA, interview, offer and rejection events that add a timeline entry to the right thread.|Not started|no mail code; Zoho is docs only|
|FR-35|Tags OA, Interview, Offer, Rejected, Accepted and Withdrawn can be set by mail or owner command, with a timeline entry, deadline and reminder.|Not started|`workflow.STATE_TAGS` knows only Preparing, Applied, Needs Action|

### Memory and profile

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-36|Onboarding fills eight fixed sections; `propose` validates schema and draft hash; `approve` is a local owner operation that rejects conflicts and writes an immutable snapshot plus the canonical vault note.|Done|`test_proposals_cannot_approve_or_invent_schema_and_stale_approval_fails`, `test_conflicting_evidence_blocks_approval`|
|FR-37|Each application freezes the approved profile hash; a manual edit to the canonical note blocks use until reviewed; a profile change after preparation blocks submission.|Done|`test_approved_snapshot_stays_frozen_and_canonical_edits_block_use`|
|FR-38|QMD indexes only the approved profile copy and rejects stale or modified sources; Hermes receives thirteen narrow tools, none of which fills, approves or submits.|Done|`test_candidate_memory_blocks_unapproved_stale_and_modified_sources`, `test_real_mcp_reads_only_approved_sections_and_exposes_no_approval`|
|FR-39|An answer the owner gives once is remembered for equivalent questions on later applications, and `#memory` lets him review and correct approved facts from Discord.|Not started|`application_answers` is per application; no channel handler|

### Owner controls

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-40|Commands (`answer`, `use`, `resume`, `defer`, `proceed`, `account … create`, `submit`, `reconcile`) are accepted only from the configured numeric owner in the control, action-needed and matching forum channels, parsed by strict patterns with 8–64-hex hash prefixes.|Done|`test_owner_command_cannot_be_forged_by_bot_or_other_author`, `test_submit_and_use_commands_accept_hash_prefixes_of_eight_to_sixty_four_hex`|
|FR-41|The worker takes resumed applications first, then pasted links, then the newest feed job; holds the queue while one application waits; hands back a `PREPARING` run older than fifteen minutes; and waits, not fails, when the model is down.|Partial|`test_queue_holds_for_waiting_applications_unless_owner_resumes`, `test_tick_executes_one_approved_submission_and_recovers_crashed_runs`; model-outage path untested|
|FR-42|Every Discord command has a local CLI equivalent.|Done|`cli.py`|

## UX requirements

The bar is the owner's: a screen that answers "what is this, what do I do, what happens next" in one glance, on a phone.

|ID|Requirement|Status|Proof|
|---|---|---|---|
|UX-1|Each owner channel holds at most one live card per application; a new card replaces it, and it is withdrawn as soon as the application stops waiting.|Done|`test_a_new_notice_withdraws_the_previous_card_in_the_same_channel`, `test_leaving_a_waiting_state_withdraws_the_owner_cards`|
|UX-2|The thread's first post is a live status card (headline, one line, reply commands) edited in place so the forum list previews the state.|Done|`test_the_thread_status_card_mirrors_the_live_owner_card`|
|UX-3|Routine steps are one plain line; cards are used only for decisions, drafts, job fit, the filled form, results and failures.|Done|`workflow.event_embeds`|
|UX-4|Every hold card says what happened, why, the questions only the owner can answer, and the exact reply in a code block.|Done|`test_hold_fields_render_reasons_questions_and_commands_for_the_owner`|
|UX-5|No internal jargon reaches the owner: sources and states appear in words, never as keys.|Partial|`workflow.source_words`; `answer` lines and "You answered" entries show 12-hex field keys, and "Qwen", "Unslop" and "package" appear on cards|
|UX-6|Cards fit a phone: title, one line, values in fields, at most six commands, summaries in whole sentences.|Done|`test_brief_keeps_whole_leading_sentences_and_never_goes_empty`|
|UX-7|The recruiting browser never takes focus: background tabs, window behind the owner's work, open tabs capped.|Done|`live_browser.restore_front`; checked by hand, no automated test|
|UX-8|The owner can act on everything from the phone except steps a site forces into the browser.|Partial|checking the exact resume PDF or live form still needs the Mac; the thread shows values and hashes, not the PDF|

## Security and privacy requirements

|ID|Requirement|Status|Proof|
|---|---|---|---|
|SP-1|External content (page text, labels, tool output, QMD results, email) informs but never authorises: it cannot change facts, choose files, widen tools or trigger submission.|Done|`test_malicious_label_has_no_authority`, `test_dynamic_fields_and_external_requests_cannot_gain_authority`|
|SP-2|The model has no submit tool; only the browser daemon submits, after a durable claim.|Done|`submission.claim_attempt`; `test_real_mcp_reads_only_approved_sections_and_exposes_no_approval`|
|SP-3|Personal data is entered only on public HTTPS pages matching the verified employer and job scope; private networks and a shared ATS hostname are refused.|Done|`test_browser_dns_and_label_boundaries`, `test_same_ats_different_employer_or_job_is_not_same_scope`|
|SP-4|Uploads come only from the frozen package; an unapproved path fails before it is read.|Done|`test_unapproved_file_rejected_before_read`|
|SP-5|Employer credentials are Fernet-encrypted with a separate owner-only key and never appear in the vault, Discord, logs or model context.|Done|`credentials.py`; `test_account_creation_and_sign_in_use_the_encrypted_store_and_never_leak`|
|SP-6|SSNs, bank details, identity documents, verification codes and SMS/authenticator MFA are never automated or bound as answers.|Done|`worker.apply_command`; `test_missing_sensitive_fact_never_guessed`|
|SP-7|The recruiting browser is a daemon-launched Chrome with its own profile and a localhost-only DevTools port, separate from the everyday browser.|Done|`live_browser.launch`|
|SP-8|Receipts and logs keep response statuses only, never headers, bodies, tokens or applicant values; the repo holds synthetic fixtures only, with a staged-secret check.|Done|`submission._submit`; `scripts/check_staged.py`|
|SP-9|The adversarial suite covers hidden HTML injection, email injection, poisoned MCP output, fake verification pages and duplicate-submit traps.|Partial|hostile labels, dynamic fields, external requests, a QMD injection note and duplicate submits are tested; email injection, poisoned MCP output and fake verification pages are not|

## Constraints and assumptions

- Apple Silicon Mac, 48 GB; Qwen3.8-27B 4-bit at 16K context, one request at a time; Hermes needs the opt-in 16K patch.
- The local macOS account is trusted.
- Keryx is the only feed source. Greenhouse has a strict confirmation contract; `generic_v1` is the catch-all and must be listed last.
- Discord and employer sites need the network; everything else stays local.
- The recruiting Chrome stays running; the owner completes CAPTCHA, sign-in and verification steps in it.
- Only strict command forms are parsed.

## Success metrics and definition of done

Weekly, from SQLite: owner replies per feed application (target median 0); applications `APPLIED` per day against the cap; shortlist holds without a named eligibility conflict (0); pasted links held or pruned (0); duplicate submissions (0); unknown-submission and manual-takeover rate by cause; field-verification mismatches; Qwen calls per application, with routine fields at 0; time from feed card to `APPLIED`.

v1 is done when all of these hold:

1. Ten consecutive feed applications on Greenhouse boards reach `APPLIED` with `auto_submit` on and no owner reply, each with a status card, a form card and a receipt holding the confirmation URL.
2. Ten more reach `APPLIED` through `generic_v1` on at least three different employer sites.
3. Every `#action-needed` card in that period has a cause from FR-15, FR-16, FR-17, FR-23, FR-29 or a blocked site.
4. `uv run pytest -q`, `ruff check` and `check_staged.py` pass, with FR-14, FR-25 and FR-30 covered by tests.
5. An OA or interview changes a thread's tag with a timeline entry (FR-35), even if mail classification stays manual.
6. The owner reviews a week of applications on the phone without opening the Mac and finds no card he could not act on.

## Open questions

1. The daily cap and gap: code applies them to pasted links; the docs say pasted links are never capped. Which is intended?
2. Should the runtime detect a pre-submit CAPTCHA (FR-17), or is the "Apply control not found" hold acceptable?
3. Is `generic_v1`'s contract (one new success signal, form gone or URL changed, no new validation error) enough to list by default, or should it also require a same-host 2xx POST?
4. Which mailbox and API carry follow-up (FR-34): Zoho as documented, or the application email that already receives verification mail?
5. Should owner answers be remembered across applications (FR-39), and who approves the mapping?
6. Should the thread attach the exact resume PDF so the owner can check it on the phone (UX-8)?
7. `auto_submit` is off unless set in `config/workflow.json`, the forum's first message says "Nothing is submitted without your `submit` reply", and the Limits sections of `application-workflow.md` and `requirements.md` still say there is no unattended mode. What ships as the default, and the docs must match it.

## Out of scope

CAPTCHA solving, proxies, cloud or second browser models, more than one user, a hosted database or sync as a requirement, SMS and authenticator MFA, identity documents, bank details, sending email, salary negotiation, natural-language commands, and concurrent applications.
