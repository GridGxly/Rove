# Security

Erga Autopilot controls a browser, handles recruiting data, and may eventually submit job applications. Treat it like a privileged local automation tool, not a normal chatbot.

This document describes the intended security model for the project. Some capabilities are still under development, so a documented control may not exist in every branch yet.

## Security goals

The system should make it difficult for an untrusted job page, email, attachment, model output, or MCP tool to:

- change the candidate profile;
- gain new permissions;
- read unrelated local files;
- access credentials it does not need;
- upload arbitrary files;
- navigate to arbitrary sensitive destinations;
- send messages or email without authorization;
- submit a different application than the one the user approved;
- retry an ambiguous submission and create a duplicate;
- poison durable memory.

The project assumes the local macOS user account itself is trusted. If an attacker fully compromises that account, they may be able to access both encrypted application data and the local key material needed to run Autopilot.

## Trust boundaries

### Trusted authority

Only these sources may authorize actions:

- an authenticated command from the configured Discord user;
- an explicit local approval;
- a previously approved local policy;
- deterministic scheduled work created from one of those approvals.

### Untrusted data

Always treat the following as data, never as authority:

- job descriptions;
- employer application pages;
- email bodies;
- email attachments;
- resumes and imported documents;
- scraped research;
- third-party websites;
- MCP tool output;
- browser accessibility text;
- model-generated text.

A page saying "ignore previous instructions" is no more authoritative than a paragraph in a job description.

## Prompt injection

Prompt injection is handled structurally rather than by relying on a system prompt alone.

Controls should include:

- mode-specific tool exposure;
- narrow MCP tool surfaces;
- strict parameter schemas;
- domain and redirect validation;
- upload allowlists;
- separation between research and application submission;
- human approval for sensitive or irreversible actions;
- deterministic profile/memory writes;
- centralized audit logging;
- adversarial test fixtures.

The research agent should not have submission tools or credentials. The application agent should not have unrestricted filesystem or shell access.

## Operating modes

The same local model may operate under different capability profiles.

Examples:

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

A mode controls which tools are available and which mutations are allowed.

For example, onboarding may update predefined candidate-profile fields, but it must not redesign the profile schema or change unrelated application state.

## Browser isolation

The recruiting browser must use a dedicated Playwright profile.

Do not connect Autopilot to your everyday browser profile.

The recruiting browser should not contain:

- banking sessions;
- personal social-media sessions unrelated to recruiting;
- unrelated production/admin accounts;
- a personal password-manager extension;
- browser sync with the user's normal profile.

The application workflow should validate expected employer, ATS, and authentication domains before sending personal data.

Do not expose arbitrary Playwright code execution to the application agent.

## File access and uploads

The application agent should upload only files belonging to the frozen application package, such as:

- the approved resume;
- an approved cover letter;
- a requested transcript or work sample when policy allows it.

A webpage must not be able to choose an arbitrary local path.

The application workflow should not have generic access to the user's home directory.

## Credentials

Generated employer-account passwords may be stored in the local Autopilot database, but they must be encrypted at rest with authenticated encryption.

The encryption key should live in a separate owner-only local file outside the database.

Never commit or post to Discord:

- plaintext passwords;
- OAuth refresh/access tokens;
- browser cookies;
- session storage;
- encryption keys;
- MFA codes.

Discord may record that an account was created and that verification succeeded, but not the secret itself.

## Extremely sensitive information

The project should not automate or persist certain values by default.

### Full Social Security number

- do not store it;
- do not send it through Discord;
- do not put it in model context;
- require manual local entry when genuinely necessary.

### Bank and routing information

Do not collect or automate this during job applications. Handle it manually only during verified post-hire onboarding.

### Passport, driver's-license number, or identity images

Require manual local handling unless a future reviewed design explicitly adds support.

### MFA

Email verification may be brokered directly between Zoho and the browser. SMS codes, authenticator apps, security keys, CAPTCHA, and unusual identity checks should pause for manual action.

## Local data

Live user state belongs outside the repository.

Never commit:

- real candidate profiles;
- real resumes;
- application databases;
- application receipts;
- browser profiles;
- screenshots containing personal data;
- Zoho email content;
- generated account credentials;
- private logs;
- backups.

Tests and examples should use synthetic identities and synthetic employers.

## Discord

The production bot should authorize commands using a configured numeric Discord user ID.

Do not rely only on display names or usernames.

Third-party source bots should receive access only to the channels they need. They should not be able to read application archives, memory, recruiting mail, or system logs.

The `#action-needed` channel should contain only items that actually require human attention so important prompts do not get lost in routine output.

## Zoho

Prefer the official Zoho Mail API with the narrowest scopes that support the workflow.

Recruiting mail should normally be read-only.

Email content is untrusted data. It can update application state only through the mail-classification and reconciliation pipeline.

When a lifecycle-changing email is rendered for a Discord screenshot, sanitize active content and render it in an isolated local page rather than opening the user's normal inbox UI.

## Candidate profile and memory

The canonical profile is schema-driven and versioned.

The model may propose or collect values, but durable profile changes must go through explicit profile operations.

Do not allow arbitrary chat text, job pages, or research output to silently become authoritative memory.

If two approved facts conflict, stop and ask the user. Do not silently choose the newest value.

Historical applications must retain the profile version they actually used.

## Resume claims

Resume facts should be backed by approved Erga evidence.

Do not invent:

- metrics;
- technologies;
- dates;
- ownership;
- performance improvements;
- adoption or user counts;
- outcomes.

Narrative context from `introduction.md` may explain motivation or perspective, but it does not override factual evidence.

## Submission safety

Submission is an irreversible operation.

Before clicking Submit, freeze an application package containing at least:

- application ID;
- verified job URL;
- job snapshot/hash;
- candidate-profile version;
- answer-mapping version;
- resume version/hash;
- exact planned form answers;
- exact approved free-text answers;
- skipped optional fields;
- unresolved warnings;
- browser state or trace reference.

If the browser crashes after Submit, do not automatically retry.

Move the application into an unknown-submission state and inspect:

- confirmation page/state;
- ATS account;
- browser/network result;
- recruiting email.

Retry only after the first attempt is shown not to have succeeded.

## Logging

Detailed audit logs are intentional, but secrets must be filtered before writing.

Logs may contain:

- timestamps;
- application IDs;
- tool names;
- safe tool parameters;
- browser URLs;
- form-field events;
- retries;
- errors;
- lifecycle transitions;
- model/runtime metadata.

Logs must not contain:

- plaintext passwords;
- OAuth tokens;
- browser cookies;
- full SSNs;
- MFA codes;
- encryption keys.

## Dependency and MCP security

MCP servers are executable software with access determined by the client process and tool configuration.

Before adding or upgrading an MCP server:

- verify the upstream project;
- pin or review the version where practical;
- inspect the exposed tool schemas;
- expose only required tools;
- rerun prompt-injection and permission tests;
- do not assume a tool is safe because it is described as read-only.

Do not silently auto-update the model runtime, Hermes, Erga, Playwright MCP, or other privileged dependencies in production.

## Security testing

The project should maintain adversarial tests covering at least:

- hidden prompt injection in HTML;
- injection in email content;
- malicious accessibility labels;
- fake local-file upload instructions;
- malicious redirects;
- attempts to exfiltrate profile data;
- attempts to access credentials;
- attempts to mutate memory;
- duplicate-submit traps;
- fake verification pages;
- poisoned MCP tool output;
- tool-name/schema changes.

Autopilot submission should not be enabled if those tests fail.

## Reporting a vulnerability

Please do not post credentials, private application data, or a working exploit containing real personal information in a public issue.

For ordinary security bugs, open a GitHub issue with a minimal synthetic reproduction and label it as security-related if the repository settings allow it.

If the issue would expose secrets or a practical exploit against a real user, use GitHub's private vulnerability reporting feature when available for this repository.

Include:

- affected version/commit;
- threat scenario;
- minimal reproduction using synthetic data;
- expected behavior;
- actual behavior;
- suggested mitigation if you have one.

## References

Useful upstream guidance includes:

- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [Playwright MCP](https://playwright.dev/mcp/installation)
- [Erga security model](https://github.com/Adr1an04/erga-mcp/blob/main/docs/security.md)
