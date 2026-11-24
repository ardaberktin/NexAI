import Foundation

@main
enum RecordingPresenceCheck {
    private static func checkWatchRelay() throws {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let later = start.addingTimeInterval(7200)
        var relay = WatchRecordingUpdates()
        let recording = WatchRecordingReport(recording: true, observedAt: start, changedAt: start)
        let context = try PropertyListSerialization.data(
            fromPropertyList: recording.context, format: .binary, options: 0)
        let fields = try PropertyListSerialization.propertyList(from: context, format: nil) as? [String: Any]
        guard let report = fields.flatMap(WatchRecordingReport.init) else {
            preconditionFailure("Invalid watch context")
        }
        precondition(relay.receive(report, at: later))
        precondition(relay.payload(at: later)?.recording == true)

        let stop = start.addingTimeInterval(100)
        precondition(
            relay.receive(WatchRecordingReport(recording: false, observedAt: stop, changedAt: stop), at: later))
        let saved = try JSONEncoder().encode(relay.persistentUpdate)
        relay = WatchRecordingUpdates(restoring: try JSONDecoder().decode(WatchRecordingUpdate.self, from: saved))
        precondition(!relay.receive(report, at: later))
        precondition(relay.payload(at: later)?.recording == false)

        relay.clear(at: later)
        precondition(!relay.receive(report, at: later))
        let wake = later.addingTimeInterval(1)
        precondition(relay.receive(WatchRecordingReport(recording: true, observedAt: wake, changedAt: start), at: wake))
        precondition(relay.payload(at: wake)?.recording == true)
        var legacy = WatchRecordingUpdates()
        precondition(!legacy.receive(WatchRecordingReport(recording: true, observedAt: start), at: later))
        precondition(legacy.receive(WatchRecordingReport(recording: true, observedAt: start), at: start))
        precondition(legacy.payload(at: later)?.recording == false)
    }

    static func main() throws {
        try checkWatchRelay()
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let idle = RecordingStatusSnapshot.Report(state: .idle, validFor: 30)
        func presence(_ reports: [String: RecordingStatusSnapshot.Report]) -> RecordingPresence {
            RecordingPresence(snapshot: RecordingStatusSnapshot(cursor: "", recorders: reports), receivedAt: now)
        }

        for local in RecordingDevice.allCases.map(\.rawValue) {
            let missing = RecordingPresence()
            precondition(missing.recordingPeers(excluding: local, at: now).isEmpty)
            precondition(missing.anyRecording(local: local, localRecording: true, at: now))
            precondition(!missing.anyRecording(local: local, localRecording: false, at: now))
            for device in RecordingDevice.allCases {
                let report = presence([device.rawValue: .init(state: .recording, validFor: 8)])
                let expectedPeers = device.rawValue == local ? [] : [device]
                precondition(report.recordingPeers(excluding: local, at: now) == expectedPeers)
                let active = report.anyRecording(local: local, localRecording: false, at: now)
                precondition(active == (device.rawValue != local))
                precondition(!report.anyRecording(local: local, localRecording: false, at: now.addingTimeInterval(8)))
                precondition(
                    !presence([device.rawValue: idle]).anyRecording(local: local, localRecording: false, at: now))
            }
        }

        let overlapping = presence([
            "mac": .init(state: .recording, validFor: 3),
            "watch": .init(state: .recording, validFor: 6),
        ])
        let macExpiry = now.addingTimeInterval(3)
        let watchExpiry = now.addingTimeInterval(6)
        precondition(overlapping.expirations(after: now) == [macExpiry, watchExpiry])
        precondition(overlapping.recordingPeers(excluding: "iphone", at: now) == [.mac, .watch])
        precondition(overlapping.recordingPeers(excluding: "iphone", at: macExpiry) == [.watch])
        precondition(overlapping.state(of: .mac, at: macExpiry) == .unknown)
        precondition(overlapping.anyRecording(local: "iphone", localRecording: false, at: macExpiry))
        precondition(!overlapping.anyRecording(local: "iphone", localRecording: false, at: watchExpiry))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = #"{"cursor":"w","recorders":{"watch":{"state":"recording","valid_for":30,"until_changed":true}}}"#
        let snapshot = try decoder.decode(RecordingStatusSnapshot.self, from: Data(payload.utf8))
        let sleeping = RecordingPresence(snapshot: snapshot, receivedAt: now)
        precondition(sleeping.expirations(after: now).isEmpty)
        precondition(sleeping.anyRecording(local: "iphone", localRecording: false, at: now.addingTimeInterval(7200)))
        print("Recording presence checks passed.")
    }
}
