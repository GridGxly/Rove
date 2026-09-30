# Browser automation

Erga Autopilot needs browser automation to feel fast without giving up correctness, auditability, privacy, or submission safety.

The reference design keeps **Qwen3.8-27B** as the local reasoning model. There is no required cloud browser-decision model and no Jev/TypeSafe runtime dependency.

The speed goal comes from moving routine browser mechanics out of the model loop.

Qwen should handle ambiguity. Normal code should handle observation, deterministic field resolution, batching, execution, waits, verification, and irreversible-action gates.

## Design goals

The browser layer should optimize for:

- correct answers before raw application count
- low mechanical latency on ordinary forms
- as few Qwen calls as practical
- compact browser observations instead of repeated full-page dumps
- deterministic handling of known profile fields
- safe recovery from dynamic pages
- complete field-level audit history
- local-first handling of applicant data
- independent verification before and after submission

A fast run is useful only when it is also reproducible and safe.

## What Qwen does

Qwen3.8-27B remains the reasoning model for work that actually needs judgment.

Examples:

- interpreting unfamiliar application wording
- deciding whether a question maps to an approved profile fact
- drafting substantive written responses
- choosing relevant evidence
- researching a company when a response needs context
- recovering from an unfamiliar browser state
- explaining why the browser cannot safely continue

Qwen should not be asked to rediscover obvious mechanics such as which field contains the first name, whether the email is already filled, or which known resume file belongs to the frozen package.

The browser runtime should resolve those cases directly from structured state.

## Fast-path architecture

A normal application should use the fastest safe path available:

```text
frozen application package
        ↓
detect page / ATS shape
        ↓
compact structured observation
        ↓
normalize application fields
        ↓
resolve known fields deterministically
        ↓
batch-fill safe resolved fields
        ↓
verify values and dynamic changes
        ↓
Qwen only for unknown or ambiguous work
        ↓
repeat compact observation if needed
        ↓
pre-submit verification
        ↓
controlled submit
        ↓
independent confirmation
```

The generic browser loop remains the fallback. It should not be the first choice for every field.

## Browser runtime

The production browser path should use one dedicated recruiting browser profile and a long-lived Playwright/Chromium session.

The runtime may use Playwright browser APIs and narrowly scoped CDP helpers internally where they reduce browser round trips or provide better state inspection. That low-level capability belongs inside trusted code.

Do not expose arbitrary Playwright or CDP execution directly to Qwen.

Playwright MCP can still be useful for development, debugging, manual inspection, and fallback tooling. The production application flow should not require a general MCP round trip for every field and click.

### Compact observation

A useful observation should gather the page state needed for the next chunk of work in as few browser calls as practical.

Prefer:

- visible and actionable controls
- labels and roles
- current values and checked/selected state
- required/disabled/read-only state
- available options
- nearby form or dialog context
- current URL and document identity
- limited visible text relevant to the current form
- stable code-owned references for observed elements during that page state

Avoid sending full markup, scripts, giant accessibility trees, or screenshots on every step when structured DOM state is enough.

Screenshots and traces remain important artifacts and debugging tools. They are not the default reasoning input for routine form filling.

### Semantic freshness

Do not invalidate an action just because the DOM changed somewhere.

Animations, timers, analytics widgets, and unrelated page updates should not automatically force Qwen to reason again.

Before acting, code should verify the state that matters for that action:

- document and expected URL
- target identity
- target visibility and enabled state
- relevant form values
- nearby dialog/form/row context
- current geometry or occlusion when needed

If the relevant state changed, observe again.

### Bounded waits

Avoid fixed multi-second sleeps.

After an action, wait for the event or state the application actually needs, with a short upper bound:

- a combobox suggestion appears
- a dependent field becomes enabled
- navigation begins or completes
- validation text changes
- a new form step appears
- a submit result becomes visible

Long waits should be evidence-driven rather than the default.

Page settling is one such wait. A single-page board often paints its shell, a cookie
banner and a loading indicator before the form, and the banner text alone looks like a
rendered page. The runtime waits for fields or body text, then declines a cookie banner
it recognises by the banner's own wording (Reject, Decline, Necessary only; never
Accept), then waits out a visible loading indicator, each with a ten-second bound. After
an application-start link, a page with no fields, links or sign-in controls gets one more
bounded wait for fields before it is reported as having no form.

## Normalize the form before filling it

The browser should inspect a form as a group rather than asking Qwen to choose one field at a time.

A normalized field record can contain:

```text
field_id
label
kind
required
current_value
options
checked_or_selected_state
sensitivity
page_or_step
dependencies
resolution_state
resolved_value
provenance
```

Typical resolution states:

- `resolved`
- `unknown`
- `needs_qwen`
- `needs_user`
- `manual_only`
- `optional_skip`

The resolver should use the normal source order:

```text
frozen approved profile snapshot
        ↓
approved Erga evidence
        ↓
approved story/narrative context
        ↓
approved remembered equivalent answer
        ↓
Qwen judgment or drafting
        ↓
user question when still unknown
```

Do not invent a value just to keep the browser moving.

## Batch deterministic work

If a page contains ten fields and eight already have approved deterministic answers, fill those eight as one execution batch where the browser/runtime safely supports it.

Examples include:

- name
- email
- phone
- location
- school
- degree
- graduation date
- work authorization
- sponsorship answer
- portfolio links
- previously approved standard application answers

After a batch, verify the resulting values against the frozen application package.

If one field fails, isolate that field rather than discarding the entire page state or asking Qwen to redo everything.

File uploads are a separate controlled operation. The browser may upload only files already included in the frozen application package.

## ATS-aware acceleration

Treat unfamiliar career sites as normal websites first, but allow small, versioned adapters when a recurring ATS has stable structure that can be used safely.

An adapter may:

- detect the ATS
- read official/publicly exposed job or form metadata
- normalize recurring field patterns
- handle known widgets
- reduce redundant browser discovery
- provide deterministic verification helpers

An adapter must not:

- bypass employer authentication
- use employer-only write APIs without legitimate authorization
- weaken domain checks
- invent applicant answers
- bypass the frozen application package
- bypass submission gates

The generic browser path must remain available when an adapter does not match or stops working.

## Qwen escalation policy

The browser runtime should call Qwen only when normal code cannot safely resolve the next step.

Good reasons to escalate:

- the field meaning is genuinely ambiguous
- a custom written response is required
- the page presents unfamiliar validation or recovery behavior
- two approved facts appear to conflict
- a new question does not map cleanly to the current profile schema
- generic structured browser controls cannot determine a safe next action

Bad reasons to escalate:

- a known field has a known approved value
- a checkbox already has the requested state
- a standard select option is already observable
- the browser needs to wait briefly for a known UI event
- a deterministic batch can complete the work

The goal is not to make Qwen faster at clicking. The goal is to avoid asking Qwen to click when code already knows what to do.

## Fallback ladder

Use the least expensive reliable mechanism that can complete the step:

1. ATS-aware metadata or adapter
2. normalized deterministic form resolution
3. batch browser execution
4. generic structured browser actions
5. Qwen recovery or interpretation
6. manual takeover

Do not add a second required browser model just to make the loop faster.

If a surface cannot be understood safely from structured state and Qwen cannot recover with the current local stack, pause for manual takeover rather than silently introducing a cloud vision or browser model.

## Submission remains separate

Fast preparation does not change submission policy.

`APPLICATION_PREPARE` may inspect, resolve, fill, upload approved files, and verify the prepared form. It must not gain final submission capability when submission is disabled.

`APPLICATION_SUBMIT` may act only on the frozen validated application package it was given.

Before Submit becomes executable, code should verify:

- expected employer or ATS destination
- correct job/application identity
- approved profile snapshot/version
- exact resume and file hashes
- all required known fields
- exact approved written answers
- unresolved warnings
- sensitive/manual-only fields
- submission policy state

Record the submission attempt before or atomically with the irreversible action according to the final transaction design.

If the result is ambiguous after Submit, move to unknown-submission state. Do not blindly retry.

A model saying `DONE` is never independent proof that submission succeeded.

## Performance and reliability metrics

Benchmark the browser layer with synthetic and controlled test applications before optimizing for daily volume.

Track at least:

- total application preparation time
- mechanical browser time
- number of browser observations
- browser protocol round trips when measurable
- number of Qwen calls
- Qwen time spent on browser recovery versus substantive reasoning
- batch-fill success rate
- post-fill verification mismatches
- stale-action/retry count
- fixed-wait time
- manual takeover rate
- peak memory and swap pressure
- submission verification success
- duplicate-submission count

The target is not a marketing number such as "seven seconds per application."

The target is to make routine mechanical work cheap enough that the remaining time is dominated by real page loading, substantive writing, user decisions, and employer-side behavior.

Duplicate submission count should remain zero.

## Concurrency

Optimize one application path before adding concurrency.

The reference runtime starts with one active Qwen request at a time until the 48GB Apple Silicon setup is measured under load.

Later, deterministic browser work may run concurrently when it does not compete unsafely for browser state, memory, credentials, or the model. Concurrency must not weaken application ordering, audit history, or submission idempotency.

## Security and privacy

Fast browser automation does not change the trust model.

- use a dedicated recruiting browser profile
- keep applicant/browser state local by default
- do not add a required cloud decision model
- treat page text and browser labels as untrusted data
- keep arbitrary shell, filesystem, Playwright, and CDP execution away from the application model
- verify expected domains before entering personal data
- upload only approved frozen-package files
- keep manual-only sensitive values manual
- preserve field-level provenance
- preserve exact submitted artifacts and confirmation evidence

Read [Security](../SECURITY.md) and [Prompt injection](prompt-injection.md) before changing the browser executor or its tool surface.

## Runtime as implemented

The daemon launches Google Chrome (or Chrome for Testing) as its own app instance with
the dedicated profile and connects over a localhost DevTools port with Patchright. A
benchmark of anti-detection tooling found that the signal bot managers act on is the
automation control protocol's shape, not static traits, so the browser is never started
by an automation library (no automation flag, `navigator.webdriver` false) and the
Playwright fork patches the remaining protocol leaks. Real Chrome on real hardware and a
residential home connection are what paid "stealth" browsers imitate.

Behavior is paced: randomized pauses, mouse travel before clicks, typed short values,
and a front-door visit before a deep link. Block pages are recognized and retried once
patiently; a second block is handed to the owner. Detection is measured, not assumed:
a private verification script launches a separate instance with the same flags against
public bot-detection pages and saves the report.

Launch activates Chrome once; the daemon hands focus back for a few seconds afterwards.
Tabs are created in the background and never take focus. Only one client may drive the
daemon's Chrome; diagnostics use a separate instance. The destination guard attaches to
a page only during automated operations, because a route handler runs only while the
daemon is inside a browser call and would stall manual browsing otherwise.

## Implementation order

Build the fast path in this order:

1. instrument the current browser baseline
2. establish a persistent dedicated browser runtime
3. build compact structured observation
4. normalize forms into field records
5. resolve deterministic fields from frozen approved state
6. add safe batch execution and post-fill verification
7. add targeted Qwen escalation for ambiguity and recovery
8. add small ATS adapters where repeated evidence justifies them
9. add pre-submit and post-submit independent verification
10. benchmark across multiple ATSes and custom forms
11. add concurrency only after single-run correctness and memory behavior are proven

Reliability gates come before unattended volume.
