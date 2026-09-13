# Will this run on my machine?

Erga Autopilot is being developed on a 14-inch MacBook Pro with an M5 Pro, 48GB of unified memory, and a 1TB SSD. That is the machine the default local setup is tuned around. You do not need the exact same Mac.

If your hardware is different, it is worth checking the whole stack before downloading a large model or changing a bunch of settings. A capable model or coding agent can read this repo, compare it with your machine, and suggest a reasonable starting point.

You can use the prompt below with ChatGPT, Claude, Gemini, Grok, or another agent that can read a public GitHub repository. A coding agent with local computer access can usually do a better job because it can inspect the hardware directly.

If your model cannot open GitHub links, give it the current `README.md`, `docs/getting-started.md`, `docs/how-it-works.md`, this file, and any runtime or config files it asks for.

## Copy this prompt

Fill in the machine details you know. If the agent can inspect your computer, leave unknown fields blank and let it use safe, read-only system commands.

```text
I want to run Erga Autopilot on my own machine.

Repository:
https://github.com/GridGxly/erga-autopilot

Before recommending anything, read the current repository. At minimum, inspect:
- AGENTS.md
- README.md
- docs/getting-started.md
- docs/how-it-works.md
- docs/hardware-check.md
- the current model/runtime/config files if they exist

Use the current code and docs as the source of truth. Do not rely on an older description of the project when the repository says something different.

My machine:
- operating system:
- computer/model:
- CPU or SoC:
- GPU:
- RAM or unified memory:
- dedicated VRAM, if applicable:
- total storage:
- free storage:
- anything else relevant:

If you have local computer or terminal access, inspect missing hardware details yourself with safe read-only system commands. Do not read or upload personal files, credentials, browser data, SSH keys, environment secrets, application data, or recruiting data just to identify the hardware.

The current Erga Autopilot design runs a local model alongside Hermes Agent, Erga, Playwright/browser automation, Discord, SQLite, and optional Zoho integration. Account for the whole running stack, not just whether the model weights technically fit in memory.

Rate my machine as one of these:
1. comfortable as documented
2. workable with tuning
3. technically possible but cramped
4. not recommended for the current local stack
5. unable to run the current stack as designed

Then explain the verdict in plain language.

Check at least:
- whether the current Qwen model and quantization fit with useful memory headroom
- memory left for the operating system, Hermes, Python, Discord, SQLite, and a real browser
- KV-cache and context-window cost
- whether unified memory or dedicated VRAM changes the calculation
- CPU/GPU or accelerator compatibility with the runtime currently used by the repo
- disk space for model weights, caches, browser profiles, traces, generated resumes, and application history
- whether the current MLX/MLX-VLM path works on my platform at all
- likely bottlenecks during a real application run
- whether a headed browser is still practical while the model is loaded

Do not invent benchmark numbers, tokens-per-second figures, or memory measurements you cannot support. If real performance is uncertain, say so and give me a benchmark plan.

If the default setup is too heavy, keep the project architecture where practical and recommend the smallest useful changes. Consider, in this order when appropriate:
- lower context length
- lower KV-cache memory
- keep model concurrency at one
- change quantization
- reduce trace/screenshot retention
- change browser mode
- use a smaller compatible local model
- change the local inference runtime only when the platform requires it

Do not casually replace Hermes, Erga, Playwright MCP, Discord, or the local-first design just because my hardware differs.

Give me:

## Verdict
One of the five ratings above.

## What will fit
Explain the model/runtime memory and disk picture, including headroom for the rest of the stack.

## Main bottleneck
Tell me what is most likely to constrain this machine.

## Recommended profile
Give me the model, quantization, context length, concurrency, browser mode, and any other settings you would start with on this hardware.

## What I would change in the repo
Point to the exact current files/settings that should change. If the repository does not expose a setting yet, say that instead of inventing a path.

## How to prove it
Give me a short benchmark checklist for model load, memory pressure or VRAM use, browser usability, tool calling, cancellation/restart, and the target context size before real applicant data is connected.

## Uncertainties
Call out anything you could not verify.

If information is missing, ask only for the hardware details needed to make the verdict.

If I later ask you to adapt my clone or fork, make the changes on a new branch. Keep private runtime data outside the repo, do not commit secrets or personal applicant data, use synthetic examples, and update the relevant docs alongside verified configuration changes.
```

## If you do not know your specs

A local coding agent can inspect them. On macOS, useful read-only commands include:

```bash
system_profiler SPHardwareDataType
sw_vers
df -h /
```

On other platforms, use the normal read-only hardware tools for that operating system. Hardware detection should not require access to browser profiles, home documents, credentials, the recruiting database, or application history.

## What a useful answer looks like

"The model fits" is not enough. A real Autopilot run also needs room for the operating system, inference runtime, Hermes, Erga, Python, Discord, SQLite, and a browser with a potentially heavy application page open.

A good answer should also avoid pretending to know the exact speed of hardware it has never benchmarked. The useful part is a starting configuration and a short plan to test it on the actual machine.

If you get a stable configuration working on different hardware, a pull request with the exact machine, runtime versions, model revision, and settings is much more useful than a generic claim that a certain amount of RAM "should work."
