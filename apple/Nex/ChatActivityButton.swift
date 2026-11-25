import SwiftUI

struct RecordingChatActivity: View {
    let model: Controller
    @ObservedObject var capture: Capture
    let open: (Date) -> Void

    var body: some View {
        RecordingStatusTimeline { presence, date in
            if presence.anyRecording(local: ServerProfile.platform, localRecording: capture.recording, at: date) {
                ChatActivityButton(model: model, open: open)
            }
        }
    }
}

private struct ChatActivityButton: View {
    @ObservedObject var model: Controller
    @Environment(\.scenePhase) private var scene
    let open: (Date) -> Void
    @State private var detections: [ChatDetection]?
    @State private var unavailable = false

    var body: some View {
        let now = Date.now
        let expirations = (detections ?? []).map(\.expiresAt).filter { $0 > now }.sorted()
        TimelineView(.explicit([now] + expirations)) { tick in
            let current = (detections ?? []).filter { $0.expiresAt > tick.date }
            Button {
                // The end of a recording is exclusive, including one ending exactly at midnight.
                open(current.first?.observedAt.addingTimeInterval(-0.001) ?? .now)
            } label: {
                label(current)
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint("Opens the Activities timeline")
        }
        .frame(maxWidth: WorkspaceStyle.conversationWidth)
        .padding(.horizontal, WorkspaceStyle.inset)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity)
        .task(id: scene != .background && model.serverProfile != nil) {
            guard scene != .background, model.serverProfile != nil else { return }
            await observe()
        }
    }

    private func label(_ current: [ChatDetection]) -> some View {
        let tint = current.first?.activity.tint ?? Color.secondary
        return HStack(spacing: 10) {
            Image(systemName: current.first?.activity.symbol ?? "waveform.path")
                .font(.body.weight(.semibold))
                .foregroundStyle(tint)
                .frame(width: 31, height: 31)
                .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 9))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("Current activity").font(.caption).foregroundStyle(.secondary)
                if current.isEmpty {
                    Text(emptyTitle)
                } else {
                    ForEach(current) { item in
                        Text("\(item.activity.title) · \(item.activity.confidence)")
                    }
                }
            }
            .font(.subheadline.weight(.medium))
            .multilineTextAlignment(.leading)
            Spacer(minLength: 4)
            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 12).padding(.vertical, 6)
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
        .contentShape(Rectangle())
    }

    private var emptyTitle: LocalizedStringKey {
        if model.serverProfile == nil { return "Connect to see activity" }
        if unavailable { return "Activity status unavailable" }
        if detections == nil { return "Checking activity…" }
        return "No activity detected"
    }

    private func observe() async {
        while !Task.isCancelled {
            do {
                let result = try await model.chatDetections()
                guard !Task.isCancelled else { return }
                detections = result
                unavailable = false
            } catch {
                guard !Task.isCancelled else { return }
                detections = nil
                unavailable = true
            }
            do { try await Task.sleep(for: .seconds(12)) } catch { return }
        }
    }
}
