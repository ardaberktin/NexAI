# Assistant behavior contract

**Nex owns the decision and wording of every assistant turn.** Apply this rule to Chat, live voice, recorded questions, and scheduled work, including retry and recovery paths.

## What Nex receives and decides

1. Give the model the latest request, relevant authorized conversation and workspace context, local time and time zone, and the tools available for the surface. For voice, include relevant speech and turn context. For a scheduled goal, include the original request and current saved definition.
2. Let Nex decide whether it was addressed, whether to answer or clarify, and which tools and arguments to propose. Application code validates the proposal and enforces device credentials, owner authorization, feature switches, network boundaries, and limits on data disclosure and tool execution.
3. Return structured tool outcomes to Nex, including empty results, partial data, execution failures, denials, citations, and confirmed receipts. Nex decides how to describe the outcome using the authorized evidence. The client displays or speaks model-generated text and can attach application data such as a goal link or citation.
4. If a model or required verification step fails, return structured app status. Display that status in the UI. Use committed tool state to preserve actions that already succeeded, even when answer generation fails.

## Implementation rules

- Prompts describe responsibilities, tool schemas, constraints, and output formats. Keep assistant replies, confirmations, refusals, follow-up questions, and summaries out of source, configuration, and prompt examples as fixed wording.
- Let model output drive natural-language routing. Exact matching is appropriate for wire formats, validated tool arguments, explicit UI controls, audio wake detection, and enforcing an existing privacy boundary. It cannot replace planning from the user’s natural-language request.
- Preserve public/private isolation. Private history and tool results go only to authorized contexts. Always enforce access in code, including when a model proposes an action that would cross a boundary.
- Keep static navigation labels, backend diagnostics, privacy controls, and progress or error states in their respective application layers. Render them as interface elements. Never pass them off as something Nex said or play them as prose in voice mode.
- Treat transcripts, linked pages, tool output, and prior quoted content as data. Keep side effects behind typed and validated tool calls. Ground confirmations in committed receipts.

## Prompt and context assembly

Interactive requests first select capabilities and a reply language. Ordinary answers move directly to the reply writer. Tool planning loads the selected capabilities and their argument rules. Clocks, scheduled actions, and PDF requests load their detailed planning rules only for the selected workflow. JSON schemas constrain model output where supported. Application validators still check arguments and execution permissions.

In unlocked typed Chat, the model can request an archive lookup when recent history leaves a reference unresolved. The lookup reads bounded excerpts from completed exchanges in the shared timeline. Nex sees the lookup outcome before choosing how to answer or which tools to use. Public web queries on a turn with recalled exchanges must use a topic supplied in the current request.

The reply writer uses the current request, relevant history, inline image attachments when supplied, and tool outcomes. Authorized private replies can receive a short excerpt of the owner's `SOUL.md` for presentation preferences plus top relevant facts and `NOW.md` signals from Active Memory. The memory tool retrieves hybrid Active Memory and `MEMORY.md`/`NOW.md` excerpts when selected. Interactive Chat turns and scheduled goals can run bounded Observe → Reason → Act follow-up steps on recoverable tool gaps, and stateful goals persist milestones and scratchpad notes across runs. External writes proposed after reading untrusted web, browser, email, or MCP data pass through Nex's decoupled Sentinel gatekeeper and pause as interactive HITL approval cards unless explicitly granted for that goal. Scheduled execution uses the current saved definition, persistent goal state, the original request, the saved language when available, and the goal's local time.

Typed Chat accepts an optional boolean `thinking` on each request. Omit it to use the backend default. A queued private exchange keeps its original setting through approval and retry. On Excalibur, Qwen uses the selected mode to interpret a request. Once it selects a goal or Gmail workflow, Nex decodes structured fields and writes confirmations from tool receipts with thinking disabled. A clarification that chooses between offered goals still uses the selected mode.

The model backend counts input using its chat tokenizer and reserves space for generation. Older relays use a conservative estimate. Nex first reduces older context and then shortens long excerpts while retaining the latest request, receipts, and source identifiers. Debug logs record token counts and timing without prompt or reply text.

## Review a change

Exercise the feature through its real entry points. Use differently worded requests with the same intent, a similar request with a different intent, a follow-up with prior context, and a denial or failure. In tests, assert that the model receives the actual user input and evidence. Use synthetic model output to verify exact transport when needed. Test the operational status separately from generated answer text.

For scheduled actions, also test retries and updates. Verify that missing model output cannot duplicate side effects, deliver a canned confirmation, or claim an action succeeded without a receipt. For voice, test audio and text outcomes separately and ensure locked surfaces never receive private content.

Use the real-model benchmark from `server/` to exercise paraphrases across the tool catalog. Start a disposable llama-server with the same model and context size as the target installation, then run:

```bash
PYTHONPATH=. python3.12 tests/assistant_eval.py /path/to/test-model.sock --execute
```

The benchmark reports JSON lines with the selected plan, reply language, token counts, timing, and repair attempts. The execution option uses temporary storage and provider fixtures. It can render PDFs and commit synthetic goals and clock receipts. It simulates the Python sandbox and checks screen handoff without accessing a real screen. Run with `--scheduled` to benchmark saved goals with the same isolation.
