import Foundation

@main
enum WatchQuestionDeliveryCheck {
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "nex-watch-test-" + nexID())
        defer { try? FileManager.default.removeItem(at: root) }
        let spool = try Spool(directory: root.appending(path: "watch"))
        let conversation = nexID()
        let id = nexID()
        try await recordQuestion(spool: spool, conversation: conversation, id: id)
        let clips = try spool.clips()
        guard clips.count == 1, let clip = clips.first else { fatalError("Expected one recorded question") }
        precondition(clip.id == id && clip.watchConversation == conversation && clip.liveHandled == false)
        precondition(abs((clip.duration ?? 0) - 2.5) < 0.02 && spool.playableDuration(id) != nil)
        try testDelivery(clip, in: spool, root: root)
        try testReply(conversation: conversation, id: id, root: root)
        testTurnLimits()
        print("Offline capture, playable AAC, duplicate delivery, restart, delayed reply, and turn limits passed.")
    }

    @MainActor
    private static func recordQuestion(spool: Spool, conversation: String, id: String) async throws {
        let ready = AsyncStream<String?>.makeStream()
        let archive = LiveArchive(spool: spool, language: "en", seed: Data(), ambient: false,
                                  watchConversation: conversation, completed: { ready.continuation.yield($0) })
        var detector = WatchQuestionDetector()
        let quiet = frame(level: 12)
        let voice = frame(level: 2200)
        // Start with 70 seconds without a phone, then speak and pause, with no network callbacks.
        for index in 0..<727 {
            let frame = (700..<712).contains(index) ? voice : quiet
            try archive.append(frame)
            switch detector.accept(frame, listening: true) {
            case .began(let start): archive.start(id: id, sample: start)
            case .ended(let end):
                try archive.saveQuestion(id: id, sample: end)
                precondition(spool.fm.fileExists(atPath: spool.pcm(id).path) || spool.fm.fileExists(atPath: spool.audio(id).path))
            case nil: archive.progress(to: detector.safeThrough)
            }
        }
        archive.close(submitOpenTurn: false)
        let timeout = Task {
            try? await Task.sleep(for: .seconds(10))
            if !Task.isCancelled { ready.continuation.finish() }
        }
        defer { timeout.cancel() }
        var iterator = ready.stream.makeAsyncIterator()
        guard let error = await iterator.next() else { fatalError("Watch encoder timed out") }
        precondition(error == nil, error ?? "")
    }

    private static func testDelivery(_ clip: Clip, in spool: Spool, root: URL) throws {
        let folder = root.appending(path: "phone")
        let inbox = try PhoneWatchInbox(directory: folder)
        let audio = try Data(contentsOf: spool.audio(clip.id))
        let metadata = try JSONEncoder().encode(clip)
        try inbox.importQuestion(metadata, audio: audio)
        try inbox.markSubmitted(clip)
        let reopened = try PhoneWatchInbox(directory: folder)
        try reopened.importClip(metadata, from: spool.audio(clip.id))
        try reopened.importQuestion(metadata, audio: audio)
        let delivered = try reopened.clips()
        let saved = try reopened.audio(clip)
        precondition(delivered.map(\.id) == [clip.id] && reopened.submitted(clip) && saved == audio)
    }

    private static func testReply(conversation: String, id: String, root: URL) throws {
        let url = root.appending(path: "question.json")
        try WatchQuestionTicket(conversation: conversation, id: id, submitted: .now).save(to: url)
        guard var pending = WatchQuestionTicket.restore(from: url) else { fatalError("Lost pending question") }
        func receipt(id: String, state: String, reply: Reply? = nil) -> Recording {
            Recording(id: id, started: "", mode: .voice, state: state, transcript: nil, error: nil,
                      notes: nil, reply: reply, audioAvailable: nil)
        }
        precondition(WatchQuestionResult(receipt(id: id, state: "processing")) == nil)
        let line = Reply(text: "Hello there.", language: "en", sources: [], engine: "local_model")
        guard let stale = WatchQuestionResult(receipt(id: nexID(), state: "ready", reply: line)),
              let answer = WatchQuestionResult(receipt(id: id, state: "ready", reply: line)) else {
            fatalError("Ready answer missing")
        }
        precondition(!pending.accept(stale) && pending.accept(answer) && !pending.accept(answer))
        try pending.save(to: url)
        precondition(WatchQuestionTicket.restore(from: url)?.result?.text == line.text)
    }

    private static func testTurnLimits() {
        let voice = frame(level: 2400)
        var detector = WatchQuestionDetector()
        for _ in 0..<15 { precondition(detector.accept(voice, listening: false) == nil) }
        precondition(detector.accept(voice, listening: true) == nil)
        precondition(detector.accept(voice, listening: true) == .began(15 * 1600))
        precondition(detector.finish() == .ended(17 * 1600) && detector.finish() == nil)
        var ended = false
        for _ in 0..<600 {
            if case .ended = detector.accept(voice, listening: true) { ended = true }
        }
        precondition(ended, "Long questions must fit in a single transferable clip")
    }

    private static func frame(level: Int16) -> Data {
        (0..<1600).map { $0 % 40 < 20 ? level : -level }.withUnsafeBytes { Data($0) }
    }
}
