import Foundation
import WatchConnectivity

@MainActor
enum PhoneWatchTransfers {
    static func receipt(_ id: String, connection: NexAPI) async throws -> Recording {
        try await connection.get("/v1/watch/recordings/" + id + "/receipt")
    }

    static func deliver(_ clip: Clip, inbox: PhoneWatchInbox, session: WCSession, connection: NexAPI,
                        voice: PhoneWatchReplyVoice, scope: String?, isCurrent: () -> Bool) async throws -> Bool {
        let question = clip.watchConversation != nil
        var row: Recording
        if question, inbox.submitted(clip) {
            row = try await receipt(clip.id, connection: connection)
        } else {
            row = try await connection.upload(clip, data: inbox.audio(clip), fromWatch: true)
            guard isCurrent() else { return false }
            if question { try inbox.markSubmitted(clip) }
        }
        guard isCurrent(), !Task.isCancelled else { return false }
        guard question else { return true }

        // Finish short answers during the background time granted for the incoming Watch transfer.
        let deadline = ContinuousClock.now.advanced(by: .seconds(16))
        while WatchQuestionResult(row) == nil && ContinuousClock.now < deadline {
            try await Task.sleep(for: .seconds(1))
            guard isCurrent() else { return false }
            row = try await receipt(clip.id, connection: connection)
        }
        guard let answer = WatchQuestionResult(row), isCurrent(), !Task.isCancelled,
              session.activationState == .activated else { return false }
        if session.isReachable, answer.text != nil { voice.prepare(clip.id, connection: connection) }
        var message: [String: Any] = [WatchLink.questionResult: try JSONEncoder().encode(answer)]
        message[WatchLink.accountScope] = scope
        session.transferUserInfo(message)
        if session.isReachable { session.sendMessage(message, replyHandler: nil, errorHandler: nil) }
        return true
    }
}
