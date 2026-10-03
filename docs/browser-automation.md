# Browser automation

Rove fills application forms in a browser of its own: the Rove Browser, a copy of the owner's Google Chrome under its own name. Code observes the page, resolves fields from approved data, fills them, and reads the values back. Qwen is asked only about questions the approved data does not answer. It never drives the browser.

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

With `human_pacing` on (the default), the daemon pauses a random fraction of a second between fields, moves the pointer to a control before clicking it, types values of up to 80 characters key by key, and visits a site's front page before its first deep link. Tests turn pacing off.

## Where applicant data may be typed

Several checks run before any value is entered.

While the daemon drives a tab, every request the tab makes is checked: public HTTPS on port 443, to a host that resolves only to public addresses. Anything else is aborted. The check is attached for the length of one operation and removed afterward, so a tab the owner browses by hand is never stalled.

`follow` clicks only a control the last observation reported as an application-start link: Apply, Apply now, Start application and similar wording. The observation must still be current and the control's label unchanged. A link whose address is on another host is followed only when that host is on the applicant-tracking list.

`prepare` types nothing unless both of these hold:

- The form's host is on the applicant-tracking list in `ATS_HOSTS` in `src/rove/live_browser.py`. The list covers Greenhouse, Lever, Ashby, Workday, Oracle Cloud, iCIMS, SmartRecruiters, Eightfold and Workable, plus employer hosts added to it in code.
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

A field's label is its associated label, then `aria-label`, then `aria-labelledby`, then the nearest label within four ancestors that owns no other control, then the placeholder. Radio buttons that share a name are reported as one question with options.

A field counts as required when the input says so, or when its label carries a `required` class or ends in an asterisk.

The values of password, hidden and file inputs are never read. On a page with a password field or a field labeled social security, passport, bank account or verification code, all field values are dropped, the text is cut short, no screenshot is taken, and the page is flagged for the owner.

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
- A question about relocating, commuting or working onsite is answered Yes when the approved preferences say relocate and include onsite work, unless it names a location the owner excluded.
- A graduation question shown as a group of options takes the option that matches the approved graduation month.

A phone-type field defaults to Mobile, and the form card shows that as a default. Every other question is left for the owner or for Qwen. Code never guesses among options or legal wording.

## Filling and verifying

Each page is filled in one pass and then observed again.

Text fields are typed and read back. The value passes when the site kept it, changed only its whitespace, or reformatted a phone number with the same last ten digits. Any other difference stops the run with a card naming the field.

- A field that already holds a different value is left alone and becomes a question.
- A US phone is typed as ten national digits first, because sites with their own country selector reject a repeated code. If a step then rejects the phone, the international form is tried once.
- A text the site silently truncates is cut to what the field keeps, at a sentence boundary, and the form card says so.
- A native select is set only when exactly one option matches. Country options match under the common spellings of United States.
- A radio group or button group is set to the matching option and checked afterward. An option that is already selected is recorded and not toggled.
- A long-text field is filled only from an answer stored for the application: one the owner typed, a draft the owner approved, or a draft used under the `auto_use_drafts` policy.

A field that appears after filling becomes a question. It is never answered from a guess.

### The resume upload

Only a file field named for a resume or CV is filled, and only with the frozen `resume.pdf` in the application's folder. Its hash is checked before the upload, and the input is checked afterward to hold that one file. A required file field for anything else becomes a question, and an optional one is skipped.

### Dropdowns and place pickers

A field is treated as a picker when it is a combobox, when it has an autocomplete hint or a "start typing" placeholder, or when its label is a place label such as Location, City or "Where are you located".

For an ordinary picker the daemon opens the list and selects the option whose text equals the approved value, when there is exactly one. Typing into the search box does not count as a selection. The choice is confirmed from the widget: the selected option, the displayed value, or the accessibility announcement.

For a place picker the daemon types the approved city and waits for suggestions, polling for up to six seconds because these widgets geocode after a pause. It picks the suggestion that equals the approved "City, State" or "City, State, Country", or else the first one that starts with the city, preferring one that also names the state. When the suggestion list has no ARIA roles, it clicks the shortest visible suggestion that contains the city, or takes the first suggestion with the keyboard when it finds none. The pick is accepted only when the text committed to the input names the approved city.

A picker that refuses the value becomes a question. The daemon keeps private evidence for the next fix: what it typed, the options the picker listed, what the input kept, and a screenshot.

When no value is known, the daemon opens the list once to read its options so Qwen and the owner see the real choices.

### Multi-page forms

When a page is complete, has no final Submit control and shows a Next or Continue control, the daemon clicks it, waits for the next step's fields, and fills again. It does this for at most four pages.

If the click leaves the same page with a validation message about the phone, the other phone format is tried once. If the form never reaches a step with exactly one final control, the run ends with "Final step not reached" and the site's message when there is one. A form in that state is never called ready.

### Required fields the site names

When a site rejects a submission and names a "required field", the label is saved for that application. The next preparation treats a field with that label as required, even if the page does not mark it.

## The package

Preparation ends by writing a package: the page URL, the profile hash, the resume hash, every filled field with its value and source, the open questions, the full form state, the final control and the pages visited. The package is hashed. It is ready for review only when no question is open and the page has exactly one final control.

Sending checks the live page against this package field by field. See [Application workflow](application-workflow.md#sending).

## The submit guard

Every page in the Rove Browser gets a script that blocks form submission events unless a flag on the document is set. Submission code sets the flag for one click and removes it afterward. The guard stops accidental submits during preparation, including the Enter key in a field. It is not a network-level guarantee against a page script that posts on its own.

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

## Not built yet

- The browser layer has no timing or round-trip instrumentation. Measurements and `rove bench` are in progress.
- Qwen is not used to recover from an unfamiliar page state. Such a state is a stop.
- Site-specific code is limited to the job-scope rules for Greenhouse, Lever and Ashby, the Greenhouse and Lever submission adapters, and the handling of React Select dropdowns.
