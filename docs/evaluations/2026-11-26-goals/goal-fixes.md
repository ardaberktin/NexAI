# Goal creation and editing on Excalibur’s Qwen 3.5 4B

**November 26, 2026 · Before and after the Nex server changes · Tested revision `c20502a`**

**Chat passed 27 of 34 turns (79%), up from 10 of 34 (29%) on the same model.** Nineteen turns improved in grade, fifteen kept their grade, and none regressed. Goal creation improved from 4 to 11 full passes out of 12. All three remaining failures were follow-up edits, including one where the user had already answered Nex’s clarification question. [Full 38-case matrix](qwen-after-matrix.csv) · [Tool calls, saved records and replies](qwen-after-evidence.jsonl) · [Original Qwen report](qwen.md)

| Workflow | Cases | Before · pass / partial / fail | After · pass / partial / fail |
| --- | ---: | ---: | ---: |
| Intent and one-time lookup | 6 | 2 / 2 / 2 | 4 / 2 / 0 |
| Creating a goal | 12 | 4 / 3 / 5 | 11 / 1 / 0 |
| Editing, inspecting or managing a goal | 16 | 4 / 3 / 9 | 12 / 1 / 3 |
| **All Chat turns** | **34** | **10 / 8 / 16** | **27 / 4 / 3** |
| Scheduled Gmail worker, scored separately | 4 | 4 / 0 / 0 | 4 / 0 / 0 |

## What improved for the user

- **Waiting for mail.** “I am waiting for a mail to come in” previously created an unsolicited check-in. This time Nex asked which email the user expected and saved nothing. It still assumed email was likely. When the user explicitly requested an alert for a Harbor Housing lease renewal, Nex saved an hourly watch with both the sender and subject and confirmed it. **B1, C2**
- **Looking for work.** “Check my emails for job postings” found the Northstar opening and its December 4 deadline. An empty search and a disconnected account now produced distinct answers. When the user asked for both a search now and hourly monitoring, Nex returned the opening and saved the watch. A request to watch for a Northstar interview invitation also kept the instruction against sending email. **B2, B5, B6, C3, C12**
- **Getting the timing right.** The Monday 08:00 calendar summary and Thursday 19:00 recycling reminder were saved with the requested weekday, start time and weekly interval. Nex explained that the 30-minute apartment watch was unsupported and asked the user to choose. The physical bank-card check-in produced an answerable card due at 18:00 the next day on its first deferred assessment. The baseline produced no card after four attempts. **C4, C5, C8, C10**
- **Editing the intended record.** Nex enabled and then removed PDF attachments on Weekly planning while preserving the task and schedule. It deleted Rent listings while keeping Job leads, paused Job leads when asked, accurately described its filter on inspection, and kept an undelivered card’s goal open without duplicating it. When two job watches could have matched, it asked which watch to move before making a change. **E4–E5, E7, E10–E13**
- **Delivering watch updates.** With a correctly configured saved watch, the worker again alerted only on the Northstar and Cedar Labs paid remote openings and reported the correct deadlines. It skipped repeated IDs without fetching them again, preserved its checkpoint during a simulated outage, then delivered the missed match and resumed the original hourly schedule. **S1–S4**

## What still fails

| Case | What the user asked | What persisted and why |
| --- | --- | --- |
| F1 | Change the hourly watch to every 15 minutes. | The edit planner produced 15 minutes, but the schedule extractor returned four hours. Nex left the watch hourly. The reply called 15 minutes unsupported while listing it as a supported option. |
| F2 | Narrow that same watch to remote engineering roles. | Qwen selected the right watch and proposed the right filter. Its edit verifier asked for more context three times. The filter stayed broad, and the reply acknowledged that it had not been saved. |
| F3 | Move Design to 09:00 and keep Engineering at 08:00. | Nex had correctly asked which goal the user meant. It then assigned the words “The design one” to both candidate goals and asked the same question again. Both watches stayed at 08:00. |

F1 and F2 had passed on an intermediate build. Both failed when the full matrix ran on the final revision. F3 preserved the sibling this time, but the requested edit still did not happen.

Four turns earned partial credit. The one-time “openings buried in my inbox” search found the fixture posting using only `in:inbox`, which could miss older relevant mail (**B3**). Setup guidance suggested a weekday watch with an end date that Nex cannot represent (**B4**). Nex declined to save a weekday-only internship watch, but incorrectly told the user that weekend checks were unsupported (**C9**). The unpaid-internship exclusion was correctly saved on Job leads, but the reply described an intention to edit and never confirmed completion (**E3**).

## How Nex changed

Qwen now makes narrower decisions about future action, goal identity, editable settings and scheduling. It receives the request, conversation and relevant saved goal data. For an untimed watch or a proposed check-in, it can be required to cite the user’s words authorizing future action. It can ask the user to select between saved goals and carry their IDs into the next turn. These decisions use model outputs and typed fields without matching user phrases or replaying answers from the matrix.

Nex converts extracted quantities, weekdays and local times into scheduler values. It checks whether a single-weekday request fits a weekly recurrence, and can return unsupported schedules to the user before saving. Edits use the selected goal’s revision, apply the selected fields and expose the persisted changes to the model writing the confirmation. PDF delivery has its own saved setting. A standalone Gmail watch retains the user’s original constraints, and the check-in worker can build a follow-up card from the saved time.

## How the comparison ran

The same Excalibur inference endpoint served the original run and this rerun. It reported `Qwen3.5-4B-Q4_K_M.gguf` with a 12,288-token context. The model file’s verified SHA-256 was `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`. The baseline server tree matched deployed revision `8e479a0`. The candidate at `c20502a` includes the preceding Gmail search and watch changes as well as the goal fixes. Typed Chat used the production model adapter at temperature zero with thinking disabled.

The candidate Chat service and workers ran locally against disposable archives and a synthetic Gmail provider. All 34 Chat turns across 30 conversations used the original prompts, starting goals, mail fixtures and expected outcomes. Follow-ups shared their conversation’s history and records. One fixture signature was updated to forward the Gmail reader’s new argument for empty results. The four worker polls began with a known watch definition and were independent of Chat’s creation results. Native UI, device push delivery and real Gmail ranking were outside the test. The running Nex installation was not updated.

A full pass required the correct action or clarification, the correct saved state and an accurate user-facing reply. A useful action with a material omission earned partial credit. Each case ran once on the frozen candidate. These prompts were used while developing the fixes, so the rates describe this regression set. Median observed Chat response time rose from 14.1 to 17.5 seconds. Median model calls stayed at five, while total Chat model calls rose from 167 to 195. These timings exclude deferred worker processing and are single-run observations.

Server verification ran 526 Python 3.12 tests successfully with six skipped. Ruff, Python bytecode compilation and shell syntax checks for the deployment scripts passed.
