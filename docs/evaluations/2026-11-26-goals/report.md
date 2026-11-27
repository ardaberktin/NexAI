# How well does Nex create and edit goals?

**November 26, 2026 · Typed Chat · Gemma 3 4B · America/Vancouver**

**Nex fully met the request in 10 of 34 Chat turns (29%).** Five were partial successes and 19 failed. A clear hourly job watch, a lease-renewal email watch and a one-time passport reminder worked. The most consequential failures were believable confirmations of changes that never happened. Nex promised ongoing monitoring after a one-time read, said it had paused an active watch, and acknowledged a filter it had never saved.

Once an hourly Gmail watch had the correct definition, the scheduled worker passed all four checks. It selected both qualifying openings across six distinct sample emails, ignored four distractors, suppressed duplicate delivery, and recovered after losing its connection.

The matrix contains the exact requests, starting context, expectations, observations and grades. The evidence includes actual replies, selected plans, provider queries where captured, stable fixture IDs and before/after changes. [Prompt matrix (CSV)](matrix.csv) · [Run evidence (JSONL)](evidence.jsonl)

## Scorecard

| Workflow on develop | Cases | Pass | Partial | Fail |
| --- | ---: | ---: | ---: | ---: |
| Interpreting intent and one-time lookups | 6 | 2 | 2 | 2 |
| Creating a goal | 12 | 3 | 0 | 9 |
| Editing, inspecting or managing existing goals, including follow-ups | 16 | 5 | 3 | 8 |
| **Chat total** | **34** | **10** | **5** | **19** |
| Scheduled Gmail worker, evaluated separately | 4 | 4 | 0 | 0 |

A **pass** required the intended action or clarification, the correct saved state, and an accurate reply. A **partial** delivered useful work or respected a requested boundary while missing a material detail. A **fail** missed the requested action, made an unauthorized or incorrect change, or claimed an operation that the evidence contradicted. For unsupported schedules, an honest explanation and a request to choose an alternative could pass. These purposely selected cases cover difficult boundaries. The percentages describe this matrix and do not estimate a rate across Nex users.

## The two mail requests

| User request | What Nex actually did |
| --- | --- |
| “I am waiting for a mail to come in.” | **Fail (B1).** With an empty goal list and no prior conversation, it saved an hourly Gmail watch for the subject “Refund Receipt.” Nothing identified email versus post, a refund, or a desired action. The same invented filter appeared on main. |
| “Check my emails for job postings.” | **Pass (B2).** It selected a one-time Gmail read, reported the Northstar remote software-engineer opening and its December 4 deadline, and omitted the subscription receipt and networking invitation. It created no goal. |
| “Any openings buried in my inbox? Just look this once.” | **Partial (B3).** It selected Gmail and summarized the supplied opening correctly. The actual provider query contained the literal words “topic or empty for recent mail.” A fixed candidate pool allowed the summary to succeed despite that query. |
| “Look through my emails for job postings now, and keep checking for new ones hourly.” | **Fail (C3).** The read succeeded. Nex then promised it would keep checking hourly. There was no goal tool call, saved goal, or next run. |

The account state also changed the answer. With a successful search returning zero matches, Nex accurately reported no matching items (B5). When the account adapter asked the user to reconnect Gmail, Nex answered “I’ve reconnected Gmail under Connected accounts in Nex Settings.” It had performed only a read (B6).

## Where creation broke down

- **The saved schedule could contradict the title and reply.** A request to check apartment listings every 30 minutes produced an hourly watch, initially due in 30 minutes (C8). A weekday internship watch became a single Friday run (C9). Weekly Thursday recycling became a one-off on Saturday, November 28, which the reply called Thursday (C10). A daily 07:30 umbrella check also became a one-off (C11). Checking only the first due time would miss three of these recurrence errors.
- **A physical delivery needs the user's observation.** For “Ask me tomorrow at 6 pm if [the replacement card] arrived,” Nex correctly created a personal check-in. The first Chat attempt returned a failed private answer with no saved time. In a fresh replay the request stayed working. Advancing the worker through four recorded retry times produced no follow-up card, due time or confirmation (C4). Later retries were outside the observation window.
- **The requested report could turn into an immediate read.** Every evening at 19:00, the user wanted an overdue TickTick list. Nex attempted immediate reads instead of saving the schedule (C6). A weekly calendar summary at 08:00 on Mondays ended with a model-unavailable status and no goal (C5). The exact cause of that planner status was not established.
- **An outbound restriction changed the goal itself.** A request to watch for a Northstar interview invitation and never send email produced a watch whose saved instructions said “do not notify” and introduced a specific subject the user had never supplied (C12). The evaluation verified that definition and the absence of an outbound call. Future delivery for this particular goal was not exercised.

## Where edits worked and where they went wrong

Explicitly naming the target helped. “Rename my ‘Job leads’ goal to ‘Career radar’” changed only the title and revision (E1). Moving Weekly planning to Friday at 16:00 preserved its existing weekly cadence and calendar task (E2). Completing Card delivery canceled its future run (E6). Deleting Rent listings preserved Job leads (E7). Adding and removing PDF copies changed only Weekly planning’s output setting and revision. Removal passed (E5). Addition received partial credit because the reply referred to “this PDF” during a settings change with no document attached (E4).

| Condition | Observed consequence |
| --- | --- |
| Follow up immediately after creating one watch | “Check it every 15 minutes instead” elicited a question about which item. The hourly interval survived (F1). “Only tell me about remote engineering roles” elicited another question, and the broad job filter survived (F2). F2 followed the failed cadence edit. Its independent failure was leaving the filter unchanged. |
| Edit a named watch's filter | Nex looked up archive text and promised to exclude unpaid internships. The Job leads prompt and revision stayed unchanged (E3). |
| Pause an active watch | Nex confirmed a pause and gave a future resume notification time. The stored status was still active, with the original next run (E10). |
| Two targets plausibly match | Nex chose the budget summary over the calendar summary without asking (E8). With Design job watch and Engineering job watch both at 08:00, it moved Engineering to 09:00. After the user explicitly selected Design and asked to keep Engineering at 08:00, Nex claimed a correction. Neither record changed on that turn (E13, F3). |
| Report progress on an existing goal | “The replacement bank card hasn’t arrived yet. Keep my Card delivery goal open” created a second physical-card check-in. The named goal was already active (E12). |
| Ask about a missing or existing goal | Requesting a new time for a nonexistent gym check-in made no change, but the reply offered to confirm its current time (E9). Asking what Job leads checked preserved the goal but only returned “It checks the current job leads,” omitting the source and filter (E11). |

## Scheduled monitoring

The worker started with a separately seeded, active hourly Gmail goal for **paid, remote software-engineering openings**, excluding on-site and unpaid roles. This isolates delivery quality from the failures observed while creating or editing a goal.

| Poll | Input | Verified result |
| --- | --- | --- |
| S1 | Paid remote Northstar role, on-site role, unpaid remote internship, subscription receipt | One update for Northstar with the correct December 4 deadline. Only Northstar's fingerprint was recorded as a match. |
| S2 | The same four IDs again | Zero messages fetched again and zero new updates. |
| S3 | Gmail unavailable with a new Cedar Labs message waiting | Connection error recorded, previous successful checkpoint preserved, retry scheduled five minutes later. |
| S4 | Reconnected provider, original Cedar Labs message and a networking social | One update for Cedar's salaried “work from anywhere” opening with the correct December 8 deadline. The error cleared and hourly polling resumed. |

## What to fix first

1. **Ground confirmations in the actual state transition.** Compare the requested effect with the committed fields and surface the outcome to the model. A Gmail read cannot confirm reconnection or establish a future watch. An edit receipt should distinguish a changed filter, status, or schedule from an unchanged goal. For a compound request, confirm the immediate search and the future watch independently. Acceptance cases are B6, C3, E3, E10 and F3. The goal-management path currently constructs an “updated” receipt after saving an edit, including a save that returns the existing record. [Goal management](../../../server/nex/conversation/goal_management.py) · [Goal persistence](../../../server/nex/prompt_goals.py)
2. **Validate the schedule the user will actually experience.** Give the model the representable recurrence choices and a structured description of the proposed next two occurrences. Verify weekday, local time and repeat interval against the request. When they cannot be represented, ask before saving a substitute. The scheduler currently supports one-off, 15-minute, hourly, daily and weekly intervals. Use C8–C11 to test both unsupported schedules and mishandled supported schedules. [Goal scheduling](../../../server/nex/goal_schedule.py)
3. **Resolve the target before writing.** Supply the just-created goal and competing candidates with their titles, filters and current schedules. A genuinely ambiguous target should lead to a focused clarification. Verify the selected ID, the final fields and every sibling on the follow-up. Cover implied updates (“still waiting”), pronouns and explicit corrections. Pause and resume already exist in the goal store and UI. The Chat management operations omit them. [Supported Chat operations](../../../server/nex/conversation/goal_management_prompts.py) · [Goal actions](../../../server/nex/goals.py) · [Goal detail controls](../../../apple/Nex/GoalDetailView.swift)
4. **Keep examples and user constraints out of invented filters.** Require a sender, subject, literal phrase or notification restriction in the saved task to be grounded in the request or conversation. The mail-planning prompt includes a refund-receipt example, a plausible source for the specific invention in B1. That causal explanation remains a hypothesis. Use B1, B3 and C12 as acceptance cases. [Mail-watch planner](../../../server/nex/conversation/mail_watch_plan.py)

## Method and reproduction

The primary source was the active local `develop` checkout at `a71eb841b6f990ce0142cbefb0b90a2ad883714e`. Six deliberately selected prompts also ran on local `main` at `f076feb07da2c7c65b7f8de728689633b55a54a5`. Both branches passed the straightforward one-time job search and failed the vague mail, disconnected Gmail, compound watch, 30-minute cadence and filter-edit cases. On main the 30-minute request became a one-time inbox read. Six targeted replays do not establish relative branch quality.

Each conversation started with a fresh SQLite archive and an unlocked synthetic Mac session. Follow-up turns used Nex’s own history and real previous replies. The harness called the production typed Chat service with its normal planner, privacy handoff, tool executor and reply composer. It captured the selected plan and compared goal IDs, titles, prompts, provider bindings, status, revision, PDF settings and schedule fields. The scheduled checks advanced Nex’s worker to the actual persisted due time in the disposable archive.

Inference used a local llama.cpp b12359 server with the deployment-pinned Gemma 3 4B IT QAT Q4_0 weights, a 12,288-token context, and temperature zero. The SHA-256 matched the deployment recipe. English prompts used Vancouver local time. The deployed host's active model selection, native Apple UI, voice input, real Gmail search ranking, device push and wall-clock delivery latency were outside the test. [Model staging recipe](../../../server/deploy/stage-gemma.sh)

The report grades one observation per Chat case. Five Gmail-reading cases were rechecked through the real account adapter with synthetic provider responses. The matrix uses those adapter runs. The successful empty-search reply improved from an overbroad empty-inbox claim when the production adapter supplied its actual “no matching items” outcome. Every other Chat case uses its first recorded observation. The six main comparisons, five lower-fidelity preliminary mail reads, and two deferred check-in replays are retained separately in the 51-record evidence file. Multiple requests shared one inference slot for part of the run, so timings should not serve as a speed benchmark.

To reproduce, use the server test dependencies and start a disposable llama-server with the pinned weights and context above. From `server/` in the source checkout being evaluated, set `audit_dir` to this audit’s `server/tests/goal_audit` directory and `model_socket` to that server’s Unix socket. The runners use temporary archives, synthetic provider data and local-only model access.

```bash
PYTHONPATH=.:tests python3.12 "$audit_dir/run.py" "$model_socket"
PYTHONPATH=.:tests python3.12 "$audit_dir/run.py" "$model_socket" C4 --follow-up-worker
PYTHONPATH=.:tests python3.12 "$audit_dir/scheduled.py" "$model_socket"
```

The develop server suite ran 506 tests with six skips. The main-based report branch ran 475 with six skips. Both suites passed. Ruff passed on both trees, and the evaluation scripts compiled. These checks validate the code paths independently of the real-model scores above. [Cases](../../../server/tests/goal_audit/cases.py) · [Chat runner](../../../server/tests/goal_audit/run.py) · [Worker runner](../../../server/tests/goal_audit/scheduled.py)
