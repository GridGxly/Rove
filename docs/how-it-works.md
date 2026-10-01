# How Rove works

Rove runs on one Mac for one person. Four background services do the work, a local model handles the parts that need judgment, and Discord shows the owner what happened and takes replies.

## System map

```text
Owner's phone
   │  replies, pasted links
   ▼
Discord ◄──── cards, thread entries, files
   │
   ├── Hermes gateway ── Qwen3.8-27B on localhost ── Rove MCP tools (read and queue only)
   │
   └── launchd services
         ├── feed       every 15 minutes   Keryx job list → queue → jobs channel
         ├── workflow   every 30 seconds   replies → queue → one application at a time
         ├── browser    long-running       the recruiting Chrome, over a local socket
         └── mail       every 15 minutes   Zoho inbox → sent applications

Local state
   ├── SQLite           queue, attempts, answers, bindings, checkpoints
   ├── private files    frozen profile, resume, package, receipt, screenshots
   ├── Obsidian vault   approved profile, voice note, readable notes
   └── Erga             career evidence, resume tailoring, application status
```

## The parts

Qwen3.8-27B is the only model. It lists a posting's requirements, drafts answers to questions the profile does not cover, repairs a draft the writing scan flagged, and labels a recruiting mail the fixed rules cannot settle. [Local runtime](local-runtime.md) has the tested build.

Hermes Agent runs the conversation in `agent-control` and is the harness for every Qwen call. In Discord the agent has thirteen narrow tools. The worker's calls give Qwen no tools at all.

The workflow worker reads the owner's replies, picks the next application, and drives it through job fit, resume, form and sending by calling the browser daemon. [Application workflow](application-workflow.md) follows that path step by step.

The browser daemon owns a dedicated Chrome and is the only process that types into a form or clicks Submit. [Browser automation](browser-automation.md) describes how it observes, fills and verifies.

The feed service imports the Keryx job list and queues matching internships. The mail service reads a Zoho inbox and moves sent applications forward. Both are optional and off until configured.

[Erga](https://github.com/Adr1an04/erga-mcp) supplies approved career evidence, tailors and validates resumes, and keeps its own application status. Rove calls it through its MCP interface and never edits its database.

State is split by what each layer is good at, as described in [Memory and storage](memory-and-storage.md). Discord's channels, cards and replies are in [Discord](discord.md).

## One application from start to finish

1. A job arrives from the feed or as a link the owner pasted, and is queued once.
2. The worker opens the posting in a background tab and follows its Apply control to the form.
3. Qwen extracts the posting's requirements. Code compares them with approved facts and decides fit. A feed job with a conflict waits for the owner.
4. Erga tailors a resume, or the approved base PDF is used. The file is frozen with its hash.
5. Code fills the form from the frozen profile and from answers the owner gave before, and verifies each value.
6. Qwen drafts the remaining answers. Code validates them and scans the writing. Questions only the owner can answer are asked in Discord.
7. When the form is complete, the package is hashed. It is sent after the owner's `send it`, or at once under the `auto_submit` policy.
8. The daemon checks the live page against the package, records the attempt, clicks once, and reads the confirmation through a site adapter.
9. The thread records the result. An unclear result waits for the owner and is never retried.
10. Later, recruiting mail can move the application to OA, Interview, Offer or Rejected.

## Who decides what

Qwen proposes. It extracts requirements, writes drafts and picks a mail label. Its output is parsed against a schema and rejected when it does not fit.

Code decides everything that can be checked: whether a requirement conflicts with an approved fact, which fact fills which field, whether an option matches, whether a draft kept its facts, every state change, and whether the Submit click may happen.

The owner decides what only a person can: approving the profile, answering questions nobody else knows, accepting a job with a conflict, sending or setting the sending policy, and settling an unclear submission.

Content from outside has no authority. Posting text, form labels, company pages, mail and model output are data. [Prompt injection](prompt-injection.md) explains how that is enforced.

## How permissions are separated

`AGENTS.md` names eight operating modes as the project's permission vocabulary. The code has no mode switch. It gets the same separation from which process holds which capability:

| Process | Can | Cannot |
| --- | --- | --- |
| Hermes agent in Discord | read approved profile sections and evidence, search jobs, propose onboarding sections, queue a link, inspect the browser | approve a profile, fill a form, submit |
| Qwen calls from the worker | return text for one bounded request | call any tool |
| Company research | read up to three public pages of the employer's site over HTTPS | use the browser, credentials or the profile |
| Browser daemon | type approved values, upload the frozen resume, click Submit after an approval | take a value, a file path or a script from a caller |
| Mail service | read one inbox, advance a sent application | send mail, queue or prepare anything |
| Local command line | approve a profile, install services, park or resume an application | be reached from Discord or by the model |

## Why local

An application history holds contact details, work authorization, school records, resume versions, written answers, employer accounts and mail. Keeping the model, the vault, the database and the files on one machine keeps that archive under the owner's control and avoids a per-application model bill.

Local does not mean isolated. Discord, Zoho, GitHub and every employer site are outside the machine, and each crossing has its own checks. See [Requirements](requirements.md#network-access) and [SECURITY.md](../SECURITY.md).
