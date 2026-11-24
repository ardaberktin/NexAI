import Foundation

struct NewsStory: Decodable, Identifiable {
    let id: String
    let title: String
    let summary: String
    let url: URL
    let image: URL?
    let topic: String
    let source: String
    let published: String
}

struct FeedSettings: Codable, Equatable {
    var prompt: String
    var zone: String
    var hour: Int
    var minute: Int
    let revision: Int
}

struct NewsFeed: Decodable {
    var settings: FeedSettings
    let day: String?
    let today: String
    let generated: String?
    let stories: [NewsStory]
    let editions: [String]
    let previousDay: String?
    var saved: [NewsStory]
    let pending: Bool
    let error: String?

    var olderDay: String? {
        guard let day else { return nil }
        return previousDay ?? editions.first { $0 < day }
    }
}

struct NexIdea: Decodable, Identifiable {
    let id: String
    let icon: String
    let title: String
    let detail: String
    let prompt: String
    let opensGoal: Bool?
}

struct IdeasSnapshot: Decodable {
    let items: [NexIdea]
    let pending: Bool?
    let failed: Bool?
}

enum GoalStatus: String, Decodable {
    case active, paused, completed
}

struct NexGoal: Decodable, Identifiable {
    let id: String
    let status: GoalStatus
    let title: String
    let prompt: String
    let zone: String
    let frequencyMinutes: Int
    let scheduledAt: String?
    let revision: Int
    let nextRun: String?
    let lastChecked: String?
    let latest: String?
    let error: String?
    let activity: [GoalActivity]?
    let artifactCount: Int?
    let followUp: GoalFollowUp?

    var frequency: GoalFrequency {
        guard scheduledAt != nil else { return .onDemand }
        return GoalFrequency(rawValue: frequencyMinutes) ?? .once
    }
}

struct GoalActionRequest: Encodable {
    let action: String
    let revision: Int
}

struct GoalFollowUp: Decodable, Identifiable {
    let id: String
    let due: String
    let question: String?
    let choices: [String]
    let selectedChoice: Int?
    let canAnswer: Bool

    var selectedLabel: String? {
        guard let selectedChoice, choices.indices.contains(selectedChoice) else { return nil }
        return choices[selectedChoice]
    }
}

struct GoalFollowUpAnswer: Encodable {
    let followUpId: String
    let choice: Int

    enum CodingKeys: String, CodingKey {
        case followUpId = "follow_up_id"
        case choice
    }
}

struct GoalDeletion: Decodable {
    let deleted: Bool
}

struct GoalActivity: Decodable, Identifiable {
    let id: String
    let created: String
    let title: String
    let detail: String
    let artifact: Bool
}

enum GoalFrequency: Int, CaseIterable, Identifiable {
    case once = 0
    case everyFifteenMinutes = 15
    case hourly = 60
    case daily = 1440
    case weekly = 10080
    case onDemand = -1

    var id: Self { self }

    var title: String {
        switch self {
        case .once: "Once"
        case .everyFifteenMinutes: "Every 15 minutes"
        case .hourly: "Hourly"
        case .daily: "Daily"
        case .weekly: "Weekly"
        case .onDemand: "On demand"
        }
    }
}

struct GoalDraft: Encodable, Identifiable {
    let id: String
    let revision: Int?
    var prompt: String
    var zone: String
    var frequency: GoalFrequency
    var date: Date

    init(_ goal: NexGoal? = nil, prompt: String = "") {
        id = goal?.id ?? nexID()
        revision = goal?.revision
        self.prompt = goal?.prompt ?? prompt
        zone = goal?.zone ?? NexDate.timeZone
        frequency = goal?.frequency ?? .once
        date = goal?.scheduledAt.flatMap(WorkspaceDate.instant) ?? Self.defaultTime(in: zone)
    }

    static func defaultTime(in zone: String) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: zone) ?? .gmt
        return calendar.nextDate(after: .now, matching: DateComponents(minute: 0, second: 0),
                                 matchingPolicy: .nextTime) ?? .now.addingTimeInterval(3600)
    }

    private enum CodingKeys: String, CodingKey {
        case revision, prompt, zone
        case frequencyMinutes = "frequency_minutes"
        case scheduledAt = "scheduled_at"
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encodeIfPresent(revision, forKey: .revision)
        try values.encode(prompt, forKey: .prompt)
        try values.encode(zone, forKey: .zone)
        try values.encode(max(frequency.rawValue, 0), forKey: .frequencyMinutes)
        if frequency != .onDemand {
            try values.encode(NexDate.wire(date), forKey: .scheduledAt)
        }
    }
}

struct NexArtifact: Decodable, Identifiable {
    let id: String
    let title: String
    let subtitle: String
    let date: String
    let kind: String
    let text: String
    let goalId: String?
}

struct WorkspaceHistory: Decodable {
    let items: [WorkspaceMessage]?
    let cursor: String?
    let watch: Bool?
    var nextBefore: String? = nil
}

struct WorkspaceHistoryPage: Decodable {
    let items: [WorkspaceMessage]
    let nextBefore: String?
}

struct WorkspaceMessage: Decodable, Identifiable {
    enum Role: String, Decodable { case user, assistant, update }
    enum CodingKeys: String, CodingKey { case id, role, text, date, sources, title, goalId, language, followUp }
    let id: String
    let role: Role
    let text: String
    let date: String
    let sources: [String]
    var title: String?
    var goalId: String?
    var language: String?
    var followUp: GoalFollowUp?
    var privateRequest: PrivateAnswer?
    var privateNotice = false
}

enum WorkspaceDate {
    static func instant(_ value: String) -> Date? {
        try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(value)
    }

    static func edition(_ day: String) -> String {
        guard let date = instant(day + "T12:00:00.000Z") else { return day }
        let format = Date.FormatStyle(date: .complete, time: .omitted, timeZone: .gmt)
        return date.formatted(format)
    }
}
