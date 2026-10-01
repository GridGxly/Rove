# Erga Autopilot product requirements

Draft, 2026-09-30. When this disagrees with code and tests, the code wins.

## Purpose and problem

Applying by hand means opening the same forms hundreds of times, retyping approved facts, writing short answers under pressure, and losing track of what went where. Erga Autopilot does that work on the owner's Mac: take in a job, check it against approved facts, prepare an evidence-backed resume, fill the form, draft answers with a local model, send, and keep an exact record. The owner reads the record on a phone and is interrupted only for facts nobody else knows.

## Who it is for

One owner: a student applying to internships from his own Mac, reading Discord on his phone. He wants applications sent while he is in class and a record he can trust months later.

## Goals

1. Applications go out without a per-application reply; `submit` approval is an optional owner policy.
2. The owner is asked only for facts only he knows and for steps a site forces into the browser.
3. Every application has one readable record: what was sent, from where, what happened next.
4. Nothing is sent twice; nothing unclear is retried.

## Non-goals

Applying to everything; a hosted service; a second user; cloud models.

## The scenario, on a phone

Morning. `#internship-jobs` shows six new feed cards, each already queued. He does nothing.

The worker opens the newest in the background recruiting Chrome; Qwen lists the posting's hard requirements and code checks them against approved facts. It fits. Erga tailors the resume (the approved base PDF if the posting is bot-protected), the form is filled from the frozen profile and verified, two written answers are drafted and cleaned, and with `auto_submit` on the package is sent once and the confirmation checked. The thread's first message now reads **Applied ✅**; below it, one line per step, a form card with every value and its source, the drafts, the receipt.

`#shortlist` holds one card: "Your call on fit · must graduate by December 2026 conflicts with your approved facts." He replies `defer 3f9c2a7b1e04`.

`#action-needed` holds one card: "Answers needed · Which office do you prefer? (Austin / Remote)". He replies `answer 3f9c2a7b1e04 8b1d0e4f2a6c = Austin`; both cards disappear and the application is sent.

A link he pastes in `#agent-control` goes to the front of the queue and is never held on fit.

Evening. One thread reads "Submission unclear · do not click again". He checks his inbox and replies `applied` in that thread.

## Functional requirements

**Done** means code and a test prove it; **Partial** names the gap.

### Job intake

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-1|Each feed tick posts every new matching internship once, newest first, at most `batch_size` per tick, expiring backlog beyond `max_pending`.|Done|`test_feed_announces_each_job_once_and_supersedes_stale_duplicates`|
|FR-2|A pasted public HTTPS link is queued ahead of feed jobs and deduplicated by canonical URL, tracking parameters stripped.|Done|`test_tracking_parameters_do_not_create_duplicate_applications`|
|FR-3|A queued feed job whose posting closed or that now matches an exclusion rule is parked with the reason; pasted links are never parked.|Done|`test_closed_keryx_posting_parks_the_queued_application`, `test_queued_feed_jobs_are_deferred_when_approved_rules_exclude_them`|

### Fit decision

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-4|Code decides fit: a code-verified conflict on program, graduation window, work authorization, sponsorship, location or degree is `not_fit`; a conflict only Qwen claims is `needs_review`; skills never change it.|Done|`reasoning.decide`; `test_only_conflicts_on_eligibility_requirements_change_the_decision`|
|FR-5|Eligibility the posting states but code cannot check is listed on the job-fit and ready cards, never asked.|Done|`test_evaluate_review_lists_unverified_eligibility_instead_of_holding`|
|FR-6|A non-fit feed job gets one card in the thread and one in `#shortlist` with `go`/`park it`; a pasted link is never held on fit.|Done|`test_owner_links_skip_the_fit_hold_that_sends_feed_jobs_to_the_shortlist`|

### Resume

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-7|Erga intake yields a validated tailored PDF, or the approved base PDF with a warning when it fails (including a bot-protected posting); the bytes and SHA-256 are frozen and only that file is uploaded, verified.|Done|`test_failed_erga_intake_keeps_the_approved_base_resume_with_a_warning`, `test_uploaded_file_can_be_verified_after_widget_removes_input`|

### Form preparation

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-8|The worker follows only observed Apply controls, waits for fields, declines cookie banners (never Accept), retries a block once, and stops for the owner on a second block or an already-applied page.|Done|`test_late_rendered_forms_wait_out_the_spinner_and_decline_cookies`, `test_a_site_that_says_already_applied_stops_before_anything_is_sent`|
|FR-9|Known fields, radio groups, Yes/No buttons and selects are resolved from the frozen snapshot, batch-filled and verified; an option is chosen only on exactly one match; a changed value stops with a card naming the field.|Done|`live_browser._verify_batch`; `test_unassociated_labels_radio_groups_and_button_choices_resolve_from_approved_facts`|
|FR-10|A complete page with a Next/Continue control is advanced and filled again, at most four steps, until a final control appears.|Done|`test_multi_page_forms_are_filled_step_by_step_until_the_final_control`|
|FR-11|An optional field with no fact and no draft is left blank and noted in one line; only required questions reach the owner.|Partial|`worker.skip_optional`; untested|
|FR-12|An account wall offers `account ID create`; on approval the daemon registers with the application email and a generated password, stores it encrypted and signs in later; email verification stays with the owner.|Done|`test_account_creation_and_sign_in_use_the_encrypted_store_and_never_leak`|
|FR-13|A page with password fields or SSN, passport, bank or verification-code labels forces manual takeover; those values stay out of screenshots and model context.|Done|`live_browser.observe`; `test_missing_sensitive_fact_never_guessed`|
|FR-14|Voluntary self-identification questions take the form's own decline option by policy.|Done|`test_self_identification_questions_take_the_forms_decline_option`|
|FR-15|A CAPTCHA or MFA challenge is recognised and handed to the owner in the recruiting browser.|Partial|no CAPTCHA detection in `live_browser.py`; a pre-submit challenge reads as "Apply control not found"|
|FR-16|`generic_v1`, listed last, confirms a submission on a site without an ATS contract only from a new success signal, a form that left, and no new validation error.|Done|`test_generic_adapter_is_last_and_confirms_only_from_new_signals`|

### Written answers

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-17|Unfamiliar required questions go to Qwen as one bounded call, retried once, over the approved profile, stories, evidence and posting; a harness stop is a failure card, never a draft; an option not on the form becomes a question.|Done|`test_harness_stop_is_not_a_model_answer`, `test_proposed_value_outside_the_options_becomes_a_question`, `test_qwen_review_cannot_omit_or_invent_question_keys`|
|FR-18|Written drafts pass the Unslop and Humanizer scans with one bounded repair that keeps every number and name; the card shows the cleaned text and a summary. A `Story/Voice.md` note in the vault steers the voice.|Done|`test_drafts_get_one_bounded_cleanup_and_are_hashed_after_it`, `test_humanizer_scan_flags_shape_tells_and_passes_plain_prose`, `test_drafting_context_carries_the_owner_voice_note`|
|FR-19|With `auto_use_drafts` (or `auto_submit`) drafts become answers without a `use` reply; draft cards stay and a later `answer` overrides.|Done|`test_auto_policy_uses_qwen_drafts_and_queues_exactly_one_submission`|
|FR-20|A required question with no fact and no draft stops with one "Answers needed" card: up to six questions with options and copy-ready `answer` lines.|Done|`test_hold_fields_render_reasons_questions_and_commands_for_the_owner`|
|FR-21|Substantive answers are preceded by company research in a restricted context.|Not started|no research code|

### Submission

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-22|With `auto_submit` a complete package is sent once on the tick that prepared it, with no owner card; `max_submissions_per_day` and `min_minutes_between_submissions` pace feed jobs, never pasted or resumed ones.|Partial|`worker.next_queued`; cap and gap untested and applied to pasted links too, against the docs|
|FR-23|Otherwise submission needs an owner `submit ID HASH` for the current package; before one click, code re-checks URL, job scope, form, final control, profile version, uploaded resume and every required answer; the attempt is claimed in SQLite first and never repeated.|Done|`test_claim_needs_exact_owner_approval_and_never_repeats`, `test_preflight_accepts_only_the_reviewed_unchanged_form`, `test_guard_blocks_native_submit_until_one_approved_attempt_is_armed`|
|FR-24|Greenhouse is `APPLIED` only with a 2xx POST, no rejected POST, the confirmation URL and block, and no form left; anything else is `UNKNOWN_SUBMISSION` with `reconcile` commands and no retry.|Done|`test_greenhouse_confirmation_requires_every_signal`|
|FR-25|A confirmed submission is mirrored to Erga and the tag becomes Applied.|Partial|`submission.erga_confirm`; tests stub it|

### Record and archive

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-26|One forum post per application is created before preparation; an uncertain creation is held, not retried; entries and cards are stored before posting and retried each tick.|Done|`test_owner_cards_are_durable_and_retried_on_the_next_tick`|
|FR-27|The form card lists every filled field with value and source; the receipt keeps confirmation URL and text, before/after screenshots, response statuses without bodies, and the package hash.|Done|`workflow.event_embeds`, `submission._submit`|
|FR-28|A private per-application directory holds observation, package, resume, receipt, screenshots and Qwen input/output; a readable vault note mirrors them on every change.|Done|`test_application_note_is_written_to_the_vault`|

### Recruiting follow-up

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-29|Recruiting mail is classified into acknowledgement, OA, interview, offer and rejection events that add a timeline entry to the right thread.|Not started|no mail code|
|FR-30|Tags OA, Interview, Offer, Rejected, Accepted and Withdrawn can be set by mail or owner command, with a timeline entry and reminder.|Not started|`workflow.STATE_TAGS` knows only Preparing, Applied, Needs Action|

### Memory and profile

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-31|Onboarding fills eight fixed sections; `propose` validates schema and draft hash; `approve` is a local owner operation that rejects conflicts and writes an immutable snapshot plus the canonical vault note.|Done|`test_proposals_cannot_approve_or_invent_schema_and_stale_approval_fails`|
|FR-32|Each application freezes the approved profile hash; a manual edit to the canonical note blocks use until reviewed; a profile change after preparation blocks submission.|Done|`test_approved_snapshot_stays_frozen_and_canonical_edits_block_use`|
|FR-33|QMD indexes only the approved profile copy and rejects stale or modified sources; Hermes receives thirteen narrow tools, none of which fills, approves or submits.|Done|`test_candidate_memory_blocks_unapproved_stale_and_modified_sources`|
|FR-34|An owner answer is remembered for equivalent questions later, and `#memory` lets him review and correct approved facts from Discord.|Not started|`application_answers` is per application; no channel handler|

### Owner controls

|ID|Requirement|Status|Proof|
|---|---|---|---|
|FR-35|Replies (word and numbered forms inside an application's thread; explicit id forms in the control channel) are accepted only from the configured numeric owner, parsed by strict patterns, and never show the owner an id.|Done|`test_owner_command_cannot_be_forged_by_bot_or_other_author`, `test_word_replies_resolve_only_inside_the_applications_thread`, `test_numbered_replies_bind_the_exact_question_and_draft`|
|FR-36|The worker takes resumed applications first, then pasted links, then the newest feed job; holds while one application waits; hands back a stale `PREPARING` run; waits when the model is down.|Partial|`test_queue_holds_for_waiting_applications_unless_owner_resumes`, `test_tick_executes_one_approved_submission_and_recovers_crashed_runs`; model outage untested|

## UX requirements

|ID|Requirement|Status|Proof|
|---|---|---|---|
|UX-1|Each owner channel holds at most one live card per application; a new card replaces it, withdrawn when the application stops waiting.|Done|`test_leaving_a_waiting_state_withdraws_the_owner_cards`|
|UX-2|The thread's first post is a live status card (headline, one line, reply commands) edited in place so the forum list previews the state.|Done|`test_the_thread_status_card_mirrors_the_live_owner_card`|
|UX-3|Routine steps are one plain line; cards are for decisions, drafts, job fit, the filled form, results and failures, sized for a phone: title, one line, values in fields, at most six commands.|Done|`workflow.event_embeds`; `test_brief_keeps_whole_leading_sentences_and_never_goes_empty`|
|UX-4|Every hold card says what happened, why, the questions only the owner can answer, and the exact reply in a code block.|Done|`test_hold_fields_render_reasons_questions_and_commands_for_the_owner`|
|UX-5|No internal jargon reaches the owner: sources and states appear in words, never as keys.|Partial|`answer` lines show 12-hex field keys; "Qwen", "Unslop" and "package" appear on cards|
|UX-6|The recruiting browser never takes focus: background tabs, window behind the owner's work, open tabs capped.|Done|`live_browser.restore_front`; checked by hand|
|UX-7|The owner can act on everything from the phone except steps a site forces into the browser.|Partial|the resume PDF and live form still need the Mac|

## Security and privacy requirements

|ID|Requirement|Status|Proof|
|---|---|---|---|
|SP-1|External content (page text, labels, tool output, QMD results, email) informs but never authorises: it cannot change facts, choose files, widen tools or trigger submission.|Done|`test_dynamic_fields_and_external_requests_cannot_gain_authority`|
|SP-2|The model has no submit tool; only the browser daemon submits, after a durable claim.|Done|`submission.claim_attempt`; `test_real_mcp_reads_only_approved_sections_and_exposes_no_approval`|
|SP-3|Personal data is entered only on public HTTPS pages matching the verified employer and job scope; private networks and a shared ATS hostname are refused.|Done|`test_same_ats_different_employer_or_job_is_not_same_scope`|
|SP-4|Uploads come only from the frozen package; an unapproved path fails before it is read.|Done|`test_unapproved_file_rejected_before_read`|
|SP-5|Employer credentials are Fernet-encrypted with a separate owner-only key and never appear in the vault, Discord, logs or model context.|Done|`credentials.py`; `test_account_creation_and_sign_in_use_the_encrypted_store_and_never_leak`|
|SP-6|SSNs, bank details, identity documents, verification codes and SMS/authenticator MFA are never automated or bound as answers.|Done|`worker.apply_command`; `test_missing_sensitive_fact_never_guessed`|
|SP-7|The recruiting browser is a daemon-launched Chrome with its own profile and a localhost-only DevTools port.|Done|`live_browser.ChromeLauncher`|
|SP-8|Receipts and logs keep response statuses only, never headers, bodies, tokens or applicant values; the repo holds synthetic fixtures, checked for secrets before commit.|Done|`submission._submit`; `scripts/check_staged.py`|
|SP-9|The adversarial suite covers hidden HTML injection, email injection, poisoned MCP output, fake verification pages and duplicate-submit traps.|Partial|email injection, poisoned MCP output and fake verification pages are untested|

## Constraints and assumptions

- Apple Silicon Mac, 48 GB; Qwen3.8-27B 4-bit, 16K context, one request at a time.
- The local macOS account is trusted.
- Keryx is the only feed source; Greenhouse has a strict contract and `generic_v1` is the catch-all.
- The recruiting Chrome stays running; the owner completes CAPTCHA, sign-in and verification in it.

## Success metrics and definition of done

Weekly, from SQLite: owner replies per feed application (median 0); shortlist holds without a named eligibility conflict (0); pasted links held or pruned (0); duplicate submissions (0); unknown-submission and manual-takeover rate by cause; Qwen calls per application.

v1 is done when:

1. Ten consecutive feed applications on Greenhouse boards reach `APPLIED` with `auto_submit` on and no owner reply, each with a receipt.
2. Ten more reach `APPLIED` through `generic_v1` on three different employer sites.
3. Every `#action-needed` card in that period has a cause from FR-12, FR-13, FR-15, FR-20, FR-24 or a blocked site.
4. `pytest`, `ruff check` and `check_staged.py` pass, with FR-11, FR-22 and FR-25 tested.
5. An OA or interview changes a thread's tag with a timeline entry (FR-30).
6. The owner reviews a week of applications on his phone alone and finds no card he could not act on.

## Open questions

1. Code caps and paces pasted links; the docs say it never does. Which is intended?
2. Should the runtime detect a pre-submit CAPTCHA (FR-15), or is the current hold acceptable?
3. Is `generic_v1`'s contract enough to list by default, or should it also require a same-host 2xx POST?
4. Which mailbox carries follow-up (FR-29): Zoho, or the application email that already gets verification mail?
5. Should owner answers be remembered across applications (FR-34), and who approves the mapping?
6. Should the thread attach the resume PDF for phone review (UX-7)?
7. `auto_submit` is off unless set in `config/workflow.json`, and the forum's first post, `application-workflow.md` and `requirements.md` still say nothing is sent without a `submit` reply. What ships as default? The docs must match.

## Out of scope

CAPTCHA solving, proxies, cloud or second browser models, more than one user, hosted database or sync, SMS/authenticator MFA, identity documents, bank details, sending email, salary negotiation, natural-language commands, concurrent applications.
