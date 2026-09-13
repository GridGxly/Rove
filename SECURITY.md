# Security

Erga Autopilot controls a browser, handles recruiting data, and may eventually submit job applications. Treat it like a privileged local automation tool, not a normal chatbot.

Some controls described here are part of the target design and may not exist on every branch yet. If the code and this file disagree, the code is what is actually running.

## What this project is trying to prevent

An untrusted job page, email, attachment, model output, or MCP tool should not be able to:

- change the candidate profile
- give itself new permissions
- read unrelated local files
- get credentials it does not need
- upload arbitrary files
- send the browser to arbitrary sensitive destinations
- send email or messages without authorization
- submit a different application from the one you approved
- retry an ambiguous submission and create a duplicate
- poison durable memory

The project assumes the local macOS user account itself is trusted. If that account is fully compromised, an attacker may be able to reach both encrypted application data and the local key material needed to run Autopilot.

## What can authorize an action

Only these sources may authorize work:

- an authenticated command from the configured Discord user
- an explicit local approval
- a local policy you already approved
- scheduled work created from one of those approvals

Everything else is input data.

That includes:

- job descriptions
- employer application pages
- email bodies
- email attachments
- resumes and imported documents
- scraped research
- third-party websites
- MCP tool output
- browser accessibility text
- model-generated text

A webpage that says "ignore previous instructions" has no more authority than any other sentence on that page.

## Prompt injection

Prompt injection is handled with permissions and boundaries, not by hoping a system prompt wins an argument with a webpage.

The design uses:

- different tool sets for different operating modes
- narrow MCP tool surfaces
- strict parameter schemas
- domain and redirect checks
- upload allowlists
- separate research and submission contexts
- human approval for sensitive or irreversible actions
- deterministic profile and memory writes
- central audit logs
- adversarial test cases

The research agent should not have submission tools or credentials. The application agent should not have unrestricted filesystem or shell access.

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

For example, onboarding may update predefined candidate-profile fields, but it must not redesign the profile schema or change unrelated application state.

## Browser isolation

The recruiting browser must use its own Playwright profile.

Do not connect Autopilot to your everyday browser profile.

The recruiting profile should not contain:

- banking sessions
- unrelated social-media sessions
- production or admin accounts that have nothing to do with recruiting
- a personal password-manager extension
- browser sync with your normal profile

Before personal data is entered, the application workflow should verify that the browser is still on the expected employer, ATS, or authentication domain.

Do not expose arbitrary Playwright code execution to the application agent.

## File access and uploads

The application agent should only upload files that belong to the frozen application package, such as:

- the approved resume
- an approved cover letter
- a transcript or work sample when policy allows it

A webpage must not be able to choose an arbitrary local path.

The application workflow should not have generic access to the user's home directory.

## Credentials

Employer-account passwords may be stored in the local Autopilot database, but they must be encrypted at rest with authenticated encryption.

The encryption key should live in a separate owner-only local file outside the database.

Never commit or post to Discord:

- plaintext passwords
- OAuth refresh or access tokens
- browser cookies
- session storage
- encryption keys
- MFA codes

Discord may say that an account was created and verified. It should not contain the secret itself.

## Extremely sensitive information

Some values should stay out of normal automation entirely.

### Full Social Security number

- never store it
- never send it through Discord
- never put it in model context
- require manual local entry when it is genuinely necessary

### Bank and routing information

Do not collect or automate this during job applications. Handle it manually only during verified post-hire onboarding.

### Passport, driver's-license number, or identity images

Keep these manual unless a future reviewed design explicitly adds support.

### MFA

Email verification may be passed directly between Zoho and the browser. SMS codes, authenticator apps, security keys, CAPTCHA, and unusual identity checks should pause for manual action.

## Local data

Live user state belongs outside the repository.

Never commit:

- real candidate profiles
- real resumes
- application databases
- application receipts
- browser profiles
- screenshots containing personal data
- Zoho email content
- generated account credentials
- private logs
- backups

Tests and examples should use synthetic people and synthetic employers.

## Discord

The production bot should authorize commands with a configured numeric Discord user ID.

Do not rely only on display names or usernames.

Third-party source bots should only see the channels they need. They should not be able to read application archives, memory, recruiting mail, or system logs.

Keep `#action-needed` for items that really need human attention. If routine logs end up there, important prompts will get buried.

## Zoho

Use the official Zoho Mail API with the narrowest scopes that support the workflow.

Recruiting mail should normally be read-only.

Email content is untrusted input. It can affect application state only through the mail-classification and reconciliation code.

When an email triggers a lifecycle screenshot for Discord, sanitize active content and render it in an isolated local page instead of opening the user's normal inbox UI.

## Candidate profile and memory

The canonical profile is schema-driven and versioned.

The model may collect or propose values, but durable profile changes must go through explicit profile operations.

Do not let arbitrary chat text, job pages, or research output silently become authoritative memory.

If two approved facts conflict, stop and ask the user. Do not silently choose the newest value.

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

Move the application into an unknown-submission state and inspect:

- confirmation page or state
- ATS account
- browser or network result
- recruiting email

Retry only after the first attempt is shown not to have succeeded.

## Logging

Detailed logs are intentional, but secrets must be filtered before anything is written.

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

MCP servers are executable software. Their real permissions come from the process that starts them and the tools you expose.

Before adding or upgrading an MCP server:

- verify the upstream project
- pin or review the version where practical
- inspect the exposed tool schemas
- expose only the tools the workflow needs
- rerun prompt-injection and permission tests
- do not assume a tool is safe just because its description says read-only

Do not silently auto-update the model runtime, Hermes, Erga, Playwright MCP, or other privileged dependencies in production.

## Security tests

The project should keep adversarial tests for at least:

- hidden prompt injection in HTML
- injection in email content
- malicious accessibility labels
- fake local-file upload instructions
- malicious redirects
- attempts to exfiltrate profile data
- attempts to access credentials
- attempts to mutate memory
- duplicate-submit traps
- fake verification pages
- poisoned MCP tool output
- tool-name or schema changes

Autopilot submission should not be enabled if those tests fail.

## Reporting a vulnerability

Do not post credentials, private application data, or a working exploit containing real personal information in a public issue.

For ordinary security bugs, open a GitHub issue with a minimal synthetic reproduction and mark it as security-related if the repository settings support that.

If the report would expose a secret or a practical exploit against a real user, use GitHub's private vulnerability reporting when it is available for this repository.

Please include:

- affected version or commit
- threat scenario
- minimal reproduction using synthetic data
- expected behavior
- actual behavior
- suggested mitigation if you have one

## References

- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [Playwright MCP](https://playwright.dev/mcp/installation)
- [Erga security model](https://github.com/Adr1an04/erga-mcp/blob/main/docs/security.md)
