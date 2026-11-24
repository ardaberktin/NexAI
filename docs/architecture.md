# Nex architecture

One Linux Nex host serves multiple accounts. Each account has its own archive and can connect a Mac and an iPhone. Apple Watch uses the iPhone as its relay. The server stores the archives and executes tools. The Apple apps own capture, presentation, and device-local permissions.

## Runtime topology

```text
Mac and iPhone ── pinned HTTPS :8765 ── Nex archive API ── SQLite / FTS5 and audio
              └── pinned live :8767 ── Nex voice service ── local model and speech runtimes
Apple Watch ── WatchConnectivity ── iPhone
Admin browser ── SSH tunnel ── server loopback console :8766
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

The shared conversation code lives in `server/nex/conversation/`. `catalog.py` advertises tools, `plan.py` validates model proposals, and `reply.py` orchestrates execution and composition. `tools.py` and the feature modules implement calls. `server/nex/models.py` manages archive inference and communicates with the live model over Unix sockets when configured. The [assistant contract](assistant-contract.md) defines the ownership and failure behavior for every turn.

## Storage and privacy

- The account registry stores salted scrypt password verifiers and hashes of random device credentials. Both listeners resolve the credential before choosing an archive. Biometric challenges and workspace sessions are scoped to the authenticated device credential.
- The host holds shared model settings and assets. The original owner archive stays at the root, and each additional account has a separate archive keyed by an opaque account ID. Each archive owns its SQLite databases, search index, transcripts, recordings, PDFs, integrations, context and scheduled jobs. The local model receives context selected inside that archive. Only the original owner can use the optional host-side Mac relay.
- Clients stage recordings in account-scoped queues until durable upload acknowledgement. Watch transfers carry the account scope through the iPhone relay. Review upload, recording, and sync behavior together when changing IDs or migrations.
- The admin console is bound to server loopback. The archive’s private permissions and backup process apply to both the database and the audio store.
- Speech, screen capture, location sharing, account access, and notifications have independent permissions. Detailed Mac screen observations stay on the Mac. Enabled integrations can transmit their permitted request data to external providers.

## Source boundaries

The server ships its Python package, tests and deployment examples. The Apple source ships a single Xcode project for three apps with shared Swift sources, plus a local Swift package with upstream dependencies. They interoperate over documented-in-code HTTP and live protocols. Keep wire compatibility and coordinate releases when those interfaces change. Build each component in isolation to verify a new dependency has landed in the right archive.
