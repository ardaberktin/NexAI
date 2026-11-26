# Nex architecture

One Linux Nex host serves multiple accounts. Each account has its own archive and can connect a Mac and an iPhone. Apple Watch uses the iPhone as its relay. The server stores the archives and executes tools. The Apple apps own capture, presentation, and device-local permissions.

## Runtime topology

```text
Mac and iPhone ── pinned HTTPS :8765 ── Nex archive API ── SQLite / FTS5 and audio
              └── pinned live :8767 ── Nex voice service ── local model and speech runtimes
Apple Watch ── WatchConnectivity ── iPhone
Admin browser ── SSH tunnel ── server loopback console :8766
Setup browser ── SSH tunnel ── private server setup.sock
```

The production app listeners bind to the Tailscale IPv4 address and Linux interface. The server link supplies the certificate fingerprint. Account sign-in issues a revocable device credential, and a separate biometric challenge-response unlocks private workspace routes. Recorded voice identification supplies a speaker label. Optional integration calls go to their providers when tools run.

## Request flow

| Surface | Entry points | What happens |
| --- | --- | --- |
| Typed Chat | `apple/Nex/ArchiveAccess.swift`, `server/nex/http_api.py`, `server/nex/privacy/public.py` | Build a turn with request, history and device time. Plan tools, check disclosure policy, then compose from completed tool results or queue a private request. |
| Live voice | `apple/Nex/LiveConversation.swift`, `server/nex/live/service.py`, `server/nex/live/session.py` | Stream audio, recognize turn boundaries, transcribe, and reuse the planner and composer through `live/public_reply.py`. Stream generated speech when allowed. |
| Recorded audio and voice | `apple/Nex/Capture.swift`, `apple/Nex/Spool.swift`, `server/nex/worker.py` | Recover and queue audio segments on device. Upload by ID and record it durably. Transcribe, index, generate notes, and route recognized requests to the same conversation flow. |
| Scheduled goals | `server/nex/workspace_jobs.py`, `server/nex/goal_runner.py`, `server/nex/goal_follow_up_jobs.py` | Read the persisted goal and schedule, ask the model which evidence it needs, run the authorized tools, and generate updates or check-ins for the workspace. |
| Private and device-local operations | `server/nex/privacy/`, `apple/Nex/PrivacyGate.swift`, `apple/Nex/ClockNotifications.swift` | Authenticate before accessing private results. Persist private questions and receipts separately. Apply device-local capabilities such as native alarms and screen permissions in the Apple app. |

The shared conversation code lives in `server/nex/conversation/`. `catalog.py` advertises tools, `plan.py` validates model proposals, `agent.py` runs bounded Observe → Reason → Act recovery steps, and `reply.py` orchestrates execution and composition. `tools.py`, `browser_tool.py`, `skills_sandbox.py`, and the feature modules implement calls. `server/nex/sentinel.py` enforces taint boundaries and manages interactive HITL action approval cards (`pending_actions`). `server/nex/active_memory.py` stores unlimited structured facts with bilingual hybrid FTS5 + vector recall synced with `MEMORY.md`. `server/nex/models.py` manages archive inference and communicates with the live model over Unix sockets (with streaming, controllable reasoning, and optional Mac deep-tier routing) when configured. The [assistant contract](assistant-contract.md) defines the ownership and failure behavior for every turn.

## Storage and privacy

- The account registry stores salted scrypt password verifiers and hashes of random device credentials. Both client listeners resolve the credential before choosing an archive. Biometric challenges and workspace sessions are scoped to the authenticated device credential. Account settings require that workspace session. The archive owner can delegate member administration. The account registry rechecks roles and the acting device before each write. The host operator creates the first owner in a browser through a private Unix socket forwarded over SSH. The setup listener only exposes the account creation page and the reusable server link. A single-use host-issued link also supports first-owner setup in the native app.
- The host holds shared model settings and assets. The original owner archive stays at the root, and each additional account has a separate archive keyed by an opaque account ID. Each archive owns its SQLite databases, search index, transcripts, recordings, PDFs, integrations, context and scheduled jobs. The local model receives context selected inside that archive. Only the original owner can use the optional host-side Mac relay.
- Clients stage recordings in account-scoped queues until durable upload acknowledgement. Watch transfers carry the account scope through the iPhone relay. Review upload, recording, and sync behavior together when changing IDs or migrations.
- A self-service data reset revokes that account’s device sessions and records a pending reset in the account registry. The archive and live voice services restart to release their shared archive lock. The archive service takes the exclusive lock, erases the requesting account’s workspace, and rotates the local queue scope before accepting another sign-in. The account registry keeps the login and role.
- The admin console is bound to server loopback. The archive’s private permissions and backup process apply to both the database and the audio store.
- Speech, screen capture, location sharing, account access, and notifications have independent permissions. Detailed Mac screen observations stay on the Mac. Enabled integrations can transmit their permitted request data to external providers.
- Each account can set default privacy for typed messages, web searches, calculations and code, and weather. Automatic Chat applies those defaults after Nex selects its tools. Per-message choices can change the default, and requests that require private access continue to use the private inbox. Private exchanges stay outside shared Chat history and its search index. Private web lookups keep their citations with the answer and send the permitted query to the web provider.

MCP connections and discovered tool schemas live in the account's archive database. The shared assistant
planner offers tools allowed by the account’s per-tool permissions. The server checks those permissions
and current MCP tool metadata before dispatching calls from an unlocked workspace or approved voice
request. Scheduled runs have a connection-level opt-in for reads and a tool-level opt-in for writes.
Nex verifies that the request authorizes a write. For scheduled writes it saves the run’s plan and
reserves each action before dispatch, then records the tool outcome to prevent replay after interruption.
The Nex host supplies configured credentials directly to the endpoint.

Built-in Gmail, Google Calendar, and TickTick writes use the same planner, private execution path, and scheduled-run
receipts. Their credentials store the provider scopes and workspace write settings together. Adding a write scope
requires OAuth consent. The execution path rechecks the account's write setting immediately before calling its API.

## Source boundaries

The server ships its Python package, tests and deployment examples. The Apple source ships a single Xcode project for three apps with shared Swift sources, plus a local Swift package with upstream dependencies. They interoperate over documented-in-code HTTP and live protocols. Keep wire compatibility and coordinate releases when those interfaces change. Build each component in isolation to verify a new dependency has landed in the right archive.
