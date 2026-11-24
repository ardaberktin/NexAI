import Foundation
import WatchConnectivity

extension WatchSync {
    func replyAudio(_ id: String) async throws -> Data {
        guard enabled, let session, session.activationState == .activated else {
            throw NexError.capture("Nex voice is unavailable.")
        }
        let account = scope
        var clip = WatchReplyAudio(id: id)
        repeat {
            try Task.checkCancellation()
            guard account == scope else { throw CancellationError() }
            var request: [String: Any] = [WatchReplyAudio.key: id, "offset": clip.audio.count]
            request[WatchLink.accountScope] = account
            let packet: [String: Any]
            do { packet = try await voicePacket(request, session: session) }
            catch {
                try await Task.sleep(for: .milliseconds(160))
                packet = try await voicePacket(request, session: session)
            }
            try Task.checkCancellation()
            guard account == scope else { throw CancellationError() }
            try clip.append(packet)
        } while !clip.complete
        return clip.audio
    }

    private func voicePacket(_ request: [String: Any], session: WCSession) async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { continuation in
            session.sendMessage(request, replyHandler: { continuation.resume(returning: $0) },
                                errorHandler: { continuation.resume(throwing: $0) })
        }
    }
}
