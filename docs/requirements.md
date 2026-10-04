# Requirements

This is the checklist for a full local install: software, accounts, configuration files, services and network access. [Getting started](getting-started.md) puts the steps in order. When a change adds or removes anything listed here, update this page in the same branch.

The repository shows which keys and variables exist. It never contains a real value, ID, path or applicant fact.

## Platform and hardware

Apple Silicon macOS is the only tested platform. The reference machine is a 14-inch MacBook Pro with an M5 Pro (18-core CPU, 20-core GPU), 48GB of unified memory and a 1TB SSD. The service installer uses launchd and the browser daemon uses macOS commands, so other platforms need code changes.

Different hardware may need a smaller context window, another quantization or another model. See [Will this run on my machine?](hardware-check.md) before downloading a large model.

## Software

| Component | Used for | Where the code expects it |
| --- | --- | --- |
| Xcode Command Line Tools, Git | building and cloning | on `PATH` |
| Python 3.12 to 3.14 | Rove itself, as declared in `pyproject.toml` | managed by `uv` |
| [`uv`](https://docs.astral.sh/uv/) | the virtual environment and locked dependencies | on `PATH` |
| oMLX with the Qwen3.8-27B MLX weights | the local model server | `~/.omlx/bin/omlx`, API at `http://127.0.0.1:8000/v1` |
| Hermes Agent | the Discord agent and the harness for Qwen calls | `~/.local/bin/hermes`, checkout at `~/.hermes/hermes-agent` |
| Erga, from the Rove fork | evidence, resume tailoring, application status | `~/.local/bin/erga-mcp` |
| Tectonic 0.17.0 | resume compilation | `~/.local/bin/tectonic` |
| Obsidian, or any Markdown vault | the approved profile and readable notes | a folder outside the checkout |
| QMD, with Node.js 22 or newer | search over the approved profile copy | `~/.local/share/rove/qmd` |
| Google Chrome | the source of the Rove Browser: `rove browser install` copies it under the state root with its own bundle id and Rove's icon. The owner's own Chrome is never used | `/Applications/Google Chrome.app` |
| Patchright's Chromium build | the synthetic fixture and the browser tests only | installed by `uv run patchright install chromium` |
| [Unslop](https://github.com/theclaymethod/unslop) clone, optional | its scanners for draft cleanup | the folder named by `unslop_path` |

Rove's Python dependencies are locked in `uv.lock`: `httpx`, `pydantic`, `mcp`, `pyyaml`, `psutil`, `patchright` and `cryptography`. [Local runtime](local-runtime.md) lists the tested versions of everything above.

QMD's requirements can change. At the time of writing, the Hermes QMD skill needed Node.js 22 or newer and SQLite with extension support, and its first run downloaded about 2GB of helper models. QMD 2.8.3's packaged SQLite worked on the tested machine without a separate install. Check the current upstream instructions when you set it up.

## Local model

The reasoning model is Qwen3.8-27B at 4-bit, served by oMLX on localhost with one request at a time and a 16K context. The served model name and the API address are constants in `src/rove/runtime.py`. The API key is read from `ROVE_MODEL_API_KEY` or from oMLX's own settings file.

No cloud model is required anywhere in the stack. Before connecting real data, run the certification steps in [Local runtime](local-runtime.md) on your own machine.

The pinned Hermes build expects a 64K context. Rove's 16K setup needs the opt-in patch described in [Local runtime](local-runtime.md#hermes-integration-and-compatibility-patch).

## Erga

Erga provides career evidence, resume tailoring and validation, and application status. Rove calls it through its MCP interface and never writes its database.

### Which Erga to install

Install Erga from the project's own fork, [GridGxly/erga-mcp](https://github.com/GridGxly/erga-mcp), branch `fetch-headers-and-job-text`, as a `uv` tool. That branch is upstream `main` plus one commit: Erga's job fetch sends ordinary browser request headers, and `intake_job_url` accepts a `job_text` argument so Rove can hand over the posting its own browser already captured when a careers site refuses Erga's direct fetch. The fork's `main` tracks upstream and carries nothing of its own.

```bash
uv tool install --force --python 3.12 \
  "git+https://github.com/GridGxly/erga-mcp@fetch-headers-and-job-text"
erga review --config ~/.config/rove/erga/config.toml
```

That installs `erga`, `erga-mcp` and `erga-tokens` under `~/.local/bin`, which is where Rove looks for `erga-mcp`. Erga's configuration for real evidence lives at `erga/config.toml` in the state root.

To pull upstream updates into the fork and reinstall:

```bash
git clone https://github.com/GridGxly/erga-mcp.git && cd erga-mcp
git remote add upstream https://github.com/Adr1an04/erga-mcp.git
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout fetch-headers-and-job-text && git merge main
# resolve conflicts if any, then run Erga's own gate before pushing:
uv lock --check && uv run ruff format --check && uv run ruff check && uv run mypy src \
  && uv run coverage run -m unittest discover -s tests
git push origin fetch-headers-and-job-text
uv tool install --force --python 3.12 \
  "git+https://github.com/GridGxly/erga-mcp@fetch-headers-and-job-text"
erga review --config ~/.config/rove/erga/config.toml
.venv/bin/pytest -q   # Rove's suite, from this repository
```

Keep the fork's `main` fast-forward only so later upstream merges stay clean. Record the installed commit in [Local runtime](local-runtime.md) when it changes.

## Local state

All runtime state lives outside the checkout, under one state root:

```text
~/.config/rove/
```

Set `ROVE_STATE_DIR` to use another folder. [Memory and storage](memory-and-storage.md#private-files) describes what the folder holds. The private Obsidian vault is a separate folder.

## Local configuration

Configuration is a handful of private JSON files and one env file under `config/` in the state root. All of them are yours to write. Nothing in the repository generates them.

### `config/workflow.json`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns on the worker and its Discord posting. |
| `guild_id` | none | The Discord server ID. |
| `forum_channel_id` | none | The `applications` forum. |
| `control_channel_id` | none | `agent-control`. |
| `action_channel_id` | none | `action-needed`. |
| `shortlist_channel_id` | none | `shortlist`. |
| `system_channel_id` | looked up | `system-log`. Found by name once and saved when missing. |
| `recruiting_channel_id` | looked up | `recruiting`. Found by name once and saved when missing. |
| `memory_channel_id` | looked up | `memory`. Found by name once and saved when missing. |
| `tags` | `{}` | Forum tag IDs by name: `Preparing`, `Applied`, `OA`, `Interview`, `Offer`, `Rejected`, `Needs Action`. |
| `hermes_python` | none | Path to the Python inside the Hermes install. Qwen calls run with it. |
| `submission_enabled` | `false` | Allows the Submit click at all. |
| `submit_adapters` | `[]` | Enabled adapters in order: `greenhouse_v1`, `lever_v1`, `generic_v1`. List `generic_v1` last. |
| `first_send_hold` | `unfamiliar` | Which forms wait for the owner's `go` before anything is typed. `unfamiliar`: a form on a host outside the board table that the owner has not let in yet. `all`: also the first application to each employer on a board. `off`: nothing waits; a host that can never take applicant data is still refused. See [Where a form may be filled](application-workflow.md#where-a-form-may-be-filled). |
| `auto_submit` | `false` | Sends a complete package without a `send it` reply. |
| `auto_use_drafts` | value of `auto_submit` | Uses Qwen's drafts as answers without a `use draft` reply. |
| `max_submissions_per_day` | `30` | Cap on submission attempts per UTC day under `auto_submit`. Links you paste and digest picks are exempt. |
| `min_minutes_between_submissions` | `0` | Minimum gap after the last attempt on any site, under `auto_submit`. |
| `min_seconds_between_submissions_per_platform` | `90` | Minimum gap between attempts on the same job board or employer site. `0` turns it off. |
| `max_waiting_applications` | `1` | How many applications may wait on the owner before the worker stops starting feed jobs. Only applies when `auto_submit` is off. |
| `max_new_holds_per_day` | no brake | With `auto_submit` on, feed jobs wait until the next UTC day once this many feed applications have stopped for the owner. Absent or `0` means no brake. |
| `feed_paused` | `false` | Holds every feed job. Links you paste and digest picks still run. `pause` and `resume` in `agent-control` write it. |
| `feed_max_age_days` | `21` | Queued feed jobs older than this are parked. `0` keeps them. |
| `browser_app` | unset | Leave unset for the Rove Browser. `shared-chrome` runs `/Applications/Google Chrome.app` itself, with the old profile and a warning in `system-log` at every daemon start. |
| `max_open_tabs` | `5` | Tabs kept open in the Rove Browser. |
| `human_pacing` | `true` | Paced typing, clicks and navigation. |
| `unslop_path` | none | A local Unslop clone whose scanners replace the built-in Unslop list. |
| `model_transport` | `hermes` | How the worker sends its prompts: through the Hermes harness, or `direct` as one request to the model server (for measurement). |
| `model_keepalive` | `true` | Sends a one-token request while jobs are queued and the model has been idle for eight minutes, so the weights stay loaded. |
| `timing` | `true` | Records stage timings for `rove bench report`. |
| `erga_failure_limit` | `3` | Identical Erga intake failures in a row before intake is paused. |
| `min_free_disk_gb` | `2` | Below this much free disk space no new application starts, and one card in `action-needed` says so until there is room again. |
| `erga_pause_minutes` | `60` | How long Erga intake stays paused after that; the approved base PDF is used meanwhile. |
| `control_help_message_id` | set by Rove | The "What you can ask Rove" message in `agent-control`, so it is edited in place and never posted twice. |
| `control_help_hash` | set by Rove | A hash of that message's text, so it is only edited when the text changes. |

A synthetic example:

```json
{
  "enabled": true,
  "guild_id": "123456789012345678",
  "forum_channel_id": "123456789012345678",
  "control_channel_id": "123456789012345678",
  "action_channel_id": "123456789012345678",
  "shortlist_channel_id": "123456789012345678",
  "tags": {"Preparing": "123456789012345678", "Applied": "123456789012345678"},
  "hermes_python": "/path/to/hermes/python",
  "submission_enabled": false,
  "submit_adapters": ["greenhouse_v1", "lever_v1", "generic_v1"]
}
```

What these policies do is described in [Application workflow](application-workflow.md#unattended-sending).

### `config/feed.json`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns on the feed service. |
| `channel_id` | none | The jobs channel for feed cards. |
| `batch_size` | `10` | Cards posted per feed tick. |
| `max_pending` | `40` | Pending announcements kept. Older ones expire unposted. |

The feed tick reads this file on every run. Create it even to keep the feed off, with `{"enabled": false}`.

### `config/mail.json`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns on mail tracking, together with the Zoho values below. |
| `lookback_days` | `3` | How far back the first tick reads. |

### `config/recruiting.json`

`obsidian_vault_path` is the path to the private vault. The launchd services read the vault path from this file. A shell can use the `OBSIDIAN_VAULT_PATH` environment variable instead.

### The private env file

Rove reads `KEY=value` lines from `config/setup.env` in the state root and from `~/.hermes/.env`. When both define a key, the Hermes file wins.

```env
DISCORD_BOT_TOKEN=replace-me
DISCORD_OWNER_USER_ID=123456789012345678
ZOHO_CLIENT_ID=replace-me
ZOHO_CLIENT_SECRET=replace-me
ZOHO_REFRESH_TOKEN=replace-me
ZOHO_ACCOUNT_ID=replace-me
```

Every value above is a placeholder. When `DISCORD_OWNER_USER_ID` is absent, the first entry of Hermes' `DISCORD_ALLOWED_USERS` is used as the owner.

### Environment variables

| Variable | Meaning |
| --- | --- |
| `ROVE_STATE_DIR` | The state root. Defaults to `~/.config/rove`. |
| `OBSIDIAN_VAULT_PATH` | The vault path for commands run from a shell. |
| `ROVE_MODEL_API_KEY` | The local model API key. Defaults to the key in oMLX's settings file. |
| `HERMES_AUTOPILOT_16K` | Opts in to the 16K context patch for Hermes. Rove sets it for its own Qwen calls. |

## Create your own Discord bot

Every install uses its own bot. The repository ships code only, and the maintainer's bot is private and not shared. The token and every server, channel and user ID stay in your local configuration and are never committed.

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a bot to it.
2. On the bot's page, turn off Public Bot so only you can add it to a server.
3. On the same page, enable Message Content Intent. Rove reads the text of your replies, and the Hermes gateway needs it too.
4. Copy the bot token and put it in the private env file as `DISCORD_BOT_TOKEN`. Rove and the Hermes gateway use the same bot.
5. Create a private server that you own, and invite the bot with the `bot` scope and only these permissions:

   | Permission | Why the code needs it |
   | --- | --- |
   | View Channels | read the channels it works in |
   | Read Message History | fetch your new replies |
   | Send Messages | post cards and lines, and create posts in the `applications` forum |
   | Send Messages in Threads | write in each application's thread |
   | Embed Links | cards are embeds |
   | Attach Files | the resume PDF and screenshots |
   | Manage Threads | change the tags on its forum posts |

   The bot edits and deletes only its own messages, so it does not need Manage Messages. It never creates a channel, so it does not need Manage Channels. It looks up `system-log`, `recruiting` and `memory` by name through the server's channel list, which needs no extra permission. Tag updates are best effort: without Manage Threads the only effect is a tag that may not change.
6. Create the channels and the forum from the layout in [Discord](discord.md#channels), and add the forum tags listed there. Make them visible only to you and the bot.
7. Turn on Developer Mode in your Discord client and copy the IDs of the server, each channel and your own user. The forum's tag IDs are in the `available_tags` list that Discord's API returns for the forum channel. Put the server, channel and tag IDs in `config/workflow.json`, the jobs channel in `config/feed.json`, and your user ID in the env file as `DISCORD_OWNER_USER_ID`.

Rove acts only on messages written by that configured user ID. Messages from anyone else, and from any bot, are ignored.

A third-party bot that posts jobs into a source channel should see only that channel. It should not be able to read the application forum, the owner channels or the system log.

The Hermes gateway has its own Discord settings: the same owner ID and the `agent-control` channel. They are described in [Local runtime](local-runtime.md#discord-gateway). Discord's portal changes over time, so check its current documentation if a label here has moved.

## Zoho Mail

Zoho is optional. When it is configured, `rove mail tick` reads the Inbox of one Zoho Mail account every 15 minutes and classifies recruiting mail about sent applications. See [Application workflow](application-workflow.md#recruiting-mail).

The integration uses the official Zoho Mail REST API through a Self Client and a refresh token. It only reads: the folder list, message headers, and one message body at a time. It never sends, moves or deletes mail. Setup, once:

1. In the [Zoho API Console](https://api-console.zoho.com/), add a Self Client and note its client ID and client secret.
2. On its Generate Code tab, enter the scopes `ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ`, a short description, and a validity of a few minutes. Create the code.
3. Within that time, exchange the code for a refresh token. The US accounts server is shown. Use the one for your data center, for example `accounts.zoho.eu` or `accounts.zoho.in`:

   ```sh
   curl -s -X POST https://accounts.zoho.com/oauth/v2/token \
     -d grant_type=authorization_code -d client_id=... -d client_secret=... -d code=...
   ```

   The response holds `refresh_token` and an `access_token` that lasts an hour. The service gets a new access token from the refresh token on every tick.
4. Find the account ID with that access token:

   ```sh
   curl -s -H "Authorization: Zoho-oauthtoken ..." https://mail.zoho.com/api/accounts
   ```

   `data[0].accountId` is the value.
5. Put the four values in the private env file as `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` and `ZOHO_ACCOUNT_ID`. Outside the US data center also set `ZOHO_API_BASE`, for example `https://mail.zoho.eu`. The matching accounts server is derived from it, or set `ZOHO_ACCOUNTS_BASE` explicitly.
6. Write private `config/mail.json` with `{"enabled": true}` and run `uv run rove install-services`. `uv run rove mail status` shows whether the values were found, without printing them.

The variable names are safe to document. Their values are not, the refresh token above all. Never paste the curl output into the repository, the vault, Discord or an issue.

## Services

`uv run rove install-services` writes four launchd agents to `~/Library/LaunchAgents` and loads them. It needs the project's virtual environment, so run `uv sync` first.

| Service | Runs | Schedule |
| --- | --- | --- |
| `dev.rove.browser` | `rove browser serve` | at login, long-running; the browser itself opens with the first job |
| `dev.rove.feed` | `rove feed tick` | every 15 minutes |
| `dev.rove.workflow` | `rove workflow tick` | every 30 seconds |
| `dev.rove.mail` | `rove mail tick` | every 15 minutes |

Each agent runs the checkout's `.venv/bin/rove` with a fixed `PATH` and `ROVE_STATE_DIR`, and writes `<name>.out.log` and `<name>.err.log` under `logs/` in the state root. Running the installer again leaves an unchanged, loaded service alone and reloads one whose definition changed.

Each timed service checks its own switch on every run, so an installed service does nothing until its config file enables it.

The browser service needs the Rove Browser, built once with `uv run rove browser install` from your installed Google Chrome. The copy lives at `browser/Rove Browser.app` in the state root and is rebuilt by the daemon when Chrome updates. [Browser automation](browser-automation.md#the-rove-browser) describes what the copy changes and what it keeps.

## Hermes

Hermes runs the Discord agent in `agent-control` and is the harness for every Qwen call the worker makes. The Rove MCP server is registered in Hermes under the name `rove`, which makes its toolset `mcp-rove`. The production profile enables that toolset only, with an include list of thirteen tools. [Local runtime](local-runtime.md#hermes-integration-and-compatibility-patch) has the settings and [Onboarding and jobs](onboarding-and-jobs.md#hermes-connection) has the tool list.

## Accounts

- a GitHub account, to clone or fork
- a Discord account, a private server and your own bot
- a Zoho Mail account with API access, if you want mail tracking

Employer accounts are created only when a board requires one and you reply `create account`. Their generated passwords stay in the encrypted local store.

## macOS permissions

Rove's code asks for no Accessibility, Screen Recording or Full Disk Access permission. The daemon starts the Rove Browser with `open` and reads the frontmost app with `lsappinfo`, and screenshots come from the browser itself. `rove browser install` uses `ditto`, `sips`, `iconutil`, `codesign` and `xattr`, which need no permission either. macOS may still ask for folder access if the vault sits in a protected folder such as Documents.

Do not grant a broad permission because a tool asks for it. If a future feature needs one, it should document the reason and scope.

## Network access

The stack is local-first, not offline. A normal run reaches:

- Discord's API
- GitHub, for the Keryx job list
- employer and applicant-tracking sites, in the Rove Browser
- employer home pages, for company research
- Zoho's API, when mail tracking is on

Setup also downloads dependencies, the model weights and QMD's helper models.

The model server listens on `127.0.0.1` only. The browser daemon listens on a Unix socket, and the Rove Browser's DevTools port is bound to localhost. Anything running as your macOS user can reach those, which is why the local account is trusted in this design.

## Data Rove does not handle

Full Social Security numbers, bank and routing details, passport or driver's-license numbers and images, and SMS, authenticator or security-key MFA are never stored or automated. A page that asks for one is handed to the owner.

## Repository safety

Before committing or pushing:

- inspect `git status` and the staged diff
- run `uv run python scripts/check_staged.py`
- confirm no applicant data, vault content or runtime artifact is staged
- do not bypass a secret-scanning or push-protection warning

Tests and fixtures use synthetic people, companies, jobs, mail, credentials and IDs. `.gitignore` is a backup layer and does not make the checkout a safe place for private files.
