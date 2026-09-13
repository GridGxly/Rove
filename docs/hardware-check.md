# Will this run on my machine?

Erga Autopilot is being developed on a 14-inch MacBook Pro with an M5 Pro, 48GB of unified memory, and a 1TB SSD. That is the machine the default local-model setup is tuned around, not a rule that everyone needs the same computer.

If your hardware is different, a useful first step is to let a capable model or coding agent read the repo and compare the current stack with your machine before you start downloading models or changing config.

You can paste the prompt below into ChatGPT, Claude, Gemini, Grok, or another agent that can read a public GitHub repository. A coding agent with access to your computer can usually do an even better job because it can inspect the machine directly.

If the model you are using cannot open GitHub links, give it the current `README.md`, `docs/getting-started.md`, `docs/how-it-works.md`, and any runtime/config files it asks for instead.

## The prompt

Copy this whole block. Fill in the machine details if you know them. If your agent has local computer access, you can leave unknown fields blank and let it inspect them with non-sensitive system commands.

```text
I want to run Erga Autopilot on my own machine.

Repository:
https://github.com/GridGxly/erga-autopilot

Before recommending anything, read the current repository. At minimum, inspect:
- README.md
- docs/getting-started.md
- docs/how-it-works.md
- docs/hardware-check.md
- the current model/runtime/config files if they exist

Use the current code and docs as the source of truth. Do not rely on an old description of the project if the repo says something different.

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

If you have local computer/terminal access, inspect missing hardware details yourself using safe read-only system commands. Do not read or upload personal files, credentials, browser data, SSH keys, environment secrets, application data, or recruiting data just to identify the hardware.

The current Erga Autopilot design uses a local model alongside Hermes Agent, Erga, Playwright/browser automation, Discord, SQLite, and optional Zoho integration. Account for the whole running stack, not just whether the model weights can technically fit in memory.

Tell me which of these best describes my machine:
1. comfortable as documented
2. workable with tuning
3. technically possible but cramped
4. not recommended for the current local stack
5. unable to run the current stack as designed

Then explain the verdict in plain language.

Check at least:
- whether the current Qwen model and quantization fit with useful memory headroom
- memory left for macOS/Linux/Windows, Hermes, Python, Discord, SQLite, and a real browser
- KV-cache/context-window cost
- whether unified memory or dedicated VRAM changes the calculation
- CPU/GPU or accelerator compatibility with the runtime currently used by the repo
- disk space for model weights, caches, browser profiles, traces, generated resumes, and application history
- whether the current MLX/MLX-VLM path works on my platform at all
- likely bottlenecks during a real application run
- whether headed browser use is still practical while the model is loaded

Do not invent benchmark numbers, tokens-per-second figures, or memory measurements you cannot support. If actual performance is uncertain, say so and give me a benchmark plan.

If the default setup is too heavy, preserve the project's architecture where practical and recommend the smallest changes that make sense. Consider, in this order when appropriate:
- lowering context length
- reducing KV-cache memory
- keeping model concurrency at one
- changing quantization
- changing trace/screenshot retention
- changing browser mode
- using a smaller compatible local model
- changing the local inference runtime only when my platform requires it

Do not casually replace Hermes, Erga, Playwright MCP, Discord, or the local-first design just because my hardware differs.

Give me:

## Verdict
One of the five ratings above.

## What will fit
Explain the model/runtime memory and disk picture, including the headroom needed for the rest of the stack.

## Main bottleneck
Tell me what is most likely to constrain this machine.

## Recommended profile
Give me the model, quantization, context length, concurrency, browser mode, and any other settings you would start with on this hardware.

## What I would change in the repo
Point to the exact current files/settings that should change. If the repository does not expose a setting yet, say that instead of inventing a path.

## How to prove it
Give me a short benchmark/checklist that tests model load, memory pressure or VRAM use, browser usability, tool calling, cancellation/restart, and the target context size before real applicant data is connected.

## Uncertainties
Call out anything you could not verify.

If information is missing, ask me only for the hardware details needed to make the verdict.

If I later tell you to adapt my clone or fork, make the changes on a new branch. Keep private runtime data outside the repo, do not commit secrets or personal applicant data, use synthetic examples, and update the relevant docs alongside any verified configuration changes.
```

## If you do not know your specs

A local coding agent can inspect them for you. On macOS, useful read-only commands include:

```bash
system_profiler SPHardwareDataType
sw_vers
df -h /
```

On other platforms, let the agent use the normal read-only hardware tools for that operating system. It should not need access to your browser profile, home documents, credentials, recruiting database, or application history to answer a hardware question.

## What a good answer should not do

Be suspicious of an answer that only says "the model fits, so you're good." A working Autopilot run also needs room for the operating system, inference runtime, Hermes, Erga, Python, Discord, SQLite, and a browser that may have a fairly heavy application open.

It also should not claim a precise speed for hardware it has never benchmarked. The useful output is a starting configuration and a way to test it on the actual machine.

If you end up with a stable configuration on hardware that differs from the main development machine, feel free to document the exact hardware, runtime versions, model revision, and settings in a pull request. That is much more useful than a generic claim that a certain amount of RAM "should work."
