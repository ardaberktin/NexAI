# Goal creation and editing with Excalibur’s Qwen 3.5 4B

**November 26, 2026 · Typed Chat · Live Excalibur inference · America/Vancouver**

**Qwen fully met the request in 10 of 34 Chat turns (29%), with 8 partial successes and 16 failures.** Gemma also passed 10, with 5 partial successes and 19 failures. Eight turns passed on both. Qwen gained full passes on the daily TickTick goal and enabling PDF copies, and lost full passes on removing those copies by follow-up and deleting a named goal. The model change did not improve the full-pass count on this matrix.

| Workflow | Qwen pass | Qwen partial | Qwen fail | Gemma pass |
| --- | ---: | ---: | ---: | ---: |
| Interpreting intent and one-time lookups | 2 | 2 | 2 | 2 / 6 |
| Creating goals | 4 | 3 | 5 | 3 / 12 |
| Editing, inspecting or managing goals | 4 | 3 | 9 | 5 / 16 |
| **Chat total** | **10** | **8** | **16** | **10 / 34** |
| Scheduled Gmail worker, evaluated separately | 4 | 0 | 0 | 4 / 4 |

## The two mail requests

- **“I am waiting for a mail to come in.” (B1)** Qwen saved a personal check-in to ask whether the mail had arrived. The private request showed “working” and the new goal had no due time when Chat returned. It never established whether “mail” meant email or postal mail, or whether the user wanted any ongoing action. Gemma had invented a refund-receipt email watch on the same prompt.
- **“Check my emails for job postings.” (B2, B5, B6)** With mixed results, Qwen reported only the Northstar remote software-engineer opening and its December 4 deadline. With a successful empty search, it correctly reported no job postings. With Gmail disconnected, it gave essentially the same empty result, even though the provider had asked the user to reconnect and had supplied no search results.
- **“Look through my emails for job postings now, and keep checking for new ones hourly.” (C3)** Qwen returned the posting and explicitly said it could not set up the hourly check. Only a Gmail read ran. In C1, asking for the hourly watch by itself saved the correct goal. The limitation appeared when the two tasks were requested together.

## What mattered in the saved records

| User intent | What the Qwen run showed |
| --- | --- |
| Set a recurrence | The 30-minute apartment watch became a **15-minute stored interval** and a **daily** check in the reply (C8). The weekday internship check became a one-off (C9). The weekly Monday calendar summary was saved with a Friday start, so it would recur on Fridays (C5). |
| Edit a watch just created in Chat | Qwen called the explicit 15-minute cadence unclear (F1). The remote-engineering-only follow-up routed to `save_memory` (F2). The job watch stayed hourly with its original broad filter. |
| Change or inspect a named goal | Renaming Job leads, moving Weekly planning to Friday at 16:00 and marking Card delivery complete all changed the correct record (E1, E2, E6). The request to exclude unpaid internships selected no tool, yet Qwen said the settings were updated (E3). |
| Clarify which goal to change | With two daily job watches at 08:00, Qwen moved Engineering to 09:00 without asking which one the user meant. The user then named Design and asked to keep Engineering at 08:00. No further change occurred. Both watches ended on the opposite schedule from the one the user requested (E13, F3). |
| Keep an existing goal open | Reporting that the replacement card had not arrived created an additional check-in (E12). |
| Save and confirm the same operation | The daily 07:30 Victoria umbrella goal was stored correctly, but Chat returned a failed private result with no confirmation (C11). This is the reverse of the unpaid-internship case, where a confirmation was returned without a saved change. |
| Ask about a physical delivery at a specific time | Qwen correctly stored tomorrow at 18:00 for the requested bank-card check-in. After four deferred attempts there was still no follow-up card or confirmation, and the private request still showed “working” (C4). Later retries were outside the test window. |

The first fixes to verify with Qwen are to ground every confirmation in the persisted change, validate the first two local occurrences against the requested schedule, and use the live goal ID or ask which goal to edit before making a change. Add a disconnected-provider check that distinguishes unavailable evidence from an empty search. The matrix supplies acceptance cases for each.

## How a saved watch performed

With a correctly configured hourly Gmail watch, the worker passed all four polls. Out of six distinct fixture messages, it alerted on the two qualifying paid remote engineering openings and excluded four distractors. The Northstar and Cedar Labs updates included the correct December 4 and December 8 deadlines. Replaying the same four IDs produced no duplicate. During a simulated outage the worker preserved the last successful checkpoint, scheduled a five-minute retry, and delivered the missed match after reconnection. The worker started with a known goal definition, independently of Chat’s creation results (S1–S4).

## What ran

Excalibur served every inference request in this evaluation. Its running llama-server process and its model metadata both identified `Qwen3.5-4B-Q4_K_M.gguf`, with a 12,288-token context and one inference slot. The file’s SHA-256 was `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`, matching the deployment recipe at `8e479a0:server/deploy/stage-qwen-3.5.sh`. The requests used temperature zero and disabled thinking, as Nex does for typed Chat.

The Chat service, scheduler and disposable SQLite archives ran locally. All 219 installed Nex Python files on Excalibur matched the local source by content and relative path. The deployed release was `8e479a0`, and its complete server Git tree matched the tested checkout at `a71eb84`. The private tunnel carried synthetic model input to Excalibur’s inference socket. Inbox candidates, account failures and goal records were fixtures. The test exercised the server-side behavior behind typed Chat and scheduled goals. Native UI, device push, real Gmail search ranking, and production account data were outside its scope.

The same 34 Chat turns, four worker polls and grading standard were used for Qwen and Gemma. Each multi-turn conversation shared its own history and goal store. The grade considers the plan, persisted state, user-facing answer and effects on sibling goals. Provider queries are visible even when the fixed inbox fixture happens to return a useful message. Each row is one targeted example, and the rates describe this matrix. [Paired prompt matrix](qwen-matrix.csv) · [Qwen evidence](qwen-evidence.jsonl) · [Gemma baseline](report.md)
