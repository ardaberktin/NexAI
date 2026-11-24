import Foundation

/// A finished clip can span several interactive WatchConnectivity messages.
struct WatchReplyAudio {
    static let key = "nex.watch.reply-voice.v1"
    static let limit = 1_048_576
    private static let chunkSize = WatchLink.immediateAudioLimit

    let id: String
    private(set) var audio = Data()
    private var total: Int?
    var complete: Bool { total == audio.count }

    init(id: String) { self.id = id }

    static func packet(id: String, bytes: Data, offset: Int) -> [String: Any] {
        guard (1...limit).contains(bytes.count), (0..<bytes.count).contains(offset) else { return [:] }
        let end = min(offset + chunkSize, bytes.count)
        return [key: id, "offset": offset, "total": bytes.count, "audio": bytes.subdata(in: offset..<end)]
    }

    mutating func append(_ packet: [String: Any]) throws {
        guard packet[Self.key] as? String == id, packet["offset"] as? Int == audio.count,
              let length = packet["total"] as? Int, (1...Self.limit).contains(length),
              total == nil || total == length, let part = packet["audio"] as? Data,
              !part.isEmpty, part.count == min(Self.chunkSize, length - audio.count) else {
            throw NexError.capture("Nex voice is unavailable for this reply.")
        }
        total = length
        audio.append(part)
    }
}
