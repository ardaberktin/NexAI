import Foundation

enum WatchLink {
    static let request = "nex.watch.request.v1"
    static let response = "nex.watch.response.v1"
    static let accountScope = "nex.watch.account-scope.v1"
    static let enabled = "nex.watch.enabled.v1"
    static let clip = "nex.watch.clip.v1"
    static let acknowledgements = "nex.watch.saved.v1"
    static let syncError = "nex.watch.error.v1"
    static let recording = "nex.watch.recording.v1"
    static let recordingObservedAt = "nex.watch.recording-observed-at.v1"
    static let recordingChangedAt = "nex.watch.recording-changed-at.v1"
    static let conversing = "nex.watch.conversing.v1"
    static let questionDelivery = "nex.watch.questions.v1"
    static let questionAudio = "nex.watch.question-audio.v1"
    static let questionStatus = "nex.watch.question-status.v1"
    static let questionResult = "nex.watch.question-result.v1"
    static let immediateAudioLimit = 44_000
    static let maximumAudioBytes = 4_194_304
    static let responseChunkSize = 32_000
    static let maximumResponseBytes = 12_000_000

    static func envelope(_ data: Data, scope: String) -> Data {
        return header(scope) + data
    }

    static func payload(_ data: Data, scope: String?) -> Data? {
        guard let scope else { return nil }
        let prefix = header(scope)
        guard data.starts(with: prefix) else { return nil }
        return Data(data.dropFirst(prefix.count))
    }

    private static func header(_ scope: String) -> Data { Data(("nex.account." + scope + "\n").utf8) }

    static func isClipID(_ value: String) -> Bool {
        UUID(uuidString: value)?.uuidString.lowercased() == value
    }
}

struct WatchRecordingReport: Sendable {
    static let lifetime: TimeInterval = 30

    let recording: Bool
    let conversing: Bool
    let observedAt: Date
    let changedAt: Date?

    init(recording: Bool, observedAt: Date = .now, conversing: Bool = false, changedAt: Date? = nil) {
        self.recording = recording
        self.conversing = conversing
        self.observedAt = observedAt
        self.changedAt = changedAt
    }

    init?(_ context: [String: Any]) {
        guard let recording = context[WatchLink.recording] as? Bool,
            let observedAt = context[WatchLink.recordingObservedAt] as? Date
        else { return nil }
        let changedAt = context[WatchLink.recordingChangedAt] as? Date
        guard changedAt.map({ $0 <= observedAt }) != false else { return nil }
        self.init(
            recording: recording, observedAt: observedAt, conversing: context[WatchLink.conversing] as? Bool == true,
            changedAt: changedAt)
    }

    var context: [String: Any] {
        var context: [String: Any] = [
            WatchLink.recording: recording, WatchLink.recordingObservedAt: observedAt, WatchLink.conversing: conversing,
        ]
        context[WatchLink.recordingChangedAt] = changedAt
        return context
    }

    func expiration(receivedAt: Date) -> Date? {
        // Allow small clock differences without giving any report more than one lease.
        let age = receivedAt.timeIntervalSince(observedAt)
        guard (-5..<Self.lifetime).contains(age) else { return nil }
        return min(receivedAt, observedAt).addingTimeInterval(Self.lifetime)
    }
}

struct WatchRelayState: Sendable {
    let enabled: Bool
    let error: String?
    let supportsQuestions: Bool?
    let scope: String?

    init(_ context: [String: Any]) {
        scope = context[WatchLink.accountScope] as? String
        enabled = context[WatchLink.enabled] as? Bool ?? false
        error = context[WatchLink.syncError] as? String
        supportsQuestions = context[WatchLink.enabled] == nil ? nil : context[WatchLink.questionDelivery] as? Bool ?? false
    }
}

struct WatchQuestionResult: Codable, Sendable {
    let id: String
    let text: String?
    let language: String?
    let error: String?

    init?(_ recording: Recording) {
        if ["queued", "processing"].contains(recording.state) { return nil }
        id = recording.id
        text = recording.reply.map { String($0.voiceText.prefix(8_000)) }
        language = recording.reply?.language
        if let status = recording.reply?.statusMessage { error = status }
        else if text?.isEmpty == false { error = nil }
        else if recording.reply?.engine == "private_request" { error = "Open the private inbox to review this question." }
        else if recording.state == "awaiting_transcription" { error = "Speech recognition is unavailable on Excalibur." }
        else { error = recording.error ?? "Nex couldn’t make out that question. Try asking again." }
    }
}
