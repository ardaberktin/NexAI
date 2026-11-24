import Foundation

struct WatchRecordingUpdate: Codable, Equatable {
    var source = "watch"
    let recording: Bool
    var validFor = WatchRecordingReport.lifetime
    var changedAt: TimeInterval?

    enum CodingKeys: String, CodingKey {
        case source, recording
        case validFor = "valid_for"
        case changedAt = "changed_at"
    }
}

struct WatchRecordingUpdates {
    var latest: WatchRecordingUpdate?
    private var newestObservation = Date.distantPast
    private var legacyExpiry: Date?

    init(restoring latest: WatchRecordingUpdate? = nil) {
        self.latest = latest
    }

    var persistentUpdate: WatchRecordingUpdate? { latest?.changedAt == nil ? nil : latest }

    mutating func receive(_ report: WatchRecordingReport, at now: Date) -> Bool {
        guard report.observedAt > newestObservation, report.observedAt <= now.addingTimeInterval(5) else {
            return false
        }
        let previous = latest?.changedAt ?? 0
        if var change = report.changedAt?.timeIntervalSince1970 {
            // A fresh observation can confirm the same session after Watch access was re-enabled.
            if latest?.validFor == 0, change <= previous, report.observedAt.timeIntervalSince1970 > previous {
                change = report.observedAt.timeIntervalSince1970
            }
            guard change > previous || (change == previous && latest?.recording == report.recording) else {
                return false
            }
            if change > previous { latest = WatchRecordingUpdate(recording: report.recording, changedAt: change) }
            legacyExpiry = nil
        } else {
            guard let expiry = report.expiration(receivedAt: now),
                report.observedAt.timeIntervalSince1970 > previous
            else { return false }
            latest = WatchRecordingUpdate(recording: report.recording)
            legacyExpiry = expiry
        }
        newestObservation = report.observedAt
        return true
    }

    mutating func clear(at now: Date) {
        let change = max(now.timeIntervalSince1970, (latest?.changedAt ?? 0).nextUp)
        newestObservation = Date(timeIntervalSince1970: change)
        latest = WatchRecordingUpdate(recording: false, validFor: 0, changedAt: change)
        legacyExpiry = nil
    }

    func payload(at date: Date) -> WatchRecordingUpdate? {
        guard let legacyExpiry, let latest else { return latest }
        let lifetime = max(0, legacyExpiry.timeIntervalSince(date))
        return WatchRecordingUpdate(recording: latest.recording && lifetime > 0, validFor: lifetime)
    }
}
