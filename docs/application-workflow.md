# Application workflow

The local workflow joins Keryx discovery and owner-pasted links in one durable queue.
It is under active verification. Preparation is implemented; real final submission,
mail reconciliation, and unattended application completion remain unavailable.

## Intake and visibility

`autopilot feed tick` checks the fixed Keryx source. An unchanged revision does not
redownload the snapshot. Changed matching internships enter a deduplicated notification
outbox and application queue. `autopilot feed seed` publishes the first 25 matches in
small grouped messages. All recruiting terms are included when the approved profile
selects any term. The installation maps the logical source channel to an existing
Discord channel in private `config/feed.json`.

`start_job_application(url)` accepts owner-requested public HTTPS links independently
of the feed. A Jobright identifier is never treated as a Keryx identifier. Verified
source-to-employer aliases live in SQLite; unknown redirects or account requirements
need review. Queue uniqueness prevents a second application for the same canonical URL.

The browser is a visible, persistent, dedicated Chromium profile. Its control interface
is a private Unix socket, not a network listener. The model cannot select arbitrary
files, execute JavaScript, use a shell, or choose arbitrary click selectors.

## Preparation and questions

The worker creates an application forum post before preparation. It opens the posting,
follows bounded observed application-start controls, uses Erga's supported job intake,
and fills deterministic approved fields as a batch. Exact profile and resume hashes,
field values, provenance, unresolved questions, and screenshots remain private artifacts.
The forum mirrors meaningful events. A failed tailored resume is never reported as
validated: an approved base PDF can be preserved with an explicit review warning.

Unfamiliar questions go to **Qwen through Hermes**. The reasoning step receives bounded
approved context and observed question keys and exposes no mutation tools. Its JSON
output is schema-validated and saved as an unapproved proposal. It cannot approve facts,
change the profile, upload a file, or submit. Unknown personal facts return to the owner.
Known profile fields are handled by code; Qwen is not called for every keystroke.

The worker accepts `answer APPLICATION_ID FIELD_KEY = value` and `resume APPLICATION_ID`
from the configured numeric owner in the permitted control/action/forum channels.
Answers are bound to observed fields. Secrets and identity checks remain manual. A
required field cannot be skipped. The owner can inspect and take over the visible page.

## Local configuration

Private `config/workflow.json` maps `guild_id`, `forum_channel_id`, `control_channel_id`,
`action_channel_id`, `source_channel_id`, `shortlist_channel_id`, and lifecycle `tags`.
It also supplies the installed `hermes_python` and an explicit `enabled` flag.
Credentials are read from private setup/Hermes configuration and never returned by MCP.

```sh
uv run autopilot install-services
uv run autopilot feed seed
uv run autopilot workflow status
uv run autopilot workflow enqueue --url https://jobs.example.com/internship
uv run autopilot workflow tick
uv run autopilot browser status
```

A paused application retains the visible session. Delivery records are written before
Discord mutations. An ambiguous forum creation or message delivery is held for
reconciliation instead of automatically creating duplicate records. The current owner
command bootstrap starts at the current channel cursor; it does not replay old approvals.

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

Current limits include incomplete custom widgets and multi-page ATS coverage, account
creation/manual identity steps, unverified final submission, and optional mail tracking.
Tests and a filled form demonstrate preparation only. The full project must not be
reported complete until the owner-command, resume, browser, submission receipt, forum,
and lifecycle paths have passed end-to-end verification.
