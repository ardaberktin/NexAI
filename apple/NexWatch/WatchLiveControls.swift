import SwiftUI

struct WatchLiveControls: View {
    let state: WatchLiveConversation.State
    @ObservedObject var live: WatchLiveConversation
    @EnvironmentObject private var workspace: WatchWorkspace

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(status, systemImage: state == .speaking ? "speaker.wave.2" : "waveform")
                .font(.headline).accessibilityAddTraits(.isHeader)
            if state == .recording {
                Button("Send question", systemImage: "arrow.up.circle") { live.submit() }
            } else if state == .speaking || state == .preparingVoice {
                Button("Interrupt", systemImage: "pause.circle") { live.interrupt() }
            } else if state == .listening || state == .answered {
                if live.reply != nil {
                    Button("Hear again", systemImage: "speaker.wave.2") { live.hearAgain() }
                }
                if state == .answered {
                    Button("Ask a follow-up", systemImage: "mic") { live.start(language: workspace.language) }
                }
            }
            Button("End conversation", systemImage: "stop.circle") { live.stop() }
            if let issue = live.issue { Text(issue).font(.footnote).foregroundStyle(.orange) }
            if let reply = live.reply { MarkdownMessageBody(reply).font(.body) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var status: LocalizedStringKey {
        switch state {
        case .connecting: "Opening microphone…"
        case .listening: "Listening on Apple Watch"
        case .recording: "Hearing your question"
        case .waiting: "Question saved · Waiting for Nex"
        case .preparingVoice: "Getting Nex’s voice…"
        case .speaking: "Nex is speaking"
        case .answered: "Your last question"
        case .unrecognized: "Try saying that again"
        }
    }
}

struct WatchLiveIssue: View {
    let message: String
    let reply: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let reply { MarkdownMessageBody(reply).font(.body).privacySensitive() }
            Text(message).font(.footnote).foregroundStyle(.orange)
        }
    }
}
