# Security

Rove controls a browser, handles recruiting data, keeps a private long-term memory vault, and may eventually submit job applications. Treat it like privileged local automation, not a normal chatbot.

Some controls described here are part of the target design and may not exist on every branch yet. When this file and the code disagree, the code is what is actually running.

## What should never happen

An untrusted job page, email, attachment, model output, vault research note, QMD result, or tool should not be able to:

- change the approved candidate profile
- give itself new permissions
- read unrelated local files
- access credentials it does not need
- upload arbitrary files
- navigate to unrelated sensitive destinations
- send messages or email without authorization
- submit a different application from the one the user approved
- retry an ambiguous submission and create a duplicate
- poison durable memory

The project assumes the local macOS user account itself is trusted. A full compromise of that account is outside this threat model.

## Authority

Only these sources may authorize privileged work:

- an authenticated command from the configured user
- an explicit local approval
- a previously approved local policy
- deterministic scheduled work created from one of those approvals

Everything else is input data, including:

- job descriptions
- employer application pages
- email bodies and attachments
- resumes and imported documents
- scraped research
- third-party websites
- browser accessibility text
- non-authoritative Obsidian research notes
- QMD retrieval results
- MCP tool output
- model-generated text

A webpage that says "ignore previous instructions" has no authority by itself.

Read [docs/prompt-injection.md](docs/prompt-injection.md) for the detailed prompt-injection model.

## Operating modes

The same local model can run under different permission sets:

```text
ONBOARDING
JOB_REVIEW
RESEARCH
RESUME
APPLICATION_PREPARE
APPLICATION_SUBMIT
MAIL_REVIEW
MANUAL_TAKEOVER
```

A mode changes what the agent can read or change.

Research should not have submission tools or credentials. Application preparation should not have final submission capability. Submission should not expose a generic shell or unrestricted filesystem.

Memory/vault permissions should also differ by mode. Research may write non-authoritative research notes where allowed, but it should not directly mutate approved profile or policy notes.

## Browser isolation

The recruiting browser must use its own dedicated Playwright/Chromium profile.

Do not connect Rove to an everyday browser profile.

The recruiting profile should not contain:

- banking sessions
- unrelated social-media sessions
- unrelated production or admin accounts
- a personal password-manager extension
- browser sync with the normal profile

Before personal data is entered, the workflow should verify that the browser is still on the expected employer, ATS, or authentication destination.

Do not expose arbitrary Playwright, CDP, or JavaScript execution to the application agent. Trusted browser-runtime code may use low-level browser APIs internally, but the model-facing tool surface must stay narrow and policy-aware.

## File access and uploads

The application agent should upload only files already included in the frozen application package, such as:

- the approved resume
- an approved cover letter
- a transcript or work sample when policy allows it

A webpage must not be able to choose an arbitrary local path.

The application workflow should not have generic access to the user's home directory.

Access to the private Obsidian vault is not permission to browse unrelated home-directory files.

## Credentials

Employer-account passwords may be stored locally, but they must be encrypted at rest with authenticated encryption.

The encryption key should live in a separate owner-only local file outside the database.

The current implementation does this with Fernet (AES-CBC with HMAC): ciphertext in `credentials/store.enc` and the key in `credentials/key` under the private state root, both mode 0600. An account is created only after the owner replies `account ... create` for that application; the daemon fills the application email, a generated password, the terms checkbox and known name fields, and records the host and username without the secret. Only the browser daemon reads the store, to complete a sign-in form on the same host.

The recruiting browser is a Chrome instance the daemon launches with a localhost-only DevTools port. Anything running as the local user could connect to that port; that account is trusted in this threat model.

Never put credentials in the Obsidian vault, normal Markdown notes, Discord, or model memory.

Never commit or post to Discord:

- plaintext passwords
- OAuth refresh or access tokens
- browser cookies
- session storage
- encryption keys
- MFA codes

Discord may record that an account was created and verified. It should not contain the secret itself.

## Data that stays manual

Some values should stay out of normal automation entirely.

### Full Social Security number

- never store it
- never send it through Discord
- never put it in model context
- never save it in the Obsidian vault
- require manual local entry when genuinely necessary

### Bank and routing information

Do not collect or automate this during job applications. Handle it manually only during verified post-hire onboarding.

### Passport, driver's-license number, or identity images

Keep these manual unless a future reviewed design explicitly adds support.

### MFA

Email verification may be passed through a narrow broker. SMS codes, authenticator apps, security keys, CAPTCHA, and unusual identity checks should pause for manual action.

## Obsidian vault boundaries

The private Obsidian vault is long-term semantic memory, not a generic writable scratchpad with equal trust everywhere.

Different areas have different authority.

Examples:

- validated `Profile/` and approved policy/story notes can become authoritative after schema validation and approval
- `Research/` contains useful but non-authoritative material
- company/application notes may be durable context without becoming candidate facts
- QMD search results are retrieval output, not authority

Canonical profile writes should go through explicit profile/memory operations rather than arbitrary free-form note edits by the application or research agent.

A human may edit the vault directly. Before manually edited profile facts are used for an application, validate the schema and check for contradictions.

If validation fails, stop. Do not silently repair or guess an applicant fact.

## Profile snapshots

The live vault changes over time. A submitted application must not change retroactively when the user edits a note later.

Before an application is allowed to submit, freeze the exact approved profile version/snapshot used by that application and record a stable hash/reference in transactional state.

Submission should use the frozen snapshot, not reread mutable profile notes in the middle of the final application flow.

## QMD and retrieval

QMD is a local index over notes/documents. Treat the index as derived state.

A result returned by QMD may be relevant, stale, non-authoritative, or from a research note. The caller must still inspect source/provenance and apply normal profile/evidence rules.

If the index is lost or suspected stale, rebuild it from the vault rather than treating the index as the only copy of memory.

QMD helper models and indexes are local runtime data and should not be committed.

## Public repository rules

Treat anything committed, pushed, placed in a pull request, printed in CI logs, or attached to a GitHub issue as public.

Real runtime state belongs outside the repository.

Never commit:

- real candidate profiles
- a real user's Obsidian vault or vault export
- real resumes or cover letters
- live application databases
- application receipts
- browser profiles or cookies
- QMD indexes containing private content
- screenshots containing personal data
- recruiting email
- generated account credentials
- OAuth tokens
- private logs
- backups

Documentation and tests must use synthetic people, companies, jobs, IDs, email, vault notes, and credentials.

The author's name may appear where attribution belongs, such as the README and license. Applicant examples should remain synthetic.

## Git and secret scanning

`.gitignore` is a backup layer, not the main security boundary.

Before a commit or push, inspect staged changes and run the repository's configured secret checks. Do not bypass a secret-scanning or push-protection warning just to make a push succeed.

CI should scan committed changes for likely secrets. Before making the repository public, scan the reachable Git history and active branches as well as the current tree.

If a real secret is ever committed, revoke or rotate it first. Removing the file from the latest commit does not make the old value safe.

## Discord

The production bot should authorize commands with a configured numeric Discord user ID.

Do not rely only on display names or usernames.

Real guild, channel, user, and role IDs belong in local configuration, not source code.

Third-party source bots should only see the channels they need. They should not be able to read application archives, memory, recruiting mail, or system logs.

Keep `#action-needed` for items that actually require human attention so important prompts do not get buried.

`#memory` is an interface to controlled local memory operations. Discord messages themselves should not silently become authoritative vault/profile facts.

## Zoho

Use the official Zoho Mail API with the narrowest scopes that support the workflow.

Recruiting mail should normally be read-only.

Email content is untrusted input. It can affect application state only through classification and reconciliation code.

When an email is rendered for a Discord screenshot, sanitize active content and render it in an isolated local page rather than opening the user's normal inbox UI.

Email content may be summarized into notes where policy allows, but it must not directly mutate the approved candidate profile.

## Candidate profile and memory

The canonical profile is schema-driven, validated, and versioned even though its human-readable representation lives in the private semantic memory layer.

The model may collect or propose values, but durable authoritative profile changes must go through explicit profile operations.

Do not let arbitrary chat text, job pages, email, research output, or QMD retrieval silently become authoritative memory.

If two approved facts conflict, stop and ask the user. Do not silently choose one.

Historical applications must keep the exact approved profile snapshot/hash they actually used.

## Resume claims

Resume facts should come from approved Erga evidence.

Do not invent:

- metrics
- technologies
- dates
- ownership
- performance improvements
- adoption or user counts
- outcomes

Narrative/story notes may add motivation or perspective, but they do not override factual evidence.

## Submission safety

Submission is irreversible.

Before clicking Submit, freeze an application package containing at least:

- application ID
- verified job URL
- job snapshot and hash
- approved profile snapshot/version and hash
- answer-mapping version or references
- resume version and hash
- exact planned form answers
- exact approved free-text answers
- skipped optional fields
- unresolved warnings
- browser state or trace reference

If the browser crashes after Submit, do not automatically retry.

Move the application into an unknown-submission state and inspect confirmation state, the ATS account, browser/network result, and recruiting mail.

Retry only after the first attempt is shown not to have succeeded.

The current implementation records the attempt in SQLite before the click, accepts only an authenticated owner `submit` command for the exact package hash, and marks `APPLIED` only when a versioned ATS adapter sees its full confirmation contract. The recruiting browser installs a submit-event guard that blocks ordinary form submission during preparation and is armed for one approved click; it is a safeguard against accidental submits, not a network-level guarantee against page scripts. Owners resolve an unknown attempt with `reconcile`; code never infers the outcome.

## Logging

Detailed audit logs are useful, but secrets must be filtered before anything is written.

Logs may contain:

- timestamps
- application IDs
- tool names
- safe tool parameters
- browser URLs
- form-field events
- retries
- errors
- lifecycle transitions
- model and runtime metadata
- safe vault note IDs/paths when needed for provenance

Logs must not contain:

- plaintext passwords
- OAuth tokens
- browser cookies
- full SSNs
- MFA codes
- encryption keys
- private note bodies unless the specific log is an approved private artifact

## Dependency and MCP security

MCP servers and local skills are executable or privileged software. Their real permissions come from the process that starts them and the tools/files they can access.

Before adding or upgrading a privileged MCP server, memory skill, or local retrieval component:

- verify the upstream project
- pin or review the version where practical
- inspect exposed tool schemas or file permissions
- expose only the capabilities the workflow needs
- rerun prompt-injection and permission tests
- do not assume something is safe because its description says read-only

Do not silently auto-update the model runtime, Hermes, Erga, Playwright/Chromium browser runtime, Playwright MCP when installed, QMD, or other privileged dependencies in production.

## Security tests

The project should keep synthetic adversarial tests for at least:

- hidden prompt injection in HTML
- injection in email content
- malicious accessibility labels
- fake local-file upload instructions
- malicious redirects
- attempts to exfiltrate candidate data
- attempts to access credentials
- attempts to mutate canonical vault/profile memory
- poisoned research notes returned by QMD
- attempts to promote research into approved profile state
- duplicate-submit traps
- fake verification pages
- poisoned MCP tool output
- tool-name or schema changes

Tests should prove that forbidden actions did not happen, not just that the model printed a refusal.

Restricted autopilot should not be enabled while those tests fail.

## Reporting a vulnerability

Do not post credentials, private application data, vault contents, or a working exploit containing real personal information in a public issue.

Use a synthetic reproduction for ordinary security bugs. If a report would expose a real secret or practical exploit against a real user, use private vulnerability reporting when it is available for the repository.

## References

- [Prompt injection](docs/prompt-injection.md)
- [Memory and storage](docs/memory-and-storage.md)
- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [Browser automation](docs/browser-automation.md)
- [Playwright](https://playwright.dev/)
- [Erga security model](https://github.com/Adr1an04/erga-mcp/blob/main/docs/security.md)
