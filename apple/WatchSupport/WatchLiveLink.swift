import Foundation

enum WatchLiveLink {
    static let prefix = Data("nex.watch.live.v1\n".utf8)
    static let maximumMessage = 32_000
    static let inputBatch = 12_800
    static let outputBatch = 12_000
    static let compactInputBatch = 32_000 * 3
    static let inputBacklog = 32_000 * 12
    static let outputBacklog = 48_000 * 35

    static func isBinary(_ data: Data) -> Bool { data.starts(with: "bplist00".utf8) }

    static func encode<T: Encodable>(_ value: T, binary: Bool) throws -> Data {
        if binary {
            let encoder = PropertyListEncoder()
            encoder.outputFormat = .binary
            return try encoder.encode(value)
        }
        return try JSONEncoder().encode(value)
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        guard data.count <= maximumMessage else { throw NexError.capture("The live relay message was too large.") }
        if isBinary(data) { return try PropertyListDecoder().decode(type, from: data) }
        return try JSONDecoder().decode(type, from: data)
    }
}

struct WatchLiveRequest: Codable, Sendable {
    enum Action: String, Codable, Sendable { case start, exchange, end }

    let action: Action
    let id: String
    var language: String?
    var timeZone: String?
    var audio: Data?
    var interrupt: Bool?
    var compactAudio: Bool?
    // Zero on start negotiates replay-safe exchanges. Older companions omit the acknowledgement.
    var sequence: Int?

    func wire(binary: Bool = false) throws -> Data {
        WatchLiveLink.prefix + (try WatchLiveLink.encode(self, binary: binary))
    }

    init(_ action: Action, id: String) {
        self.action = action
        self.id = id
    }
}

struct WatchLiveUpdate: Codable, Sendable {
    var events: [WatchLiveEvent] = []
    var ended = false
    var error: String?
    var compactAudio: Bool?
    var sequence: Int?
}

struct WatchLiveEvent: Codable, Sendable {
    enum Kind: String, Codable, Sendable {
        case ready, turn, turnEnd, reply, progress, chime, stopAudio, audio
    }

    let kind: Kind
    var id: String?
    var sample: Int?
    var text: String?
    var audio: Data?
    var language: String?
}
