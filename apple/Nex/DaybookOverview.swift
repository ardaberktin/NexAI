import SwiftUI

struct DaybookOverview: View {
    let day: DaybookDay
    let context: DaybookContext
    let sideBySide: Bool
    let ask: (DaybookContext) -> Void
    let openConversation: (DaybookContext) -> Void
    let openActivity: (RecordedActivity) -> Void
    let showSource: (String) async -> Void

    private var hasEntries: Bool { !day.conversations.isEmpty || !(day.activities ?? []).isEmpty }

    var body: some View {
        let layout =
            sideBySide
            ? AnyLayout(HStackLayout(alignment: .top, spacing: 28))
            : AnyLayout(VStackLayout(alignment: .leading, spacing: 22))
        layout {
            briefing
            timeline.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var briefing: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Daily briefing", systemImage: "sparkles")
                .font(.title2.weight(.semibold))
            if let report = day.briefing, !report.summary.isEmpty {
                Text(report.summary).font(.body).textSelection(.enabled)
                ForEach(report.highlights) { highlight in
                    highlightRow(highlight)
                }
                if day.updating {
                    Label("Updating with new observations", systemImage: "arrow.trianglehead.clockwise")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Text(
                    hasEntries ? "Excalibur is preparing your briefing."
                        : "Your briefing appears as Nex processes the day’s recordings."
                )
                .foregroundStyle(.secondary)
            }
            if hasEntries {
                Button("Ask about this day", systemImage: "waveform.bubble") { ask(context) }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
    }

    private func highlightRow(_ highlight: DaybookHighlight) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Divider()
            Label(highlight.kind.title, systemImage: highlight.kind.symbol)
                .font(.caption.weight(.semibold)).foregroundStyle(Color.accentColor)
            Text(highlight.text).font(.callout).textSelection(.enabled)
            HStack {
                Button("Source", systemImage: "doc.text") {
                    Task { await showSource(highlight.source) }
                }
                Button("Ask Nex", systemImage: "waveform.bubble") {
                    ask(context.focused(on: highlight.source, detail: highlight.text))
                }
            }
            .font(.caption)
            .buttonStyle(.bordered)
        }
    }

    private var timeline: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Day timeline").font(.title2.weight(.semibold))
            if day.pending > 0 {
                Label("\(day.pending) awaiting transcription", systemImage: "waveform")
                    .font(.caption).foregroundStyle(.secondary)
            } else if !hasEntries {
                Text("No conversations or identified activities for this date.").foregroundStyle(.secondary)
            }
            ForEach(entries) { entry in
                switch entry {
                case .activity(let activity):
                    ActivityRow(activity: activity) { openActivity(activity) }
                case .conversation(let conversation):
                    conversationRow(conversation)
                }
            }
        }
    }

    private var entries: [DaybookEntry] {
        (day.conversations.map(DaybookEntry.conversation) + (day.activities ?? []).map(DaybookEntry.activity))
            .sorted { ($0.started, $0.id) < ($1.started, $1.id) }
    }

    private func conversationRow(_ conversation: DaybookConversation) -> some View {
        Button {
            openConversation(context.focused(on: conversation.id))
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: conversation.mode == .voice ? "waveform.bubble.fill" : "text.bubble")
                    .foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 5) {
                    Text(conversation.title).font(.headline)
                    Text("\(conversation.timeRange) · \(conversation.deviceName)")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(conversation.preview).font(.subheadline).lineLimit(2)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

private enum DaybookEntry: Identifiable {
    case conversation(DaybookConversation)
    case activity(RecordedActivity)

    var id: String {
        switch self {
        case .conversation(let item): "conversation-" + item.id
        case .activity(let item): "activity-" + item.id
        }
    }

    var started: String {
        switch self {
        case .conversation(let item): item.started
        case .activity(let item): item.started
        }
    }
}
