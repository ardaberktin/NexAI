# Nex development instructions

## Nex owns assistant decisions and responses

Nex is an AI assistant. In every assistant workflow, give the model the user's request, relevant conversation and context, and the available tools. Nex decides whether to respond, what information to seek, which tools to use, whether to ask for clarification, and what to say. Return relevant tool results to the model so it can decide what to do next and compose a grounded response.

- Never hardcode assistant phrases, replies, confirmations, refusals, follow-up questions, summaries, or fallback answers. This applies to canned text and reply templates in source code, configuration, and prompts. Use prompts to describe the task, tools, and constraints and let Nex choose the wording.
- Never use hardcoded phrase lists, keyword matches, regular expressions, or request-specific branches to infer intent, choose a tool, or decide the outcome. Route natural-language requests through the model across Chat, live and recorded voice, and scheduled tasks.
- Keep authentication, permissions, privacy rules, explicit user controls, input validation, and tool execution in application code. Give the model structured outcomes for successful and failed tool calls. If the model is unavailable, surface the failure through app status. Keep fixed UI labels and operational status text in the UI.

When changing assistant behavior, check the normal and fallback paths for hardcoded replies and intent routing. Verify that differently worded requests reach the model and that its responses use the context and tool results available at runtime.

## Work in the monorepo

- Read [Architecture](docs/architecture.md) for entry points and data flow, then read [Assistant contract](docs/assistant-contract.md) before changing any assistant path.
- The Apple clients live in `apple/`. The Linux archive API, voice service, and deployment examples live in `server/`. Keep both components buildable from their own source archives.
- Preserve authenticated routes, privacy gates, recording recovery, and retry semantics. Update Swift decoding and Python endpoints together for wire-format changes. Keep compatibility with saved recordings and queued requests.
- Verify changed Python behavior with `python3.12 -m unittest discover -s tests -v` from `server/`. Compile relevant Apple schemes with Xcode and warnings enabled. Add regression coverage for new request routing, failure states, or migrations.
- Keep machine-specific archives, credentials, pairing files, device profiles, release binaries, and model weights out of Git. Use disposable data and synthetic fixtures for tests.
