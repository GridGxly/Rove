# Browser automation

Rove fills application forms in a browser of its own: the Rove Browser, a copy of the owner's Google Chrome under its own name. Code observes the page, resolves fields from approved data, fills them, and reads the values back. Qwen handles unresolved questions and bounded visual actions inside CAPTCHA frames. It receives no general browser execution tool.

The path of a whole application, including sending, is in [Application workflow](application-workflow.md).

## The browser daemon

`rove browser serve` is a long-running process, installed as the launchd service `dev.rove.browser`. It owns the browser and listens on a Unix socket in the state root that only the owner's account can open. A lock file keeps a second daemon from starting.

The socket accepts ten actions: `open`, `observe`, `follow`, `prepare`, `register`, `login`, `reopen`, `close`, `submit` and `status`. None of them runs caller-supplied JavaScript, takes a file path, or takes a field value. Values come from the frozen profile and the answers stored for the application.

The worker is the daemon's normal client. If the socket is missing, the worker asks launchd to start the service and waits for it.

The service starts at login, but the browser does not. The first action that needs it (`open`, `observe`, `follow`, `prepare`, `register`, `login`, `reopen` or `submit`) launches it; `status` and `close` only reconnect to a browser that is already running. After a reboot there is no Rove Browser until a job starts.

## The Rove Browser

The recruiting browser used to be `/Applications/Google Chrome.app` itself, started with a separate profile. macOS treats every instance of one app bundle as the same app, so a click on Chrome in the Dock, or a link opened from another app, could land in the recruiting profile. The owner saw his own Chrome open with the recruiting profile's shortcuts and a sign-in prompt.

The recruiting browser is now its own app. `rove browser install` copies `/Applications/Google Chrome.app` to `browser/Rove Browser.app` in the state root and changes only its identity:

- `CFBundleIdentifier` becomes `dev.rove.browser`, `CFBundleName` and `CFBundleDisplayName` become `Rove Browser`.
- `Contents/Resources/app.icns` is replaced with Rove's icon, built from `src/rove/assets/rove-app-icon.png` with `sips` and `iconutil`, and `CFBundleIconName` is dropped so the icon file applies instead of Chrome's asset catalog. The Dock shows the ring mark.
- `KSUpdateURL`, `KSVersion` and `KSBrandID` are removed. Chrome registers with Google's updater only when those are present, so the copy is never updated or touched by it. `KSProductID` and `KSChannelID` stay: Chrome reads its channel from them, and without them it reports an unknown channel.
- The framework versions that Chrome keeps around after an update are dropped; only the one the binary loads stays.
- Every other key, the framework and the helper apps are untouched.

The copy is then signed ad hoc, deepest code first. Chrome's own signature carries entitlements that need Google's certificate (the application identifier, keychain groups, `com.apple.developer.*`) and the hardened runtime with library validation; a binary that keeps any of them under an ad-hoc signature is killed at launch, so the copy keeps neither. Nested code keeps its identifiers. `codesign --verify --deep --strict` must pass before the copy replaces the previous one. Nothing is downloaded, and the whole build takes a few seconds because the copy is a clone on APFS.

A record in `browser/rove-browser.json` holds the Chrome version the copy was built from, the source path and the time. `rove browser status` names the app in use, its version and whether it is the shared Chrome. `rove browser install --check` reports without building, and a second `install` on the same Chrome version does nothing.

### Same binary, same fingerprint

The copy is the owner's Chrome: the same engine, version, codecs, Widevine module and component set, started with the same flags. That matters because sites that block automation compare what they see with real Chrome, and a different build (Chrome for Testing, for example, lacks Widevine and has its own brand strings) is a tell.

Parity was checked on the reference Mac with stock Chrome and the copy each in a fresh profile, both driven through Patchright over the DevTools port on a local page: `navigator.userAgent`, `userAgentData.brands` and the high-entropy values (`fullVersionList`, `platformVersion`, `architecture`, `model`, `uaFullVersion`, `bitness`), plugin and MIME lists, `pdfViewerEnabled`, WebGL vendor and renderer, `requestMediaKeySystemAccess("com.widevine.alpha")`, `navigator.webdriver`, `window.chrome`, screen, time zone, languages, hardware concurrency, device memory, codec support, a canvas hash and the CDP version were identical. The public bot-detection pages used for the project's earlier check report the copy the same way they report stock Chrome.

### Following Chrome updates

Chrome updates itself; the copy does not. At every daemon start and before every launch the daemon compares `CFBundleShortVersionString` of `/Applications/Google Chrome.app` with the record and rebuilds the copy when they differ, with one line in `system-log`. It never rebuilds while the Rove Browser is running; `rove browser status` then shows the lag, and the rebuild happens after the browser quits. `rove browser install` does the same by hand.

### The profile

The Rove Browser opens `browser/recruiting-profile-rove` in the state root. It is launched with `--use-mock-keychain`: a bundle the Keychain has never seen would otherwise ask for "Chrome Safe Storage" at every start. With the flag, the profile's cookies and passwords are encrypted with a fixed key, which is acceptable for an isolated recruiting profile protected by its own permissions. Because that changes how stored cookies are encrypted, the copy does not reuse the old `browser/recruiting-profile`; that folder is left as it is.

Keep the profile for recruiting only. It should hold no banking sessions, no personal password manager and no browser sync.

### The shared Chrome, by name only

`browser_app: "shared-chrome"` in the private workflow config runs `/Applications/Google Chrome.app` the old way, with the old `recruiting-profile` and without the mock keychain. The daemon then writes one warning line to `system-log` and to its error log at every start, because the Dock problem above is back. Nothing falls back to the shared Chrome on its own: when the Rove Browser is missing, the daemon fails with "run `rove browser install`".

When the daemon starts and finds a recruiting profile held by a browser that is not the configured app (the shared Chrome from before the switch), it stops that instance with SIGTERM, Chrome's clean shutdown, so the configured app can take over. The holder is read from the profile's own lock file.

### Launching and tabs

The daemon starts the app as a separate instance with `open`, with a DevTools port on localhost, and connects to that port with [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), a Playwright fork. Because the daemon starts the browser itself, it runs without an automation flag. Launching brings the browser to the front for a moment, so the daemon notes which app had focus and hands focus back for a few seconds afterward. This needs no macOS permission.

Every application gets its own tab, created in the background through the DevTools protocol. Tabs do not take focus. The window stays behind the owner's work and can be opened from the Dock.

- A tab closes when its application is applied or the owner parks it.
- At most `max_open_tabs` tabs stay open (default 5). The oldest is closed first, and a closed tab reopens on `go`.
- The browser outlives the daemon. After a restart the daemon reconnects to the same port, keeps the tabs that belong to applications still waiting, and closes the rest.

Drive that browser with one client only. During development a second Playwright client attached to the same instance stalled it.

After changing browser code, restart the service with `launchctl kickstart -k gui/$UID/dev.rove.browser`. The browser window and its tabs survive.

Patchright's own Chromium build, installed by `patchright install chromium`, runs the tests and the synthetic fixture only. It is never the recruiting browser.

### Pacing

With `human_pacing` on (the default), the daemon pauses briefly before a click, moves the pointer to a control before clicking it, types values of up to 80 characters key by key, and visits a site's front page before its first deep link. Reading or verifying an unchanged field adds no pacing delay. Tests turn pacing off.

## Where applicant data may be typed

Several checks run before any value is entered.

While the daemon drives a tab, every request the tab makes is checked: public HTTPS on port 443, to a host that resolves only to public addresses. Anything else is aborted. The check is attached for the length of one operation and removed afterward, so a tab the owner browses by hand is never stalled.

`follow` clicks only a control the last observation reported as an application-start link: Apply, Apply now, Start application and similar wording. The observation must still be current and the control's label unchanged. A link whose address is on another host is followed only when that host is on the applicant-tracking list.

`prepare` types nothing unless both of these hold:

- The form's host and path match a board in `src/rove/destinations.py`. The list covers Greenhouse, Lever, Ashby, Workday, Oracle Cloud, iCIMS, SmartRecruiters, Eightfold and Workable, plus employer hosts added to it in code.
- The page has the same job scope as the queued link. For Greenhouse the scope is the board and the job number. For Lever and Ashby it is the company and the posting. For other hosts it is the host and the job number in the path, or the path itself when there is no number.

A shared applicant-tracking hostname is never enough. A form for another employer or another job on the same host does not match.

Account creation is limited to a host on the list or the host of the queued link.

## Observation

One script evaluation returns everything the next step needs:

- the title and the first 15,000 characters of visible text
- every visible input, textarea and select, and every file input, with its label, kind, role, placeholder, required and disabled state, current value, character limit and options (up to 300 for a select)
- groups of pressed-state buttons, such as a Yes/No pair, as one choice
- application-start links, Next and Continue controls, sign-in and account controls, and final Submit controls
- markers: a visible CAPTCHA, "already applied" wording, the Greenhouse confirmation block, Lever's success heading and verification error, and the text of visible status and validation messages

A field's label follows accessible-name precedence: `aria-labelledby`, then `aria-label`, then its associated label, the nearest label within four ancestors that owns no other control, and finally the placeholder. This keeps a compound phone field's country-code selector distinct from the phone number. Radio buttons that share a name are reported as one question with options.

A field counts as required when the input says so, or when its label carries a `required` class or ends in an asterisk.

The values of password, hidden and file inputs are never read. On a page with a password field or a field labeled social security, passport, bank account or verification code, all field values are dropped, the text is cut short, and no screenshot is taken. Identity steps are flagged for the owner; an email-code step may continue through the verified mailbox flow below. Failure screenshots also inspect the current frames for secrets, including when a failure happened before the next observation.

Every other observation is saved privately with a screenshot of the viewport. Page text is marked as untrusted data and cannot authorize anything.

### Waiting for the page

Single-page boards often paint a shell, a cookie banner and a loading indicator before the form. After a navigation the daemon waits, each with a ten-second bound, for fields or body text and then for a visible loading indicator to go away. In between it declines a cookie banner it recognises by the banner's own wording (Reject, Decline, Necessary only). It never clicks Accept.

After an application-start link, a page with no fields, links or sign-in controls gets one more bounded wait for fields before it is reported as having no form.

### Block pages and closed postings

A page with no fields whose title or opening text says "Access Denied", "Pardon our interruption", "Just a moment...", a reference number, or similar is treated as a block. The daemon waits 12 to 30 seconds, enters through the site's front page, and tries once more. A second block hands the application to the owner.

A page with no fields that says the posting no longer accepts applications parks the application.

## Matching a field to an approved fact

A label is matched by exact wording first: first, middle, last and preferred name, full name, email, phone, city, state, postal code, country, LinkedIn, GitHub, portfolio or website, and "City, State" for a location. School, major, degree, and GPA when the profile allows disclosing it, are matched when the profile has exactly one school.

A short plain label is also matched by meaning. "Profile Link (Optional)" is the portfolio, "LinkedIn Profile URL" is LinkedIn, and "Mobile Number" is the phone. A label of more than five words, or one that names a reference, manager, supervisor, emergency contact, employer or recruiter, or that contains words such as upload, verify, ignore or instruction, is never matched this way. "(Optional)" and "(Required)" are ignored.

A few questions are answered from approved eligibility and preferences:

- "Are you legally authorized to work in the United States" takes the approved answer.
- "Will you now or in the future require sponsorship" is answered No only when both approved sponsorship answers are No.
- The employer's own name in either question does not change it: "to work for Example in the United States" and "sponsorship from Example" are the same two questions. A length of time, an office, another employer or a family member in that place makes it a different question, which stays the owner's.
- A question about relocating, commuting or working onsite is answered Yes when the approved preferences say relocate and include onsite work, unless it names a location the owner excluded.
- A graduation question shown as a group of options takes the option that matches the approved graduation month.

A phone-type field defaults to Mobile, and the form card shows that as a default. Every other question is left for the owner or for Qwen. Code never guesses among options or legal wording.

## Filling and verifying

Each page is filled in one pass and then observed again.

Text fields are typed and read back. The value passes when the site kept it, changed only its whitespace, or reformatted a phone field without changing its number (a US +1 prefix may be omitted). Other fields never use phone-number matching. Any other difference stops the run with a card naming the field.

- A field that already holds a different value is left alone and becomes a question.
- A US phone is typed as ten national digits first, because sites with their own country selector reject a repeated code. If a step then rejects the phone, the international form is tried once.
- A text the site silently truncates is cut to what the field keeps, at a sentence boundary, and the form card says so.
- A native select is set only when exactly one option matches. Country options match under the common spellings of United States.
- Searchable dropdowns receive keyboard events and use their linked popup, including ARIA grids. A selected row must commit and close the control or expose selected-state evidence. US state names also match their [USPS abbreviations](https://pe.usps.com/text/pub28/28apb.htm). An approved county answer can disambiguate city and ZIP rows, even when the county field appears later on the page. A same-named city in a different state is rejected. Multiple remaining locations stay unresolved and the owner card explains the ambiguity. The click targets the observed row text, so reordered results cannot redirect it to a different row.
- A radio group or button group is set to the matching option and checked afterward. An option that is already selected is recorded and not toggled.
- A long-text field is filled only from an answer stored for the application: one the owner typed, a draft the owner approved, or a draft used under the `auto_use_drafts` policy.

A field that appears after filling becomes a question. It is never answered from a guess.

### The resume upload

Only a file field named for a resume or CV is filled, and only with the frozen `resume.pdf` in the application's folder. Its hash is checked before the upload, and the input is checked afterward to hold that one file. A required file field for anything else becomes a question, and an optional one is skipped.

Some boards remove the upload control once it has the file and show the file's name instead. The daemon marks the page that took the resume. When a pass runs again on that same page (after an optional question was left blank or a draft was filled in) and the page no longer offers a resume upload, the package records the same attachment. A page that was loaded again has lost the mark and shows its control, so the file is attached again. Without the mark nothing counts as attached, and the send is refused.

### Dropdowns and place pickers

A field is treated as a picker when it is a combobox, when it has an autocomplete hint or a "start typing" placeholder, or when its label is a place label such as Location, City or "Where are you located".

For an ordinary picker the daemon opens the list and selects the option whose text equals the approved value, when there is exactly one. Typing into the search box does not count as a selection. The choice is confirmed from the widget: the selected option, the displayed value, or the accessibility announcement.

For a place picker the daemon types the approved city and waits up to six seconds for suggestions. It requires one matching city, preferring the approved state (including its USPS abbreviation). Multiple matching counties or states remain unresolved. A popup without ARIA option roles is read within the input's linked popup or nearby container; unrelated menus cannot supply a choice. Rove never takes the first suggestion just because a wait expired. The chosen row must commit into the input or the widget's selected-value display.

A picker that refuses a known value is recorded as a browser control problem. It does not ask Qwen to invent another answer. The daemon keeps private evidence for a fix: what it typed, the options the picker listed, what the input kept, and a screenshot when no secret field is present. Missing address facts also stay out of model drafting.

County grids may list a county with its state, such as "Example County, KS". Rove
matches the approved county name and rejects a different displayed US state. More
than one matching row remains unresolved. Failed picker evidence includes the linked
rows' text and visible/selected state, including a list that changed before selection.

When no value is known, the daemon opens the list once to read its options so Qwen and the owner see the real choices.

That read includes rows in the input's linked grid. It lets the standing policy use
the form's own decline option for self-identification instead of asking for a
demographic fact. A named month control may match an approved numeric month to the
same month written out; duplicate matching options still stop selection.

### Multi-page forms

When a page is complete, has no final Submit control and shows a Next or Continue control, the daemon clicks it, waits for the next step's fields, and fills again. It does this for at most eight pages.

When a later page introduces questions that need drafting, the worker can resolve up
to eight new question batches in one preparation pass under the configured draft
policy. Each batch is saved before the browser uses it. A repeated unresolved question,
missing required fact, unapproved draft or manual verification stops the pass. Optional
fields without an approved fact or draft remain blank. A form that keeps adding new
questions reaches a named continuation hold; it is never called ready or submitted.

A first step that only asks for an email address to start under (an email field, at most two more fields, and a Next control) counts as the form's first page. It is filled from the profile like any other page, after the fit review and the site check.

When a step does not move, the data requests the page made during the click (method, path and status, never a query string or a body) are saved as `step-stuck.json`, and the system log gets one line with their count and statuses.

If the page after a Next click reads as the site's own "application received" page, the run stops with "The site may have taken the application". It is never counted as sent on a guess: the owner looks and replies `applied` or `park it`.

If the click leaves the same page with a validation message about the phone, the other phone format is tried once. If the form never reaches a step with exactly one final control, the run ends with "Final step not reached" and the site's message when there is one. A form in that state is never called ready.

### Required fields the site names

When a site rejects a submission and names a "required field", the label is saved for that application. The next preparation treats a field with that label as required, even if the page does not mark it.

## The package

Preparation ends by writing a package: the page URL, the profile hash, the resume hash, every filled field with its value and source, the open questions, the full form state, the final control and the pages visited. The package is hashed. It is ready for review only when no question is open and the page has exactly one final control.

Sending checks the live page against this package field by field. See [Application workflow](application-workflow.md#sending).

## The submit guard

Every page in the Rove Browser gets a script that blocks form submission events unless a flag on the document is set. Submission code sets the flag for one click and removes it afterward. The guard stops accidental submits during preparation, including the Enter key in a field. It is not a network-level guarantee against a page script that posts on its own.

Some sites wire a step's Next control as a submit of that step's own form. The flag is therefore also set for the one observed click on a control read as Next or Continue, and removed as soon as the step was taken. It is never set for any other click during preparation.

## Steps in front of the form

Two kinds of step are not questions, and `gates.py` holds their rules.

**A picture check (CAPTCHA).** Rove tries the existing local Qwen vision model first (`captcha_solver: "local"`, the default). Only a visible frame on an allowlisted CAPTCHA provider is captured. The image stays on this Mac; the model gets no applicant profile, cookies or tokens. Code accepts only bounded click or drag coordinates inside that frame, checks that the challenge's instruction and media identities have not changed, and limits an attempt to eight rounds and 90 seconds. Animated challenges receive a bounded sequence of frames. Set `captcha_solver: "manual"` to skip the local attempt. Unsupported or unsuccessful challenges still produce one "CAPTCHA needs you" card and preserve the tab.

Progress is measured by visible form identities and the site's result, not a count of inputs. Hidden CAPTCHA response textareas cannot mark a challenge solved. After Next, the browser waits for a delayed challenge and for the actual next form to render. Resuming an answer or verification hold keeps the existing application tab when it still belongs to the same job. Searchable dropdowns read their own linked grid; a shared calling code such as +1 requires an exact country match.

Detection watches throughout the bounded wait after Next. A hidden response textarea appearing does not end that wait. Progress compares the URL and identities of visible form controls, excluding hidden controls, CAPTCHA fields and challenge dialogs. Closing a challenge alone does not mean it was solved. The local solver accepts a response token or a real form transition after the challenge disappears; tokens never leave the browser. The fallback watcher resumes only after a real form transition, and the log says the check cleared without guessing who solved it. Submission still requires the normal independent confirmation contract.

A challenge that comes up after a Next click closes by itself when nobody answers it, so the card names the control to press ("press “Next” on this application's tab and solve the check that comes up") instead of pointing at a check that may be gone.

**A tab left to the owner.** The guard that blocks form submits while Rove fills a page would also swallow the owner's own press of a Next, a Sign in or a Verify. When a run stops on a step that is his to take in the browser (a CAPTCHA, a sign-in, a manual step, a form Rove could not reach or finish, a send he makes himself), the worker asks the daemon to lift the guard on that tab's page as it stands (`hand_over`). A page loaded afterwards has the guard again. The guard is put back on every frame of a tab before any operation in which Rove drives it. The card that asks for `send it` is not such a step: that send stays Rove's.

**A code mailed to the owner's application address.** A page that says it sent a code and shows the boxes for one (a single box, or four to eight one-character boxes) is a code step. The daemon reads the code from the owner's own Zoho mailbox with `mail.verification_code`: only Inbox mail that arrived after the step that asked for it, only from the site's own mail domains (the form's domain, its board's sending domain, such as `oracle.com` for `oraclecloud.com`), and only when Zoho's verdict says that domain really sent it. It waits up to two minutes, types the code into the boxes it read, presses the one control that reads as Verify or Continue, and goes on. A resumed request older than ten minutes may use one unique, enabled Send New Code or Resend Code button. The new request time is saved before that click, preventing a resend loop and excluding older mail. Sender display names and subjects cannot authorize an unrelated sending domain. The message's To address must match the frozen application email; another alias or missing recipient metadata is rejected. Rove requests recipient details using [Zoho's documented `includeto` option](https://www.zoho.com/mail/help/api/get-emails-list.html). The code is never logged, saved or posted; the thread gets one line. When no code arrives or the site refuses it, the run stops with a card and the step is the owner's. A page that says its code came by text message, a call or an authenticator app is never a code step: those stay manual.

A page's own loading screen (no words, no buttons, a spinner or progress mark over the window) is waited out for up to fifteen seconds and is never treated as a pop-up to close.

Requests for `blob:` and `data:` addresses are let through the public-HTTPS filter: they are content the page already holds, not a destination.

## Sign-in and account pages

A page with a password field is a sign-in page, or a registration page when it has two password fields or a create-account control. The daemon signs in only with an account stored for that host and creates an account only after the owner's `create account` reply. The password is generated locally and kept in the encrypted credential store. See [Application workflow](application-workflow.md#accounts).

## Evidence kept for debugging

All of it stays in the application's private folder under the state root:

- `observation.json` and `browser.png`, from the latest observation
- `failure.png`, the tab as it looked when an action raised an error
- `picker-<field>.json` and `picker-<field>.png`, for a picker that refused a value
- `dropdown-<field>.json`, how a dropdown selection was confirmed
- `form-before-submit.png`, the form just before the Submit click

The stop screenshot is also posted to the application's thread. See [Discord](discord.md#files-posted-in-the-thread).

## The synthetic fixture

`rove bench fixture` runs preparation, an owner-answer hold, resume and submission against a loopback employer. Its server independently checks the transmitted field values and exact synthetic resume bytes, rejects duplicate fields and duplicate submissions, and exposes confirmation only after acceptance. The worker must also record APPLIED. Qwen, Erga and Discord are stand-ins; this measures integration and browser mechanics, not model accuracy or live service delivery.

`rove smoke` is a separate, older path used for certification. It serves a synthetic form on localhost, fills ten known fields from a synthetic profile, uploads a fixture resume, and submits nothing. It does not accept real application URLs. See [Local runtime](local-runtime.md).

## Rules for changing this layer

Qwen3.8-27B is the only reasoning model. Do not add a cloud browser model, or a second required model, to make forms faster. Speed comes from fewer model calls and fewer browser round trips.

Keep Qwen out of mechanics. A known field with an approved value, a checkbox already in the right state, or a short wait for a known UI event is work for code.

Keep the daemon's actions narrow. The model and the Hermes agent must not receive Playwright, DevTools, JavaScript, shell or filesystem execution, even though the daemon uses those APIs itself.

Wait for a concrete state with a bound. The code's fixed pauses are the pacing pauses, short polls while the browser launches or a place picker loads, and the pause before retrying a blocked site.

Add a site-specific adapter only when repeated evidence justifies it, keep it small and versioned, and keep the generic path working. An adapter may recognise a site, read its widgets and verify a result. It must not bypass authentication, weaken the destination checks, invent an answer, or bypass the package and the submission checks.

A model saying a form was submitted proves nothing. Confirmation comes from an adapter reading evidence after the click.

Optimize one application before adding concurrency. One Qwen request at a time is the tested configuration.

When a page cannot be handled safely, stop for the owner.

## Coverage and remaining limits

A recognized board or a passing fixture does not establish complete support for every employer on it. As of the October 4, 2026 acceptance audit, the seven selected Oracle/Navy Federal applications have no confirmed submissions. The [visual benchmark](visual-benchmark.md) has no verified full-game completion.

| Board | Implemented path | Evidence and limit |
| --- | --- | --- |
| Greenhouse, Lever | Board-specific submission verification plus shared form filling | Browser fixtures cover fields and submission evidence. This audit does not establish broad live success rates. |
| Ashby, Workday, Oracle | Job-scope recognition plus generic form and submission handling | Synthetic coverage varies. The current live acceptance cases are Oracle; signup, session expiry and dependent location fields remain material cases. |
| Workable, Paylocity, JazzHR, BambooHR | Versioned board modules, enabled separately in local configuration | Fixtures cover each board's controls and positive/negative submission evidence. They are not live applications. |
| Indeed and other aggregators | Intake link resolution toward a verified employer application | No verified Indeed Easy Apply workflow. |
| Unrecognized employer forms | Explicit destination validation and generic fallback | Unfamiliar controls or uncertain destination/receipt stop the run. No claim of universal support. |

### Speed measurements and Jev

The [Jev browser demo](https://github.com/browser-use/jev-ultrafast) uses compact DOM observations, dynamic action choices, fewer protocol calls and a text generator only when it needs to type. Its reported seven-second flight search begins after the initial observation and excludes important application features such as frames, uploads and popup tabs. It is not a comparable job-application benchmark.

Rove already reads a form in a grouped observation and resolves known facts in code. Its October 4 live audit found a 395-second held application pass: 251 seconds of model calls and 138 seconds of browser operations. That was a failed/held pass, not a completed application. Resuming that form had incorrectly replaced the job description with form text, triggering a fresh fit review. The corrected resume reused the review in 0.03 seconds on the next observed pass; this is one phase measurement, not an end-to-end speedup claim.

Known-value dropdown failures now bypass answer drafting. Verified unchanged fields incur no extra per-field pacing delay. The worker still measures real stage times and call counts through `rove bench report`; the isolated `rove bench fixture` measures code and browser work with a model stand-in. Neither report substitutes for live outcome evidence.

### Recovery boundaries

| Case | Current behavior |
| --- | --- |
| Slow rendering, late CAPTCHA or upload parsing | Bounded waits observe actual page state; hidden response fields do not count as progress. |
| Expired application session | On the same job, try the site's exact Continue Working and Resume Application controls once each; never use Submit for recovery. |
| Shared calling code, duplicate city names, combined postal/city rows | Use approved country/state and exact components; require one observed matching option. |
| Unknown street address, county, legal or qualification fact | Ask for the missing fact. Model drafting cannot supply it. |
| Browser restart or dropped connection | Reattach eligible tabs and preserve holds; uncertain sends remain blocked from retry. |
| Lost response after Submit | Keep unknown-submission state until a receipt or explicit reconciliation settles it. |
| Duplicate job URLs or concurrent workers | Canonical job identity and transactional submission claims prevent another send. |
| Discord delivery outage | Preserve local state and retry recorded delivery; a channel message is not proof of employer receipt. |
| Optional unanswered question | Leave it blank where the site's validation permits; a required question remains unresolved. |

Qwen has no general browser execution tool for arbitrary unfamiliar page recovery. Assessments, unsupported widgets, unresolved visual challenges and missing facts can still interrupt the workflow. Passing the seven-job gate would validate those cases; it would not prove all job boards work.
