import Foundation

/// Receipt delivery and audio requests share each in-flight render and its completed bytes.
@MainActor
final class PhoneWatchReplyVoice {
    private struct Render {
        let connection: NexAPI
        let expires: Date
        let task: Task<Data, Error>
    }
    private var renders: [String: Render] = [:]

    func reset() {
        renders.values.forEach { $0.task.cancel() }
        renders.removeAll()
    }

    @discardableResult
    func prepare(_ id: String, connection: NexAPI) -> Task<Data, Error> {
        renders = renders.filter { $0.value.expires > .now && $0.value.connection === connection }
        if let existing = renders[id] { return existing.task }
        if renders.count >= 3, let oldest = renders.min(by: { $0.value.expires < $1.value.expires })?.key {
            renders.removeValue(forKey: oldest)?.task.cancel()
        }
        let task = Task {
            try await connection.checkPrivacyVersion(live: true)
            let bytes = try await connection.bytes("/v1/watch/recordings/" + id + "/voice", timeout: 9, port: 8767)
            guard (1...WatchReplyAudio.limit).contains(bytes.count) else {
                throw NexError.capture("Nex voice is unavailable for this reply.")
            }
            return bytes
        }
        renders[id] = Render(connection: connection, expires: .now.addingTimeInterval(90), task: task)
        return task
    }

    func packet(_ id: String, offset: Int, connection: NexAPI) async -> [String: Any] {
        guard WatchLink.isClipID(id), (0..<WatchReplyAudio.limit).contains(offset) else { return [:] }
        let job = prepare(id, connection: connection)
        do {
            return try await WatchReplyAudio.packet(id: id, bytes: job.value, offset: offset)
        } catch {
            if renders[id]?.task == job { renders.removeValue(forKey: id) }
            return [:]
        }
    }
}
