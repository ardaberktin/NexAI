# Nex

Nex is a self-hosted AI assistant and audio journal for Mac, iPhone, and Apple Watch. Its native apps connect to a Linux server on your Tailscale network. Nex runs its assistant models locally and uses the data and tools you enable to answer questions, recall recordings, and run scheduled goals.

- **Hosted accounts** let multiple users connect to one server with separate archives, workspace data, and integrations.
- **Chat and voice** share a model-driven tool flow. Start a live conversation or record a question for later.
- **Recordings and Daybook** keep time-stamped audio, searchable transcripts, editable notes, and source citations. Devices can queue audio offline and upload when they reconnect.
- **Goals and workspace tools** support scheduled check-ins, local alarms and timers, and optional public web, weather, Gmail, Google Calendar, and TickTick access.
- **Private answers** use device-bound biometric approval and stay in the unlocked workspace. Voice recognition is separate from authentication.

## Get started

| Component | Platform | Source release asset | Setup |
| --- | --- | --- | --- |
| Nex Server | Linux with Python 3.12 and Tailscale | `nex-server-source.tar.gz` | [Server setup](server/README.md) |
| Nex for Apple devices | macOS, iOS, and watchOS | `nex-apple-source.zip` | [Apple build and sign-in](apple/README.md) |

Download either asset from the repository’s **Releases** page when a release is available, or clone this repository for both. The assets contain source. Build the Apple apps with Xcode and sign in to an existing Nex server. Install the server first if you’re setting up both. Live conversation and archive transcription have separate model and runtime requirements.

## Repository guide

| Path | Contents |
| --- | --- |
| [`apple/`](apple/) | SwiftUI apps, Xcode schemes, Watch relay, and the local voice capture package |
| [`server/`](server/) | Python archive API, live voice service, model and tool execution, tests, and deployment examples |
| [`docs/`](docs/) | Architecture and the assistant behavior contract |
| [`scripts/`](scripts/) | Source release packaging |

Start with the [architecture](docs/architecture.md) to trace how a request moves through Nex. Read [AGENTS.md](AGENTS.md), the [assistant contract](docs/assistant-contract.md), and the [contribution guide](CONTRIBUTING.md) before changing the project.

Project source is available under the [MIT License](LICENSE). [Third-party dependencies and models](THIRD_PARTY_NOTICES.md) carry their own terms. For private vulnerability reporting, see [SECURITY.md](SECURITY.md).
