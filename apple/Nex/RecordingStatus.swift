import SwiftUI

enum RemoteRecordingState: String, Decodable {
    case recording, idle, unknown
}

struct RecordingStatusSnapshot: Decodable {
    static let path = "/v1/workspace/recording-status"

    struct Report: Decodable {
        let state: RemoteRecordingState
        let validFor: TimeInterval
        var untilChanged: Bool? = nil
    }

    let cursor: String
    let recorders: [String: Report]
}

struct RecordingPresence {
    var snapshot: RecordingStatusSnapshot?
    var receivedAt = Date.distantPast

    func expirations(after date: Date) -> [Date] {
        guard let snapshot else { return [] }
        return Set(
            snapshot.recorders.values.filter { $0.untilChanged != true }.map {
                receivedAt.addingTimeInterval($0.validFor)
            }
        )
        .filter { $0 > date }.sorted()
    }

    func state(of recorder: RecordingDevice, at date: Date) -> RemoteRecordingState {
        guard let report = snapshot?.recorders[recorder.rawValue] else { return .unknown }
        if report.untilChanged == true || date < receivedAt.addingTimeInterval(report.validFor) { return report.state }
        return .unknown
    }

    func recordingPeers(excluding local: String, at date: Date) -> [RecordingDevice] {
        RecordingDevice.allCases.filter { $0.rawValue != local && state(of: $0, at: date) == .recording }
    }

    func anyRecording(local: String, localRecording: Bool, at date: Date) -> Bool {
        localRecording || !recordingPeers(excluding: local, at: date).isEmpty
    }
}

extension EnvironmentValues {
    @Entry var recordingPresence = RecordingPresence()
}

struct RecordingStatusTimeline<Content: View>: View {
    @Environment(\.recordingPresence) private var presence
    @ViewBuilder var content: (RecordingPresence, Date) -> Content

    var body: some View {
        let now = Date.now
        TimelineView(.explicit([now] + presence.expirations(after: now))) { tick in
            content(presence, max(tick.date, .now))
        }
    }
}

struct RecordingStatusPeers: View {
    let local: RecordingDevice

    var body: some View {
        RecordingStatusTimeline { presence, date in
            let recorders = presence.recordingPeers(excluding: local.rawValue, at: date)
            if !recorders.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(recorders, id: \.self) { recorder in
                        label(for: recorder)
                    }
                }
            }
        }
    }

    private func label(for recorder: RecordingDevice) -> some View {
        let device = Text(verbatim: recorder.title)
        return Label {
            Text("\(device) recording")
        } icon: {
            Image(systemName: "record.circle.fill")
        }
        .labelStyle(.titleAndIcon)
        .font(.caption)
        .foregroundStyle(.red)
        .accessibilityElement(children: .combine)
    }
}
