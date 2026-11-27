# Qwen thinking and Nex goal reliability

**Excalibur · Qwen 3.5 4B Q4_K_M · November 27, 2026 · code `5f8f5af`**

**All three follow-up edits passed with thinking both on and off.** For all 34 Chat turns, the two modes produced the same grade, selected identical tool plans, committed identical goal field changes and ended with the same Chat status. Each scored 33 passes, one partial and zero failures. Before the fix, thinking on passed 20 and thinking off passed 27. All four scheduled worker cases passed in both modes.

[Full 38-case comparison, prompts, grades, replies and timings](matrix.csv) · [Paired tool, storage and inference evidence](evidence.jsonl) · [Previous thinking comparison](../2026-11-26-goals/qwen-thinking.md)

## Accuracy against the original prompts

Counts are **pass / partial / fail**. A pass requires the appropriate action or clarification, correct saved state and an accurate user-facing reply.

| Workflow | Cases | Previous off | Fixed off | Previous on | Fixed on |
| --- | ---: | ---: | ---: | ---: | ---: |
| Intent and one-time lookup | 6 | 4/2/0 | 5/1/0 | 4/2/0 | 5/1/0 |
| Goal creation | 12 | 11/1/0 | 12/0/0 | 5/4/3 | 12/0/0 |
| Goal management and follow-ups | 16 | 12/1/3 | 16/0/0 | 11/0/5 | 16/0/0 |
| All Chat | 34 | 27/4/3 | 33/1/0 | 20/6/8 | 33/1/0 |
| Scheduled Gmail worker | 4 | 4/0/0 | 4/0/0 | 4/0/0 | 4/0/0 |

The thinking-on run recovered the Monday calendar summary, Thursday recycling reminder and physical-card check-in. It also confirmed the Harbor Housing, Victoria weather and Northstar interview watches after saving them. The Friday reschedule and unpaid-internship filter both produced the requested database change and a confirmation. In both modes, an empty job search was described as zero matching messages, and a disconnected Gmail account produced a connection error.

The worker delivered only the paid remote Northstar and Cedar Labs roles. It suppressed the repeated batch, kept its checkpoint during the outage, retried after five minutes and resumed the hourly schedule.

## The three follow-up edits

All three failed in both previous runs. F1 and F2 are successive edits to a watch that the conversation itself created. F3 follows a clarification between two seeded watches.

| Request | Observed result | Off | On |
| --- | --- | ---: | ---: |
| F1 · Change the hourly job watch to 15 minutes | The original Gmail watch advanced one revision. Its interval changed from 60 to 15 minutes. Other saved settings were preserved. | 20.34 s | 34.69 s |
| F2 · Only report remote engineering jobs | Narrowed the same watch to remote engineering openings. The Gmail binding and 15-minute cadence were preserved. | 21.89 s | 36.16 s |
| F3 · Move Design to 09:00 and keep Engineering at 08:00 | After the user selected Design, moved only that watch to 09:00 daily. Engineering stayed at 08:00 and both filters were preserved. | 21.92 s | 49.12 s |

## What changed in Nex

- Typed Chat now accepts an optional boolean `thinking`. The original value follows a private request through approval and replay. Omission preserves the backend default. The archive bridge honors an explicit off value for both streamed and nonstreamed Qwen responses.
- The model interprets the request with the selected setting. After it chooses a goal or Gmail workflow, the constrained tool arguments and receipt-based confirmations use direct generation. An explicit choice between ambiguous goals still goes through the selected thinking mode. This keeps Qwen involved in the decisions while reducing repeated judgments on an already selected action.
- For edits, Qwen selects the permitted fields and quotes the user's authorization from the current turn or the pending clarification. Nex validates the proposal, applies the patch to the selected record and writes the confirmation from the committed field changes. A historical title such as “Every hour…” can no longer be presented to the receipt writer as evidence about the new interval. Lifecycle, deletion, privacy and conflict checks stay in the execution path.
- The Gmail planner supplies a search topic and alternative terms, including an explicit empty scope for general inbox reads. An empty filtered result reports the matching count and identifies the size of the whole inbox as unknown. For unsupported schedules, the receipt supplies the exact allowed intervals and tells the model that the user needs to choose an alternative.

## Latency

| End-to-end measurement | Thinking off | Thinking on | Increase with thinking |
| --- | ---: | ---: | ---: |
| Median Chat turn | 17.3 s | 37.4 s | +20.1 s · 2.16× |
| 90th-percentile Chat turn | 27.95 s | 50.92 s | +22.97 s · 1.82× |
| All 34 Chat turns | 10m 25s | 21m 29s | 11m 04s · 2.06× |
| All four worker polls | 0m 56s | 4m 36s | 3m 40s · 4.93× |

In the earlier run, median Chat latency was 17.5 seconds off and 90.1 seconds on. In this run, the endpoint returned reasoning on **44 of 186** primary Chat calls with thinking on and **0 of 187** with it off. The earlier on run used reasoning on all 196 primary Chat calls. The worker's on run used reasoning on 16 of 16 calls. The deferred physical-card assessment is scored separately from initial Chat latency. The 90th percentile uses nearest rank.

## Remaining limitations

- **Deep inbox search (B3) was partial in both modes.** Qwen identified the Northstar job from the three supplied messages. The actual provider query was only `in:inbox`. An ordinary unfiltered read requests up to eight messages, so an older posting could be missed. The more explicit “Check my emails for job postings” did generate topical search terms.
- **F1 changes the interval but preserves the already scheduled first poll.** The watch was created at 21:40 with a first poll at 22:40. Both modes saved a 15-minute interval and retained 22:40 as the next scheduled run. The matrix grades the requested cadence edit and reports this first-run behavior separately.
- **Wording can vary.** Both physical-card check-ins asked the same question at the same time and offered an arrived/pending choice. The button text differed. Both modes also assumed the ambiguous word “mail” in B1 meant email, but asked which message the user expected and took no action.

## Test setup

The same 34 Chat turns across 30 conversations and four scheduled polls ran through Nex with a disposable archive, unlocked synthetic workspace and fixture mail. Both final runs used commit `5f8f5af` and the same Qwen weights on Excalibur, one inference slot, 12,288 tokens of context, temperature zero and up to 256 reasoning tokens per eligible request. Chat passed the setting through the production conversation entry point and private handoff. The test adapter called the real inference endpoint through a private Unix socket. Workers selected the mode independently. Each scenario used the same frozen wall time, November 26 at 21:40 (UTC−07:00), with real monotonic timing. The off run finished before the on run began.

The matrix compares goal field changes and worker matches, retry times and checkpoints. For C4 it also compares the saved follow-up card excluding the generated card ID. Generated replies, search parameters and confirmation status were reviewed separately. The provider fixture supplies a fixed pool of synthetic messages, so real Gmail retrieval, device UI and push delivery remain unmeasured. One observation per case and mode supports conclusions on this matrix. Token traces retain only visible results, reasoning presence, budgets and timings. The test output excludes private reasoning text.

## Reproduction

Run from `server/` with the evaluation dependencies, `time-machine==3.6.0` and an existing private socket forwarding to the model's HTTP endpoint. Keep the model slot free between runs.

```sh
for mode in off on; do
  PYTHONPATH=.:tests python3.12 tests/goal_audit/thinking.py /path/to/model.sock \
    --mode "$mode" --at '2026-11-26T21:40:00-07:00' > "goals-$mode.jsonl"
done
```

Python 3.12 ran 532 server tests with six skips and zero failures. Ruff, Python compilation and `git diff --check` passed. These changes are on the local evaluation branch. Excalibur supplied inference only.
