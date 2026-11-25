import Foundation

@MainActor
final class WatchConversationInput {
    var recording: Bool { capture.isRecording }
    private let capture: WatchCapture
    private var detector = WatchQuestionDetector()
    private var archive: LiveArchive?
    private var turn: String?
    private(set) var lastFrame = ContinuousClock.now

    enum Event { case began, submitted(String) }

    init(capture: WatchCapture) {
        self.capture = capture
    }

    func open(id: String, language: NexLanguage, completed: @escaping @Sendable (String?) -> Void) async throws
        -> AsyncThrowingStream<Data, Error> {
        let pcm = AsyncThrowingStream<Data, Error>.makeStream(bufferingPolicy: .bufferingOldest(40))
        let spool = capture.spool
        let ambient = try await capture.openLive(language: language, stream: pcm.continuation)
        try Task.checkCancellation()
        guard capture.spool.directory == spool.directory else { throw CancellationError() }
        archive = LiveArchive(spool: spool, language: language.rawValue, seed: Data(), ambient: ambient,
                              watchConversation: id, completed: completed)
        lastFrame = .now
        return pcm.stream
    }

    func stage(_ frame: Data, listening: Bool) throws -> Event? {
        lastFrame = .now
        try archive?.append(frame)
        let boundary = detector.accept(frame, listening: listening)
        if let boundary { return try handle(boundary) }
        archive?.progress(to: detector.safeThrough)
        return nil
    }

    func finishQuestion() throws -> Event? { try detector.finish().flatMap { try handle($0) } }

    private func handle(_ boundary: WatchQuestionDetector.Boundary) throws -> Event? {
        switch boundary {
        case .began(let sample):
            let id = nexID()
            turn = id
            archive?.start(id: id, sample: sample)
            return .began
        case .ended(let sample):
            guard let id = turn, let archive else { return nil }
            try archive.saveQuestion(id: id, sample: sample)
            turn = nil
            return .submitted(id)
        }
    }

    func close() {
        capture.closeLive()
        archive?.close(submitOpenTurn: false)
        archive = nil
        turn = nil
        detector = WatchQuestionDetector()
    }
}
