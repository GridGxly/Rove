# Security

Erga Autopilot controls a browser, handles recruiting data, and may eventually submit job applications. Treat it like privileged local automation, not a normal chatbot.

Some controls described here are part of the target design and may not exist on every branch yet. When this file and the code disagree, the code is what is actually running.

## What should never happen

An untrusted job page, email, attachment, model output, or tool should not be able to:

- change the candidate profile
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

## Browser isolation

The recruiting browser must use its own Playwright profile.

Do not connect Autopilot to an everyday browser profile.

The recruiting profile should not contain:

- banking sessions
- unrelated social-media sessions
- unrelated production or admin accounts
- a personal password-manager extension
- browser sync with the normal profile

Before personal data is entered, the workflow should verify that the browser is still on the expected employer, ATS, or authentication destination.

Do not expose arbitrary Playwright code execution to the application agent.

## File access and uploads

The application agent should upload only files already included in the frozen application package, such as:

- the approved resume
- an approved cover letter
- a transcript or work sample when policy allows it

A webpage must not be able to choose an arbitrary local path.

The application workflow should not have generic access to the user's home directory.

## Credentials

Employer-account passwords may be stored locally, but they must be encrypted at rest with authenticated encryption.

The encryption key should live in a separate owner-only local file outside the database.

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
- require manual local entry when genuinely necessary

### Bank and routing information

Do not collect or automate this during job applications. Handle it manually only during verified post-hire onboarding.

### Passport, driver's-license number, or identity images

Keep these manual unless a future reviewed design explicitly adds support.

### MFA

Email verification may be passed through a narrow broker. SMS codes, authenticator apps, security keys, CAPTCHA, and unusual identity checks should pause for manual action.

## Public repository rules

Treat anything committed, pushed, placed in a pull request, printed in CI logs, or attached to a GitHub issue as public.

Real runtime state belongs outside the repository.

Never commit:

- real candidate profiles
- real resumes or cover letters
- live application databases
- application receipts
- browser profiles or cookies
- screenshots containing personal data
- recruiting email
- generated account credentials
- OAuth tokens
- private logs
- backups

Documentation and tests must use synthetic people, companies, jobs, IDs, email, and credentials.

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

## Zoho

Use the official Zoho Mail API with the narrowest scopes that support the workflow.

Recruiting mail should normally be read-only.

Email content is untrusted input. It can affect application state only through classification and reconciliation code.

When an email is rendered for a Discord screenshot, sanitize active content and render it in an isolated local page rather than opening the user's normal inbox UI.

## Candidate profile and memory

The canonical profile is schema-driven and versioned.

The model may collect or propose values, but durable profile changes must go through explicit profile operations.

Do not let arbitrary chat text, job pages, email, or research output silently become authoritative memory.

If two approved facts conflict, stop and ask the user. Do not silently choose one.

Historical applications must keep the profile version they actually used.

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

`introduction.md` may add motivation or perspective, but it does not override factual evidence.

## Submission safety

Submission is irreversible.

Before clicking Submit, freeze an application package containing at least:

- application ID
- verified job URL
- job snapshot and hash
- candidate-profile version
- answer-mapping version
- resume version and hash
- exact planned form answers
- exact approved free-text answers
- skipped optional fields
- unresolved warnings
- browser state or trace reference

If the browser crashes after Submit, do not automatically retry.

Move the application into an unknown-submission state and inspect confirmation state, the ATS account, browser/network result, and recruiting mail.

Retry only after the first attempt is shown not to have succeeded.

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

Logs must not contain:

- plaintext passwords
- OAuth tokens
- browser cookies
- full SSNs
- MFA codes
- encryption keys

## Dependency and MCP security

MCP servers are executable software. Their real permissions come from the process that starts them and the tools exposed to the model.

Before adding or upgrading a privileged MCP server:

- verify the upstream project
- pin or review the version where practical
- inspect the exposed tool schemas
- expose only the tools the workflow needs
- rerun prompt-injection and permission tests
- do not assume a tool is safe because its description says read-only

Do not silently auto-update the model runtime, Hermes, Erga, Playwright MCP, or other privileged dependencies in production.

## Security tests

The project should keep synthetic adversarial tests for at least:

- hidden prompt injection in HTML
- injection in email content
- malicious accessibility labels
- fake local-file upload instructions
- malicious redirects
- attempts to exfiltrate candidate data
- attempts to access credentials
- attempts to mutate memory
- duplicate-submit traps
- fake verification pages
- poisoned MCP tool output
- tool-name or schema changes

Tests should prove that forbidden actions did not happen, not just that the model printed a refusal.

Restricted autopilot should not be enabled while those tests fail.

## Reporting a vulnerability

Do not post credentials, private application data, or a working exploit containing real personal information in a public issue.

Use a synthetic reproduction for ordinary security bugs. If a report would expose a real secret or practical exploit against a real user, use private vulnerability reporting when it is available for the repository.

## References

- [Prompt injection](docs/prompt-injection.md)
- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [Playwright MCP](https://playwright.dev/mcp/installation)
- [Erga security model](https://github.com/Adr1an04/erga-mcp/blob/main/docs/security.md)
