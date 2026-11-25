import Foundation

enum NexError: LocalizedError {
    case connection(String)
    case api(Int, String)
    case capture(String)
    case offlineSpeech

    var errorDescription: String? {
        switch self {
        case .connection(let message), .api(_, let message), .capture(let message): message
        case .offlineSpeech: "An on-device speech recognizer is unavailable for this language."
        }
    }
}

enum CaptureMode: String, Codable, Sendable {
    case ambient, voice
}

enum RecordingDevice: String, CaseIterable {
    case mac, iphone, watch

    var title: String {
        switch self {
        case .mac: "Mac"
        case .iphone: "iPhone"
        case .watch: "Apple Watch"
        }
    }

    static func name(_ device: String) -> String {
        Self(rawValue: device)?.title ?? device
    }
}

enum NexLanguage: String, CaseIterable, Identifiable {
    case auto, en, tr
    var id: String { rawValue }
    var title: String {
        switch self {
        case .auto: "Automatic"
        case .en: "English"
        case .tr: "Turkish"
        }
    }
    var locale: String { self == .tr ? "tr-TR" : "en-US" }
}

struct Clip: Codable, Identifiable, Sendable {
    let id: String
    let started: Date
    let mode: CaptureMode
    var wake: Bool
    let language: String
    var duration: Double?
    var sampleRate: Double?
    var timeZone: String?
    var liveHandled: Bool?
    var openingChimed: Bool?
    var watchConversation: String?
}

struct Gap: Codable, Identifiable {
    let id: String
    let started: String
    let text: String
}

struct Reply: Codable, Sendable {
    let text: String
    let language: String
    let sources: [String]
    let engine: String
    var speaker: String? = nil
    var goalId: String? = nil
    var approvalNotice: String? = nil

    var voiceText: String { engine == "private_request" ? approvalNotice ?? "" : text }

    var statusMessage: String? {
        switch engine {
        case "model_unavailable": "A response from the local model is unavailable."
        case "web_verification_failed": "Web sources could not verify a response."
        case "private_queue_full": "The private inbox is full. Review pending questions to free up space."
        default: nil
        }
    }
}

struct VoiceStreamEvent: Decodable {
    let kind: String
    let audio: Data?
    let reply: Reply?
    let state: String?
}

struct Recording: Codable, Identifiable {
    let id: String
    let started: String
    let mode: CaptureMode
    let state: String
    let transcript: String?
    let error: String?
    let notes: String?
    let reply: Reply?
    let audioAvailable: Bool?
    var device: String? = nil
}

struct TranscriptSnapshot: Decodable {
    let text: String?
    let state: String
    let revision: Int
    let history: [TranscriptRevision]

    var editable: Bool { state != "queued" && state != "processing" }
}

struct TranscriptRevision: Decodable, Identifiable {
    let number: Int
    let changed: String
    let editor: String
    let previousText: String?
    var id: Int { number }
}

struct TranscriptCorrection: Encodable {
    let id: String
    let revision: Int
    let text: String
}

struct MemoryItem: Codable, Identifiable {
    let id: String
    let kind: String
    let started: String
    let device: String
    let text: String
    let sources: [String]
    let engine: String?
    let audioAvailable: Bool?
    let webUrl: String?
    let webTitle: String?
    let webLinks: [WebLink]?
}

struct WebLink: Codable {
    let title: String
    let url: String
}

struct ListResponse<Item: Decodable>: Decodable {
    let items: [Item]
}

struct ModelStatus: Decodable {
    let chat: String
    let transcription: String
    let speech: [String]
}

enum WorkspaceFeature: String, CaseIterable, Identifiable {
    case daybook, activities, feed, ideas, goals

    var id: Self { self }
}

struct WorkspaceFeatures: Codable, Equatable {
    private let values: [String: Bool]

    init(from decoder: Decoder) throws {
        values = try decoder.singleValueContainer().decode([String: Bool].self)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(values)
    }

    subscript(feature: WorkspaceFeature) -> Bool { values[feature.rawValue] ?? true }
}

struct ServerStatus: Decodable {
    let recordings: [String: Int]
    let remainingBytes: Int
    let models: ModelStatus
    let connections: [String: String]?
    let accountWriteAccess: [String: Bool]?
    let workspaceFeatures: WorkspaceFeatures?
}

struct DeviceClockReading: Codable, Sendable {
    let unixSeconds: Int
    let utcOffsetSeconds: Int
    let timeZone: String

    private enum CodingKeys: String, CodingKey {
        case unixSeconds = "unix_seconds", utcOffsetSeconds = "utc_offset_seconds", timeZone = "time_zone"
    }

    static var current: Self {
        let date = Date.now
        let zone = TimeZone.current
        return Self(unixSeconds: Int(date.timeIntervalSince1970), utcOffsetSeconds: zone.secondsFromGMT(for: date),
                    timeZone: zone.identifier)
    }
}

enum NexDate {
    static var timeZone: String {
        let identifier = TimeZone.current.identifier
        return TimeZone.knownTimeZoneIdentifiers.contains(identifier) ? identifier : "UTC"
    }

    static func wire(_ date: Date = .now) -> String {
        date.ISO8601Format(.iso8601(timeZone: .gmt).year().month().day().time(includingFractionalSeconds: true)
            .timeSeparator(.colon).timeZone(separator: .colon))
    }

    static func display(_ value: String) -> String {
        let style = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        guard let date = try? style.parse(value) else { return value }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    static func timeRange(_ started: String, _ ended: String) -> String {
        let format = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        guard let start = try? format.parse(started), let end = try? format.parse(ended) else { return started }
        let first = start.formatted(date: .omitted, time: .shortened)
        let last = end.formatted(date: .omitted, time: .shortened)
        return first == last ? first : "\(first) – \(last)"
    }
}

func nexID() -> String { UUID().uuidString.lowercased() }
