# Getting started

Rove is pre-alpha and is set up by hand. The steps below are in the order that keeps real data out until the earlier pieces work. [Requirements](requirements.md) is the full checklist these steps refer to.

Work with synthetic data first, then with real data and submission off, and enable sending last.

## 1. Check the machine

Read the hardware notes in [Requirements](requirements.md#platform-and-hardware). On a Mac that differs from the reference machine, use [Will this run on my machine?](hardware-check.md) before downloading the model.

```bash
xcode-select --install   # if the command-line tools are missing
uv --version
git --version
node --version           # 22 or newer, for QMD
```

## 2. Clone and install

```bash
git clone https://github.com/GridGxly/Rove.git
cd Rove
uv sync
uv run patchright install chromium
```

The last command installs Patchright's Chromium build. The synthetic fixture and the browser tests use it. The recruiting browser uses Google Chrome when it is installed and falls back to that build otherwise.

Keep hardware-specific changes in your own clone or fork.

## 3. Keep live data outside Git

The checkout holds code, docs, tests and synthetic fixtures. Everything real lives in the state root, `~/.config/rove` by default, and in your vault. The state root is created on first use. Set `ROVE_STATE_DIR` to put it somewhere else.

Do not copy a real resume, profile, receipt, mail or screenshot into the repository, including into `tests/`.

## 4. Local model and Hermes

Install oMLX, download the model, and apply the settings in [Local runtime](local-runtime.md). Install Hermes and apply its settings and the 16K patch from the same page.

```sh
uv run rove start      # start the model server, then the Hermes gateway
uv run rove status
uv run rove stop
```

The model server listens on localhost only. Weights load on the first request.

## 5. Prove the stack with synthetic data

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run rove smoke
```

The test suite runs offline and uses a temporary state folder. `rove smoke` opens a browser against a synthetic form on localhost, fills known fields from a synthetic profile and submits nothing. [Local runtime](local-runtime.md#verification-and-benchmarks) describes the benchmark commands and what the certification covered.

Do not continue with real data until these pass on your machine.

## 6. Erga

Install Erga from the Rove fork as described in [Requirements](requirements.md#which-erga-to-install). Put its real configuration at `erga/config.toml` in the state root and import your reviewed, factual master resume through Erga's own interface.

Rove reads evidence from Erga and asks it to tailor resumes. It does not edit Erga's database.

## 7. The vault

Choose a private folder outside the checkout as the Obsidian vault and record its path in `config/recruiting.json` in the state root:

```json
{"obsidian_vault_path": "/path/to/private/vault"}
```

Rove creates a `Rove/` folder inside it when the profile is approved. To give drafts your own voice, add `Rove/Story/Voice.md` with a few paragraphs you wrote yourself. [Memory and storage](memory-and-storage.md#the-obsidian-vault) describes every note.

## 8. Onboarding

The profile is collected as a draft, reviewed by you, and approved by a local command. The Hermes agent can run the interview in Discord and propose sections. It cannot approve them.

```sh
uv run rove onboarding status
uv run rove onboarding show
uv run rove onboarding approve --expected-hash REVIEWED_DRAFT_HASH
uv run rove memory index
```

[Onboarding and jobs](onboarding-and-jobs.md) covers the sections, the approval rules and the retrieval index.

## 9. Import jobs

```sh
uv run rove jobs sync
uv run rove jobs matches --limit 10
```

Importing jobs queues nothing and posts nothing.

## 10. Create your own Discord bot

Every install needs its own bot in a private server. The maintainer's bot is not shared.

1. Create an application and a bot in the Discord Developer Portal.
2. Turn off Public Bot and enable Message Content Intent.
3. Put the token in the private env file as `DISCORD_BOT_TOKEN`.
4. Invite the bot to a private server you own, with the permissions listed in [Requirements](requirements.md#create-your-own-discord-bot).
5. Create the channels, the forum and its tags from [Discord](discord.md#channels).
6. Put your own numeric user ID in the env file as `DISCORD_OWNER_USER_ID`.

The token and all server, channel and user IDs stay in local configuration and are never committed. Rove acts only on messages from the configured owner.

Then configure the Hermes gateway for the same bot, owner and `agent-control` channel, as in [Local runtime](local-runtime.md#discord-gateway).

## 11. Write the configuration

Create `config/workflow.json` and `config/feed.json` in the state root from the key tables in [Requirements](requirements.md#local-configuration). For the first runs:

- set `enabled` to `true` in `workflow.json`
- leave `submission_enabled`, `auto_submit` and `auto_use_drafts` unset
- set `enabled` to `false` in `feed.json` until you want feed jobs queued

## 12. Install the services

```sh
uv run rove install-services
uv run rove browser status
uv run rove workflow status
```

This installs the four launchd agents listed in [Requirements](requirements.md#services). The browser service starts the recruiting Chrome in the background. Its profile is separate from your everyday browser, and it should stay that way: no personal logins, no password manager, no sync.

To stop a service, unload it:

```sh
launchctl bootout gui/$UID/dev.rove.workflow
```

Service output goes to `logs/` in the state root.

## 13. Prepare one application

Paste a job link in `agent-control`, or queue it from the shell:

```sh
uv run rove workflow enqueue --url https://jobs.example.com/internship
```

The worker picks it up on a following tick and opens a forum thread. Follow it there: the job-fit card, the resume, the filled form, the drafts and any question for you. With submission off, a complete form ends with a "Ready · send it yourself" card, and you press Submit in the recruiting Chrome.

Check the filled values against what you approved. Read [Application workflow](application-workflow.md) for what each stop means and [Discord](discord.md#replies) for the replies.

## 14. Turn on sending

When preparation is reliable for you, set `submission_enabled` to `true` and list the adapters in `submit_adapters`. A complete form now ends with "Ready to submit", and your `send it` reply sends that exact package once.

Read [Sending](application-workflow.md#sending) first. A submission cannot be undone, and an unclear result waits for you.

## 15. Optional: the feed, unattended sending and mail

- Set `enabled` in `config/feed.json` and run `uv run rove feed seed` once to start receiving feed jobs.
- Set `auto_submit` only after you have reviewed several applications that Rove prepared and you sent. [Unattended sending](application-workflow.md#unattended-sending) explains the cap, the gap and what still stops.
- Connect Zoho for mail tracking with the steps in [Requirements](requirements.md#zoho-mail).

## Updating

```sh
git pull
uv sync
uv run rove install-services
launchctl kickstart -k gui/$UID/dev.rove.browser
```

The last command restarts the browser daemon so it runs the new code. The Chrome window and its tabs survive. After updating Hermes, review and reapply the 16K patch. After updating Erga, follow the recipe in [Requirements](requirements.md#which-erga-to-install).

## Where to look when something is wrong

- the application's thread and `system-log` in Discord
- `uv run rove workflow status`, `uv run rove browser status` and `uv run rove mail status`
- `logs/` in the state root, including `delivery-failures.log`
- the application's folder under `applications/` in the state root
