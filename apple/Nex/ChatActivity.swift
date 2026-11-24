import Foundation

struct ChatActivity: Decodable {
    let category: String
    let title: String
    let confidence: String
    let device: String
    let started: String
    let ended: String
}

struct ChatDetection: Identifiable {
    let activity: ChatActivity
    let observedAt: Date
    let expiresAt: Date
    var id: String { activity.category }
}

struct ChatActivitySnapshot: Decodable {
    // Daily timelines from older servers omit the recognition window.
    static let legacyFreshness: TimeInterval = 90
    var items: [ChatActivity]
    let asOf: String?
    let freshFor: TimeInterval?

    func detections(receivedAt: Date) -> [ChatDetection] {
        let anchor = asOf.flatMap(WorkspaceDate.instant) ?? receivedAt
        let freshness = freshFor ?? Self.legacyFreshness
        let cutoff = anchor.addingTimeInterval(-freshness)
        var latest: [String: ChatDetection] = [:]
        for item in items {
            guard let end = WorkspaceDate.instant(item.ended), end > cutoff, end <= anchor else { continue }
            if let previous = latest[item.device], previous.activity.started > item.started { continue }
            let expiresAt = receivedAt.addingTimeInterval(freshness + end.timeIntervalSince(anchor))
            latest[item.device] = ChatDetection(activity: item, observedAt: end, expiresAt: expiresAt)
        }

        // An unclear clip supersedes an older detection on its own microphone.
        var seen = Set<String>()
        return latest.values.sorted { $0.observedAt > $1.observedAt }.filter {
            $0.id != "unknown" && $0.id != "speech" && seen.insert($0.id).inserted
        }
    }
}
