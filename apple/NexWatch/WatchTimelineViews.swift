import SwiftUI

struct WatchDaybookView: View {
    @State private var date = Date.now
    @EnvironmentObject private var workspace: WatchWorkspace

    var body: some View {
        let day = DaybookContext(date: date)
        WatchPage(title: "Daybook", key: day.id, request: WatchRequest(.daybook, day: day)) { (result: DaybookDay) in
            WatchDayPicker(date: $date)
            NavigationLink("Ask about this day", destination: WatchChatView(day: day))
            if let briefing = result.briefing {
                Section("Briefing") {
                    NavigationLink("Read briefing", destination: WatchReadingView(title: "Briefing", detail: briefing.summary))
                    ForEach(briefing.highlights) { highlight in
                        NavigationLink {
                            List {
                                Text(highlight.text)
                                NavigationLink("Ask Nex", destination: WatchChatView(
                                    day: day.focused(on: highlight.source, detail: highlight.text)))
                            }
                            .navigationTitle(highlight.kind.title)
                            .modifier(WatchPrivateScreen())
                        } label: {
                            Label(highlight.text, systemImage: highlight.kind.symbol).lineLimit(3)
                        }
                    }
                }
            } else if result.updating {
                Text("Updating briefing…").font(.caption).foregroundStyle(.secondary)
            }
            if result.pending > 0 { Text("\(result.pending) recordings processing").font(.caption) }
            Section("Conversations") {
                if result.conversations.isEmpty { Text("No conversations for this day.").foregroundStyle(.secondary) }
                ForEach(result.conversations) { item in
                    NavigationLink {
                        WatchConversationView(day: day.focused(on: item.id), item: item)
                    } label: {
                        WatchTimelineRow(title: item.preview.isEmpty ? item.title : item.preview,
                                         subtitle: item.timeRange + " · " + item.deviceName)
                    }
                }
            }
            if workspace.features?[.activities] == true {
                Section("Activities") {
                    ForEach(result.activities ?? []) { item in
                        WatchActivityLink(day: day, item: item)
                    }
                }
            }
        }
    }
}

struct WatchActivitiesView: View {
    @State private var date = Date.now
    @State private var device: String?

    var body: some View {
        let day = DaybookContext(date: date)
        WatchPage(title: "Activities", key: day.id, request: WatchRequest(.activities, day: day)) { (result: ActivityDay) in
            WatchDayPicker(date: $date)
            Picker("Microphone", selection: $device) {
                Text("All devices").tag(nil as String?)
                Text("Apple Watch").tag("watch" as String?)
                Text("iPhone").tag("iphone" as String?)
                Text("Mac").tag("mac" as String?)
            }
            let items = result.items.filter { device == nil || $0.device == device }
            if items.isEmpty { Text("No activity for this selection.").foregroundStyle(.secondary) }
            ForEach(items) { item in WatchActivityLink(day: day, item: item) }
            if result.pending > 0 { Text("\(result.pending) recordings processing").font(.caption) }
        }
    }
}

private struct WatchDayPicker: View {
    @Binding var date: Date

    var body: some View {
        HStack {
            Button("Previous day", systemImage: "chevron.left") { move(-1) }.labelStyle(.iconOnly)
            Spacer(minLength: 2)
            Text(date.formatted(.dateTime.month(.abbreviated).day()))
                .font(.footnote.weight(.semibold))
                .accessibilityLabel(date.formatted(date: .complete, time: .omitted))
            Spacer(minLength: 2)
            Button("Next day", systemImage: "chevron.right") { move(1) }.labelStyle(.iconOnly)
                .disabled(Calendar.current.isDateInToday(date))
        }
        .buttonStyle(.borderless)
    }

    private func move(_ days: Int) {
        date = Calendar.current.date(byAdding: .day, value: days, to: date) ?? date
    }
}

private struct WatchConversationView: View {
    let day: DaybookContext
    let item: DaybookConversation

    var body: some View {
        WatchPage(title: "Conversation", request: WatchRequest(.conversation, day: day)) { (result: DaybookDetail) in
            Text(item.timeRange + " · " + item.deviceName).font(.caption).foregroundStyle(.secondary)
            NavigationLink("Ask about this", destination: WatchChatView(day: day))
            ForEach(result.recordings) { clip in WatchRecordingLink(clip: clip) }
        }
    }
}

private struct WatchActivityLink: View {
    let day: DaybookContext
    let item: RecordedActivity

    var body: some View {
        NavigationLink {
            WatchPage(title: "Activity", request: WatchRequest(.activities, day: day.focused(on: item.id))) { (result: ListResponse<RecordedActivity>) in
                Text(item.title).font(.headline)
                if !item.detail.isEmpty { Text(item.detail) }
                Text((item.sounds).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                ForEach(result.items) { observation in
                    NavigationLink {
                        WatchRemoteRecording(id: observation.id)
                    } label: {
                        WatchTimelineRow(title: observation.title, subtitle: observation.timeRange)
                    }
                }
            }
        } label: {
            WatchTimelineRow(title: item.title, subtitle: item.timeRange + " · " + item.deviceName)
        }
    }
}

struct WatchTimelineRow: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).lineLimit(3)
            Text(subtitle).font(.caption2).foregroundStyle(.secondary)
        }
    }
}
