# Discord

Discord is Rove's remote control and its readable record. SQLite holds the exact state and private files hold the artifacts. If Discord and local state disagree, local state wins, and Discord can be repaired from it.

Every install uses its own bot in a private server. [Requirements](requirements.md#create-your-own-discord-bot) explains how to create one and which permissions it needs.

## Channels

```text
SOURCES
# internship-jobs       one card per new feed job

PIPELINE
applications            forum, one post per application
# shortlist             job-fit decisions

AGENT
# agent-control         talk to the Hermes agent, paste job links
# action-needed         everything else that waits on the owner
# memory                review and correct remembered answers

RECRUITING
# recruiting            one line per classified recruiting mail

SYSTEM
# system-log            identifiers and technical events
```

The names are logical. Private configuration maps each one to a channel ID, and no ID belongs in the repository. Three channels can also be found by name: when `system_channel_id`, `recruiting_channel_id` or `memory_channel_id` is missing, Rove looks for a channel named `system-log`, `recruiting` or `memory` once and writes the ID into the private config. Without such a channel that feature stays off.

The feed posts internships only, so a new-grad source channel would stay empty.

## Who Rove listens to

Rove acts only on messages whose author is the configured numeric owner ID and is not a bot. Display names and usernames are never checked.

The worker reads new messages every tick in `agent-control`, `action-needed`, `shortlist`, `recruiting`, `system-log` and the thread of every application that is not in the applied or submitting state. It reads `memory` separately, as described under [Memory channel](#memory-channel). The first read of a channel only records a position, so old messages are never replayed. Each message is applied once.

## The application thread

The forum post is created before preparation starts. Its first message is a live status card that Rove edits in place: the headline, one line of context, the posting link and up to four replies. The forum list therefore previews the current state of every application.

The messages below it are the chronological record. Routine steps are one line each: the page opened, the Apply control clicked, the resume ready, a form step taken, the company lookup before drafting, optional fields left blank, the owner's replies, state changes with their trigger, and the send. Cards are used where there is something to read:

- the job-fit result, with conflicts and eligibility that could not be checked
- the filled form, one field per entry with its value and where it came from
- each Qwen draft, with the reply that approves it
- every stop that needs the owner
- account creation and sign-in
- the submission result
- recruiting mail

Sources appear in words such as "your profile", "your reply", "your earlier answer", "your resume" and "the posting". Application IDs, field keys and package hashes are left out of everything the owner reads. They go to `system-log`.

### Files posted in the thread

- the resume PDF, when it is prepared, with a line saying whether Erga tailored it or it is the approved base
- a tailored draft Erga rejected, rendered to PDF and marked as not sent, with the reason
- the browser screenshot from the moment a run stopped
- after a send that did not confirm, the form just before the click and the page after it

Files over 8 MiB are skipped. A screenshot shows the form as filled, including contact details, which is one reason the server must be private. The regular observation takes no screenshot of a page with a password field or a field labeled social security, passport, bank account or verification code.

## Owner channels

`action-needed` and `shortlist` are to-do lists. An application has at most one live card in each. A new card replaces the previous one, and the cards are deleted as soon as the application stops waiting on the owner.

A card shows the job title as a link to the thread, a headline, the reason, the numbered questions, and the replies in a code block. `shortlist` gets job-fit decisions and nothing else. `action-needed` gets every other stop. The stops are listed in [Application workflow](application-workflow.md#when-rove-stops-for-the-owner).

With the `auto_submit` policy on, a package that completes is sent without a card. The thread is where the owner reviews it afterward.

## Replies

A reply works in three places:

- in the application's own thread, where the application is implied
- as a Discord reply to the application's card in `action-needed` or `shortlist`
- as a plain message in one of those two channels, when exactly one card is live there

The whole message must be the reply. Case, surrounding backticks and a trailing period, exclamation mark or question mark are ignored.

```text
go · proceed · continue · resume              carry on
park it · park · defer · later · skip         park it and let the queue continue
send it · send · submit · apply · apply now   send the ready package once
use draft 2 · draft 2 · use 2                 approve Qwen's second draft as shown
2: value · 2 = value · answer 2: value        answer question 2
2: skip                                       leave optional question 2 blank
applied · i applied · done · sent it myself   the owner sent or confirmed it
not sent · not submitted · nothing sent       the owner verified nothing was sent
create account · make an account · account    allow one account on this board
```

`go` after a job-fit hold accepts the job and continues. Anywhere else it prepares the application again from a fresh load of the page.

`park it` parks the application and closes its tab. A later `go` picks it up.

`send it` binds to the package that is ready at that moment. It is refused when nothing is ready, when the application was already sent, or while a send is in flight.

Question numbers count the list on the latest stop card, in form order. Draft numbers count Qwen's drafts in the order it wrote them, and each draft card names its own reply. After answering, reply `go` so the form is prepared again with the answers.

When exactly one question is open, a plain reply of up to 200 characters without a link is taken as its answer. If the question has options, the reply must name one of them.

`use draft N` approves the exact text on the card. It is refused if the approved profile changed since the draft was written.

`skip` works only for an optional question. A question on a password, file or hidden field, or one labeled social security, passport, bank account, verification code or driver's license, cannot be answered through Discord.

`applied` and `not sent` apply only when a submission is unclear or the application was handed to the owner in the browser.

An answer typed as `N: value` is remembered for the same question on later forms. See [Application workflow](application-workflow.md#remembered-answers).

A reply that cannot apply gets one plain line in the same channel saying why. Any other owner message in a channel the worker reads gets one line listing the replies. In `agent-control` the worker stays quiet, because the Hermes agent answers there.

### Explicit forms

These carry the application ID, so they work in `agent-control` and anywhere else the worker reads:

```text
resume APPLICATION_ID
defer APPLICATION_ID
proceed APPLICATION_ID
account APPLICATION_ID create
submit APPLICATION_ID PACKAGE_HASH
reconcile APPLICATION_ID applied
reconcile APPLICATION_ID not-submitted
use APPLICATION_ID FIELD_KEY PROPOSAL_HASH
answer APPLICATION_ID FIELD_KEY = value
```

A hash may be shortened to its first 8 or more hex characters. It is rejected when it does not match the current package or draft. The IDs and hashes are in `system-log`.

## Agent control

`agent-control` is where the owner talks to Rove in plain words, usually from his phone. The Hermes gateway answers there with Qwen and the narrow tool list in [Onboarding and jobs](onboarding-and-jobs.md#hermes-connection). Nothing the agent can call fills a form, approves a fact or submits.

The chat shows answers only. Tool calls, progress lines, reasoning and "still working" notes are switched off for Discord in the [gateway settings](local-runtime.md#discord-gateway). The 👀 and ✅ reactions on his message and the typing indicator are the only signs of work. Each tool call is one line in `system-log` instead: what was done in plain words, how long it took, and ok or failed. The line never carries the arguments or the result. A tool that fails gives the agent one plain sentence, not an exception.

A message in the channel, "What you can ask Rove", lists what works:

```text
paste a job link               queued as his own; add first to jump the line
status                         what Rove is doing, the queue, sends today
what's waiting on me           what needs him, and which channel has each card
why did you skip Acme          what happened with one company, and why
how many did you send today    sends today and the daily cap
pause · resume                 hold or restart jobs from the feed
where do I go to school        any fact from the approved profile
what do you know about me      points to memory, where list shows the remembered answers
```

Code writes the answers to these: the agent calls one tool and passes its words on. A fact about the owner is read from the approved profile every time, and the agent says "I don't know that yet" rather than guess. A request Rove cannot carry out, such as sending an application, changing the profile or a remembered answer, signing in, solving a CAPTCHA or sending mail, gets one sentence naming the closest thing it can do. A message it does not understand gets a few examples to try.

Rove posts that message once and keeps its message ID in the private workflow config as `control_help_message_id`, with a hash of its text. When the text changes, the next start of the MCP server edits the same message. The bot pins it when it has Discord's Pin Messages permission; without it, the owner pins it once by hand.

### Pasted links

A message that is only job links, or links with a few words such as "apply" or "please", is the owner's own link. It skips the job-fit hold and goes ahead of every feed job; several of his links go oldest first.

- `first`, `now`, `next`, `priority`, `asap` or "do this one first" next to the link puts it ahead of his other pasted links that have not started.
- `first` or `move it up` on its own moves his latest paste to the front, when it comes within half an hour of the paste or as a Discord reply to Rove's line about it.
- The latest `first` wins.

The answer says where the link stands: "Queued. It goes next." or "Queued. 2 of your links are ahead of it; say `first` to move it up." A link that is already tracked gets its state, for example "Already tracked: applied."

Exactly one answer is posted. Hermes cannot stay silent on a message, so the agent gives the answer: it calls `answer_paste`, which reads the owner's own recent messages back from Discord, checks the author ID, applies them through the same code the worker uses and returns that code's line. The agent's words carry no authority here. Each message is applied once, by whichever side reaches it first, and recorded in the `control_replies` table. A line no agent picked up within 45 seconds, because the gateway or the model is down, is posted by the worker.

The worker answers nothing else in `agent-control`. The explicit forms above still work there.

## Memory channel

`memory` is where the owner reviews and corrects the answers Rove remembers. The store is the `answer_memory` table in SQLite, and the channel is a way to read and change it. No model reads the channel: messages are matched by keywords and numbers in code. Only the configured owner is heard, and every message gets one plain reply.

```text
what do you know · list                         numbered list, most used first, 12 per message
more                                            the rest of the list
relocation · what do you answer for relocation? the answers whose question carries those words
forget relocation · forget 3                    remove one answer
change 3 to No · relocation: No                 change one answer
remember: question = answer                     add an answer
```

Numbers refer to the last list shown. "Most used" is counted from the packages of prepared applications. A new value for a question that has options must be one of them.

An answer added with `remember:` has no form behind it, so it is used only on forms that word the question the same way.

Answers about work authorization, sponsorship, citizenship, clearance, criminal history, demographics, age and similar topics appear in a list as "saved". Rove spells one out only when the message names that question.

Approved profile facts can be read here and not changed. Asking about the name, email, phone, school, major, degree, graduation, work authorization, sponsorship or citizenship shows the approved value and says that it changes through the profile flow. A `remember:` line for a question the profile already answers is refused the same way.

Rove does not keep an answer to an identity or credential question, such as a social security number, passport, bank account, verification code or password.

When Rove learns or changes an answer in an application thread, it posts one line here: "Saved: when a form asks ..., I answer ...". For a private answer the line leaves the value out.

The channel has no effect on applications. A reply such as `go` typed here is read as a question about memory. Replies are written to an outbox first, so a Discord outage delays a reply without losing it.

## Feed channel

The feed service posts one card per new matching job: the title linking to the posting, the company and location, the cycle and track, and a footer that says whether it was queued. See [Application workflow](application-workflow.md#where-jobs-come-from).

## Recruiting channel

The mail service posts one line per classified mail: the label in words (Application received, Online assessment, Interview, Offer, Rejected, or Recruiting mail when Qwen could not place it), the application, the sender's domain, the subject, the deadline as the mail states it, and a link to the thread. The mail body never leaves the private state root.

## System log

`system-log` carries what the owner's cards leave out. Each line starts with the application ID:

- the thread opened, with the posting URL and the thread link
- the job-fit decision
- the company research result
- a package approved or queued for sending, with its hash
- each submit attempt, with the adapter and package hash
- the outcome: applied with the confirmation URL, unclear or rejected with the reason
- state changes with their trigger
- stops with their headline, and stopped preparation with the step and error type
- Qwen failures and model outages
- blocked sites
- recruiting mail, with the message ID and how it was classified
- Discord deliveries the API refused

Lines are best effort. A line that fails is written to the private delivery log and not retried, and nothing else waits on it. No line carries a secret or an applicant's answer.

## Forum tags

Tags are the quick status. The state-change line in the thread is the record of why.

| State | Tags |
| --- | --- |
| Queued, preparing, parked, submitting | `Preparing` |
| Needs the owner, ready for review, needs the owner in the browser, submission unclear | `Preparing`, `Needs Action` |
| Applied | `Applied` |
| Online assessment | `OA`, `Needs Action` |
| Interview | `Interview`, `Needs Action` |
| Offer | `Offer`, `Needs Action` |
| Rejected | `Rejected` |

The tags must exist on the forum, and private config maps each name to its tag ID. A tag that is not mapped is skipped. `Accepted`, `Withdrawn` and `Priority` are not set by code.

## Delivery

Thread entries and owner cards are written to SQLite first and posted afterward. A post that fails is logged to `logs/delivery-failures.log` under the state root and retried on every worker tick, so a Discord outage delays the record without losing it. Each message carries a stable nonce that Discord is asked to enforce, which guards against a duplicate when a retry follows closely.

A failed card deletion or status-card edit is logged and not retried. A forum post whose creation timed out is not created again automatically, because the first request may have succeeded. That application waits until someone checks the forum.

## What stays out of Discord

Passwords, tokens, cookies, verification codes, encryption keys and mail bodies are never posted. Account creation is recorded with the host and the username only.
