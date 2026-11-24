import Foundation

struct DaybookContext: Codable, Equatable, Identifiable, Sendable {
    let day: String
    let timeZone: String
    var source: String?
    var focus: String?

    var id: String { [day, timeZone, source ?? "", focus ?? ""].joined(separator: "/") }

    var query: [URLQueryItem] {
        var items = [URLQueryItem(name: "day", value: day), URLQueryItem(name: "time_zone", value: timeZone)]
        if let source { items.append(URLQueryItem(name: "source", value: source)) }
        return items
    }

    func focused(on source: String, detail: String? = nil) -> Self {
        Self(day: day, timeZone: timeZone, source: source, focus: detail)
    }
}

extension DaybookContext {
    init(date: Date, timeZone: TimeZone = .current) {
        let format = DateFormatter()
        format.locale = Locale(identifier: "en_US_POSIX")
        format.calendar = Calendar(identifier: .gregorian)
        format.timeZone = timeZone
        format.dateFormat = "yyyy-MM-dd"
        self.init(day: format.string(from: date), timeZone: timeZone.identifier)
    }
}

struct DaybookDay: Decodable {
    let conversations: [DaybookConversation]
    let activities: [RecordedActivity]?
    let pending: Int
    let briefing: DaybookBriefing?
    let updating: Bool
}

struct DaybookConversation: Decodable, Identifiable {
    let id: String
    let started: String
    let ended: String
    let device: String
    let mode: CaptureMode
    let count: Int
    let preview: String

    var title: String { mode == .voice ? "Conversation with Nex" : "Recorded conversation" }
    var deviceName: String { RecordingDevice.name(device) }

    var timeRange: String {
        NexDate.timeRange(started, ended)
    }
}

struct DaybookDetail: Decodable {
    let recordings: [Recording]
}

struct DaybookBriefing: Decodable {
    let summary: String
    let highlights: [DaybookHighlight]
    let generatedAt: String
}

struct DaybookHighlight: Decodable, Identifiable {
    enum Kind: String, Decodable {
        case decision, followUp = "follow_up", keyMoment = "key_moment"

        var title: String {
            switch self {
            case .decision: "Decision"
            case .followUp: "Follow-up"
            case .keyMoment: "Key moment"
            }
        }

        var symbol: String {
            switch self {
            case .decision: "checkmark.seal"
            case .followUp: "arrow.turn.down.right"
            case .keyMoment: "sparkle"
            }
        }
    }

    let kind: Kind
    let text: String
    let source: String
    var id: String { [kind.rawValue, source, text].joined(separator: "/") }
}
