# Nex development instructions

## Nex owns assistant decisions and responses

Nex is an AI assistant. In every assistant workflow, give the model the user's request, relevant conversation and context, and the available tools. Nex decides whether to respond, what information to seek, which tools to use, whether to ask for clarification, and what to say. Return relevant tool results to the model so it can decide what to do next and compose a grounded response.

- Never hardcode assistant phrases, replies, confirmations, refusals, follow-up questions, summaries, or fallback answers. This applies to canned text and reply templates in source code, configuration, and prompts. Use prompts to describe the task, tools, and constraints and let Nex choose the wording.
- Never use hardcoded phrase lists, keyword matches, regular expressions, or request-specific branches to infer intent, choose a tool, or decide the outcome. Route natural-language requests through the model across Chat, live and recorded voice, and scheduled tasks.
- Keep authentication, permissions, privacy rules, explicit user controls, input validation, and tool execution in application code. Give the model structured outcomes for successful and failed tool calls. If the model is unavailable, surface the failure through app status. Keep fixed UI labels and operational status text in the UI.

When changing assistant behavior, check the normal and fallback paths for hardcoded replies and intent routing. Verify that differently worded requests reach the model and that its responses use the context and tool results available at runtime.

## Manage branches and local work

- Start a change with `git status --short --branch`. Inspect the current branch, its upstream, and any staged or unstaged edits. Use a branch the user has designated for the task. Otherwise, reuse a branch already dedicated to the task or create a `codex/<short-task-name>` branch from the current `main` base. Refresh the upstream ref when the task depends on the latest remote state.
- Keep `main` ready to release. If unrelated edits or another task are in progress, use a separate worktree for the new task. Preserve the user's files, staged changes, and commit history. Ask before discarding changes, rewriting commits the user may rely on, or removing a branch or worktree that may contain work.
- Review the diff and run the relevant checks from the same working directory and with the same configuration as CI. After verification, make a focused local commit on the task branch unless the user wants uncommitted changes. Include only changes belonging to the task. Leave the branch available for review and report its name, commit, check results, and any remaining work.
- Treat pushes, pull requests, and merges into `main` as separate steps. Proceed when the user has authorized the specific step. Prepare releases from a reviewed commit with passing CI and package both source downloads from its version tag. Follow the [release guide](docs/releases.md).
- Keep development builds and test data isolated from the user's running Nex installation. Create or publish release tags and GitHub Releases, deploy to Excalibur, or install builds on the user's devices only with their authorization. For an approved deployment, record the source commit, release tag when available, Apple build numbers, and the previous working version in the private deployment notes.

## Work in the monorepo

- Read [Architecture](docs/architecture.md) for entry points and data flow, then read [Assistant contract](docs/assistant-contract.md) before changing any assistant path.
- The Apple clients live in `apple/`. The Linux archive API, voice service, and deployment examples live in `server/`. Keep both components buildable from their own source archives.
- Preserve authenticated routes, privacy gates, recording recovery, and retry semantics. Update Swift decoding and Python endpoints together for wire-format changes. Keep compatibility with saved recordings and queued requests.
- Verify changed Python behavior with `python3.12 -m unittest discover -s tests -v` from `server/`. Compile relevant Apple schemes with Xcode and warnings enabled. Add regression coverage for new request routing, failure states, or migrations.
- Keep machine-specific archives, credentials, pairing files, device profiles, release binaries, and model weights out of Git. Use disposable data and synthetic fixtures for tests.
