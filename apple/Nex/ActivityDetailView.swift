import SwiftUI

struct ActivityDetailView: View {
    @ObservedObject var model: Controller
    let activity: RecordedActivity
    let day: DaybookContext
    @State private var clips: [RecordedActivity]?
    @State private var error: String?
    @State private var source: MemoryItem?
    @State private var question: DaybookContext?

    var body: some View {
        WorkspaceSheet(title: "Activity details") {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .top, spacing: 12) {
                        WorkspaceIcon(symbol: activity.symbol, tint: activity.tint)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(activity.title).font(.title2.weight(.semibold))
                            Text("\(activity.timeRange) · \(activity.deviceName)").foregroundStyle(.secondary)
                        }
                    }
                    Button("Ask Nex about this activity", systemImage: "waveform.bubble") {
                        question = day.focused(on: activity.id, detail: activity.title)
                    }
                    .buttonStyle(.borderedProminent)
                    if let error { WorkspaceIssue(message: error) { await load() } }
                    if let clips {
                        Text("Observations (\(clips.count))").font(.headline)
                        if clips.isEmpty { Text("This activity has been revised.").foregroundStyle(.secondary) }
                        ForEach(clips) { clip in observation(clip) }
                    } else if error == nil {
                        ProgressView("Opening observations…")
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task { await load() }
        .sheet(item: $source) { item in
            SourceSheet(item: item, model: model) { Task { await load() } }
        }
        .sheet(item: $question) { selection in
            DaybookAskSheet(model: model, context: selection)
        }
    }

    private func observation(_ clip: RecordedActivity) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Divider()
            Text("\(clip.timeRange) · \(clip.basis) · \(clip.confidence)")
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(clip.detail).textSelection(.enabled)
            if !clip.sounds.isEmpty {
                Label(clip.sounds.joined(separator: ", "), systemImage: "waveform")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Open recording", systemImage: "waveform.circle") {
                Task {
                    do { source = try await model.source(clip.id) } catch { self.error = error.localizedDescription }
                }
            }
            .buttonStyle(.bordered).controlSize(.small)
        }
    }

    private func load() async {
        do {
            clips = try await model.activityObservations(day.focused(on: activity.id))
            error = nil
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}
