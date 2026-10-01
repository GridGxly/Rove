# Reference Mac runtime measurements

Measured on September 28 and 29, 2026 on a 14-inch M5 Pro MacBook Pro, 18 CPU / 20 GPU cores, 48GB unified memory, with normal desktop applications open. The runtime and pinned versions are in [Local runtime](local-runtime.md). These are individual observed trials, not guaranteed throughput or a long-duration stability certification. Raw requests, private process snapshots and transcripts remain outside Git.

## Direct baseline

MTP off, thinking off, temperature zero, 512 output tokens. The 16K test reserves generation space: 15,872 input + 512 output = 16,384 total.

| Input tokens | Cache state | Cached tokens | TTFT seconds | Decode tokens/second | Total seconds |
|---:|---|---:|---:|---:|---:|
| 4,096 | first | 0 | 23.64 | 12.06 | 66.08 |
| 4,096 | repeat | 2,048 | 8.36 | 11.78 | 51.82 |
| 8,192 | first | 0 | 32.74 | 11.27 | 78.17 |
| 8,192 | repeat | 6,144 | 8.79 | 11.22 | 54.40 |
| 15,872 | first | 0 | 63.40 | 10.48 | 112.25 |
| 15,872 | repeat | 14,336 | 7.35 | 10.47 | 56.27 |

Most samples in this initial run showed warning pressure. Swap grew from about 2.03 to 2.34 GiB over the suite. System-wide GPU peaks reached 100%. Cache hits improved prefill/TTFT; oMLX's displayed prompt throughput counts cached tokens and should not be interpreted as newly computed token throughput.

## Hermes comparison

September 29, MTP on. Each Hermes request had the same payload replayed directly, with 234 to 242 output tokens and one Hermes API call. The actual prompts contained 4,102 / 8,162 / 15,555 tokens.

| Input | Hermes decode tok/s | Direct decode tok/s | Hermes TTFT s | Direct TTFT s |
|---:|---:|---:|---:|---:|
| 4,102 | 21.76 | 21.73 | 0.98 | 0.53 |
| 8,162 | 17.52 | 13.12 | 15.09 | 7.66 |
| 15,555 | 14.70 | 15.05 | 43.20 | 59.30 |

The matched warm 4K case reused 4,096 tokens in both paths and is the clearest harness comparison. The longer direct replays had warmer caches, and live Discord requests overlapped with part of the benchmark. The 16K replay queued behind another request. Do not attribute those latency differences solely to Hermes. All sampled Hermes requests in this comparison showed normal memory pressure.

## Acceleration decision

A separate isolated test paused Discord ingress and generated 512 tokens from a 15,872-token prompt:

| Mode | Cached tokens | TTFT s | Decode tok/s |
|---|---:|---:|---:|
| External VLM MTP | 14,336 | 6.74 | 21.96 |
| 8-bit TurboQuant, first | 0 | 46.41 | 13.90 |
| 8-bit TurboQuant, repeated | 14,336 | 8.33 | 14.20 |

The 8-bit path really converted 15 cache layers, as confirmed by runtime logs. It cannot run alongside external VLM MTP. MTP is the selected setting. JSON, structured tool arguments, reasoning-channel output and a small arithmetic check passed. This does not establish comprehensive factual accuracy.

## Full stack and authority checks

The Discord synthetic workflow completed in 78.5 seconds with three model calls and four narrow MCP tools. It verified ten fields, used approved Erga evidence, retrieved a malicious test note through QMD, saved an unapproved answer draft and asked for unknown facts. No submission or profile mutation occurred. Browser mechanics used zero model calls. A later verified PDF upload fixture took 1.29 seconds.

A deliberate overlap test held the visible browser open while Qwen generated and QMD/Erga retrieval ran. Qwen produced 512 tokens from 8,192 input at 23.50 tokens/second. QMD retrieval took 3.65 seconds and returned the correct project. Its process exited afterward.

Activity Monitor showed 40.15GB Memory Used, 23.68GB Wired, 10.28GB Compressed, 6.00GB Cached Files and 4.18GB Swap, with a green graph. The model footprint was 18.77GB; an earlier long-request snapshot showed 20.37GB. During overlap, swap stayed flat and 16 of 18 pressure samples were normal; two were warnings. Dedicated browser aggregate RSS peaked at 1,000MiB; QMD worker RSS peaked at 1,778MiB. These process RSS figures are not Metal allocation totals.

The broader four-minute workflow/retrieval window included 101 normal and 19 warning pressure samples. Compressed memory reached roughly 21.5GiB during that window; no red samples were recorded. The desktop remained operable through computer-use checks, but precise input latency, temperature and fan RPM were not measured. High Power was enabled on AC for the September 29 runs. The tests do not isolate cooling or power mode as the cause of improvement.

A temporary 30-second idle TTL triggered an actual model unload and freed 17.87GB. The final TTL is restored to 600 seconds. Normal requests reload an idle model. Keep one active request and the 16K ceiling while collecting further real-workload evidence.

These measurements used the synthetic, prepare-only workflow. Real submission, applicant onboarding, CAPTCHA and MFA handling, and recruiting mail were outside this certification. Timing of real applications has not been measured yet.
