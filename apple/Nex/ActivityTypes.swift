import SwiftUI

struct ActivityDay: Decodable {
    let items: [RecordedActivity]
    let pending: Int
}

struct ActivityCoverage {
    let duration: Duration
    let kinds: Int

    init(_ periods: [RecordedActivity]) {
        let format = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        var coveredUntil = Date.distantPast
        var seconds: TimeInterval = 0
        for period in periods {
            guard let start = try? format.parse(period.started), let end = try? format.parse(period.ended) else {
                continue
            }
            seconds += max(0, end.timeIntervalSince(max(coveredUntil, start)))
            coveredUntil = max(coveredUntil, end)
        }
        duration = .seconds(seconds.rounded())
        kinds = Set(periods.map(\.category)).subtracting(["unknown"]).count
    }
}

struct RecordedActivity: Decodable, Identifiable, ActivityAppearance {
    let id: String
    let started: String
    let ended: String
    let device: String
    let category: String
    let title: String
    let confidence: String
    let basis: String
    let detail: String
    let sounds: [String]
    let count: Int?

    var deviceName: String { RecordingDevice.name(device) }
    var timeRange: String { NexDate.timeRange(started, ended) }
}

protocol ActivityAppearance {
    var category: String { get }
}

#if !os(watchOS)
extension ChatActivity: ActivityAppearance {}
#endif

extension ActivityAppearance {
    var symbol: String {
        switch category {
        case "ps5", "gaming": "gamecontroller.fill"
        case "movie": "film"
        case "show", "media": "tv"
        case "livestream": "dot.radiowaves.left.and.right"
        case "youtube": "play.rectangle.fill"
        case "short_video", "tiktok", "instagram": "play.rectangle.on.rectangle"
        case "phone": "iphone"
        case "call": "phone.fill"
        case "computer": "desktopcomputer"
        case "music": "music.note"
        case "conversation", "speech", "nex": "bubble.left.and.bubble.right"
        case "cooking": "fork.knife"
        case "exercise": "figure.run"
        case "reading": "book"
        case "housework": "house"
        case "travel": "tram"
        default: "waveform"
        }
    }

    var tint: Color {
        switch category {
        case "ps5", "gaming", "movie", "show", "livestream", "media": .indigo
        case "youtube", "short_video", "tiktok", "instagram", "music": .pink
        case "conversation", "speech", "call", "nex": .blue
        case "unknown": .secondary
        default: .accentColor
        }
    }
}

#if !os(watchOS)
extension Controller {
    func chatDetections() async throws -> [ChatDetection] {
        let now = Date.now
        let today = DaybookContext(date: now)
        let connection = try connection()
        let query = today.query + [URLQueryItem(name: "current", value: "1")]
        var snapshot: ChatActivitySnapshot = try await connection.get("/v1/activities", query: query)
        if snapshot.freshFor == nil {
            // Older servers return the requested local day. A recent clip can straddle midnight.
            let earlier = DaybookContext(date: now.addingTimeInterval(-ChatActivitySnapshot.legacyFreshness))
            if earlier.day != today.day {
                let previous: ChatActivitySnapshot = try await connection.get("/v1/activities", query: earlier.query)
                snapshot.items = previous.items + snapshot.items
            }
        }
        return snapshot.detections(receivedAt: .now)
    }

    func activities(_ day: DaybookContext) async throws -> ActivityDay {
        try await connection().get("/v1/activities", query: day.query)
    }

    func activityObservations(_ selection: DaybookContext) async throws -> [RecordedActivity] {
        let result: ListResponse<RecordedActivity> = try await connection().get(
            "/v1/activities", query: selection.query)
        return result.items
    }
}
#endif
