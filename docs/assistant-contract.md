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

## Review a change

Exercise the feature through its real entry points. Use differently worded requests with the same intent, a similar request with a different intent, a follow-up with prior context, and a denial or failure. In tests, assert that the model receives the actual user input and evidence. Use synthetic model output to verify exact transport when needed. Test the operational status separately from generated answer text.

For scheduled actions, also test retries and updates. Verify that missing model output cannot duplicate side effects, deliver a canned confirmation, or claim an action succeeded without a receipt. For voice, test audio and text outcomes separately and ensure locked surfaces never receive private content.
