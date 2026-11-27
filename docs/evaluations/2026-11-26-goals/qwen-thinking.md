# Does thinking help Qwen edit goals?

**Excalibur · Qwen 3.5 4B Q4_K_M · November 26, 2026**

**Thinking fixed none of the three failed follow-up edits.** Full Chat passes fell from 27/34 (79%) to 20/34 (59%). The median turn took 90.1 seconds with thinking and 17.5 seconds without it. Every Chat turn took longer. Two grades improved, ten regressed and twenty-two stayed the same. [All 38 paired cases](qwen-thinking-matrix.csv) · [Thinking-on evidence and timed worker controls](qwen-thinking-evidence.jsonl) · [Thinking-off report](goal-fixes.md)

## The three follow-up edits

| User request | Thinking off | Thinking on | Time multiplier | Saved result with thinking |
| --- | ---: | ---: | ---: | --- |
| F1 · Change the hourly job watch to every 15 minutes | 27.7 s | 171.6 s | 6.2× | Kept 60 minutes. The reply rejected 15 minutes and offered 15 minutes in the same answer. |
| F2 · Only report remote engineering roles | 44.1 s | 137.6 s | 3.1× | Kept the broad filter. The reply treated a scope change as an unsupported recurrence. |
| F3 · Move Design to 09:00 and keep Engineering at 08:00 | 9.7 s | 64.6 s | 6.7× | Kept both at 08:00 and repeated the same two-choice question after the user named Design. |

The saved hourly watch existed before F1. F2 inherited that watch from F1, so its filter could still have been narrowed even though F1 had failed. For F3, Nex had already offered both goal titles and received an explicit selection. All three turns selected the goal-management edit operation in both modes. In each case, the stored record showed no edit.

## Quality across Chat

| Workflow | Cases | Off · pass / partial / fail | On · pass / partial / fail |
| --- | ---: | ---: | ---: |
| Intent and one-time lookup | 6 | 4 / 2 / 0 | 4 / 2 / 0 |
| Creating goals | 12 | 11 / 1 / 0 | 5 / 4 / 3 |
| Editing, inspecting and managing goals | 16 | 12 / 1 / 3 | 11 / 0 / 5 |
| **All Chat** | **34** | **27 / 4 / 3** | **20 / 6 / 8** |

- **Two explanations improved.** Chat gave a workable example of a recruiter-email watch (**B4**). It also declined the unsupported weekday-only schedule without incorrectly saying that weekend checks were impossible (**C9**).
- **Three previously working goals were never created.** The physical-card check-in produced an invalid plan and no follow-up card (**C4**). Nex rejected supported weekly schedules for Monday’s calendar summary and Thursday’s recycling reminder (**C5, C10**).
- **Three correctly saved goals lost their confirmation.** The Harbor Housing watch, Victoria weather check and Northstar interview watch were active in storage, but each Chat request ended in failed status without a reply (**C2, C11, C12**).
- **Two edits lost their saved changes.** The Friday reschedule failed without changing the goal (**E2**). The unpaid-internship exclusion produced only a statement that Job leads was active (**E3**). The off run had persisted both changes, although its E3 reply was incomplete.
- **Two searches overstated matches.** Both modes identified the Northstar posting. With thinking enabled, Chat described all three returned emails as matches even though the fixture also contained a receipt and a networking invitation (**B2, C3**). C3 still saved the requested hourly watch.

## How much slower

| Chat latency | Thinking off | Thinking on | Increase |
| --- | ---: | ---: | ---: |
| Median | 17.5 s | 90.1 s | +72.5 s · 5.13× as long |
| 90th percentile | 28.9 s | 164.3 s | +135.5 s · 5.69× as long |
| Sum of the 34 measured turns | 10 min 31 s | 59 min 16 s | +48 min 45 s · 5.64× as long |

The 34 Chat turns used 195 model calls with thinking off and 196 with it on. All 196 thinking-on calls returned reasoning, and all finished normally at the inference endpoint. Some application-level plans or confirmations still failed. The time penalty occurred with almost the same number of calls. The 90th percentile uses the nearest-rank method.

## Scheduled Gmail worker

The worker passed **4/4 in both modes**. It delivered only the paid remote Northstar and Cedar Labs openings with their correct deadlines, suppressed repeat IDs, preserved its checkpoint through the outage and resumed the original hourly cadence after its five-minute retry. The four polls together took **45.4 seconds off and 281.5 seconds on** (6.20× as long). Median poll time rose from 10.1 to 59.5 seconds. Both runs used 16 model calls.

## What the comparison measured

The 34 typed Chat turns cover 30 conversations. The four worker polls use a separately seeded watch. Both runs used the fixed scenarios, synthetic messages, starting records and rubric from the previous goal evaluation. A pass requires the requested action or clarification, correct persisted state and an accurate reply. We checked revised fields, sibling goals, worker checkpoints and deferred check-in delivery. Chat timing runs from entry into Nex to its initial returned result, including planning, tool work and answer generation. The later processing of the physical-card follow-up is scored for correctness and excluded from both Chat latency measurements.

Excalibur ran the same Qwen 3.5 4B Q4_K_M weights in a single Vulkan inference slot at 12,288 tokens of context. The model file SHA-256 was `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`. Both runs used temperature zero. The thinking-on adapter allows up to 256 reasoning tokens per ordinary inference, plus room for the answer and delimiters. A synthetic request through the deployed Nex bridge verified the new token allocation. Low-latency standby classifications and warm-up calls keep their fast path. The evidence records whether a separate reasoning field appeared, completion token counts and finish reasons.

The thinking-off Chat reference is the recorded run on `c20502a`. The thinking-on benchmark used the same goal logic at `db1580b`. The Chat service and worker ran against disposable local archives. The remote model performed every inference. A separate minimal release at `985ff21`, based on Excalibur’s existing `8e479a0`, enabled thinking on the live host. The evaluation branch’s goal changes were not deployed. Native UI, real mail and device push were outside the test.

The original worker evidence has no wall-clock timings. The worker latency comparison uses a separately timed replay of the same four polls with thinking explicitly off on the same model. Their original grades remain the quality baseline. One observation per case and mode supports a comparison on this matrix only. Prompt caching, host load, and changes in the number of model calls can affect wall time.

## Verification and reproduction

The deployed revision passed Ruff, bytecode compilation and the Python 3.12 suite (508 tests run, six skipped). The goal evaluation revision passed the same checks (528 tests run, six skipped). The service health check and the deployed source revision were verified after the run. Thinking remains enabled on Excalibur.

From the evaluation checkout’s `server/` directory, the command below runs the same 38 cases with a private Unix socket forwarding to this Qwen model. Replace the socket path with the existing tunnel. The same command accepts `--mode off` for a new paired control.

```bash
PYTHONPATH=.:tests python3.12 tests/goal_audit/thinking.py /path/to/model.sock --mode on
```
