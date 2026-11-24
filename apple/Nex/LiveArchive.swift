import Foundation

/// Turn boundaries and ambient audio share one sample timeline so the recordings do not overlap.
@MainActor
final class LiveArchive {
    private static let rate = 16_000
    private var bytes = Data()
    private var offset = 0
    private var current: (id: String, start: Int)?
    private let seedFrames: Int
    private let writer: LiveArchiveWriter
    private let maximumFrames: Int
    private var closed = false

    private var end: Int { offset + bytes.count / 2 }

    init(spool: Spool, language: String, seed: Data, ambient: Bool, watchConversation: String? = nil,
         completed: @escaping @Sendable (String?) -> Void) {
        seedFrames = seed.count / 2
        maximumFrames = Self.rate * (watchConversation == nil ? 40 : 65)
        bytes = seed
        writer = LiveArchiveWriter(spool: spool, language: language, ambient: ambient,
                                   clock: .now.addingTimeInterval(-Double(seedFrames) / Double(Self.rate)),
                                   watchConversation: watchConversation, completed: completed)
    }

    func append(_ audio: Data) throws {
        guard !closed else { return }
        bytes.append(audio)
        guard end - offset < maximumFrames else {
            throw NexError.capture("Excalibur fell behind. Recording is continuing locally.")
        }
    }

    func progress(to sample: Int) {
        guard current == nil, sample - offset >= Self.rate * 2 else { return }
        consumeAmbient(to: min(sample, end))
    }

    func progressThroughCapturedAudio() { progress(to: end) }

    func start(id: String, sample: Int) {
        guard !closed, UUID(uuidString: id) != nil, sample >= offset, sample < end, current == nil else { return }
        consumeAmbient(to: sample, finish: true)
        current = (id, sample)
    }

    func finish(id: String, sample: Int) {
        guard let turn = current, turn.id == id else { return }
        finishTurn(turn, through: min(sample, end))
    }

    /// A pending watch question can be restored as soon as its ticket is published.
    func saveQuestion(id: String, sample: Int) throws {
        let last = min(sample, end)
        guard let turn = current, turn.id == id, last - turn.start >= 2_880 else {
            throw NexError.capture("The watch couldn’t save that question. Try speaking again.")
        }
        current = nil
        let clip = try writer.stageVoice(take(to: last), at: turn.start, id: id)
        writer.encodeStaged(clip)
    }

    func close(submitOpenTurn: Bool = true) {
        guard !closed else { return }
        closed = true
        if submitOpenTurn, let turn = current { finishTurn(turn, through: end) }
        consumeAmbient(to: end, finish: true)
    }

    private func finishTurn(_ turn: (id: String, start: Int), through last: Int) {
        current = nil
        guard last - turn.start >= 2_880 else { return }
        writer.voice(take(to: last), at: turn.start, id: turn.id)
    }

    private func consumeAmbient(to sample: Int, finish: Bool = false) {
        let first = min(sample, max(seedFrames, offset))
        if first > offset { _ = take(to: first) }
        let data = sample > offset ? take(to: sample) : Data()
        writer.ambient(data, at: first, finish: finish)
    }

    private func take(to sample: Int) -> Data {
        let count = (sample - offset) * 2
        let result = Data(bytes.prefix(count))
        bytes.removeFirst(count)
        offset = sample
        return result
    }
}

/// Audio encoding stays off the live WebSocket and reuses the crash-recoverable journal writer.
private final class LiveArchiveWriter: @unchecked Sendable {
    private let queue = DispatchQueue(label: "nex.live.archive", qos: .utility)
    private let spool: Spool
    private let language: String
    private let watchConversation: String?
    private let clock: Date
    private let journal: RecordingSegments?
    private let completed: @Sendable (String?) -> Void
    private let rate = 16_000.0

    init(spool: Spool, language: String, ambient: Bool, clock: Date, watchConversation: String?,
         completed: @escaping @Sendable (String?) -> Void) {
        self.spool = spool
        self.language = language
        self.watchConversation = watchConversation
        self.clock = clock
        self.completed = completed
        journal = ambient ? RecordingSegments(
            spool: spool, sampleRate: 16_000,
            options: RecordingOptions(mode: .ambient, wake: false, language: language, seconds: 25),
            clipReady: { completed(nil) }
        ) : nil
    }

    func ambient(_ data: Data, at start: Int, finish: Bool) {
        guard let journal, !data.isEmpty || finish else { return }
        queue.async {
            do {
                if !data.isEmpty {
                    _ = try self.samples(data).withUnsafeBufferPointer {
                        try journal.write($0, started: self.date(at: start))
                    }
                }
                if finish {
                    try journal.finish()
                    journal.resetTimeline()
                }
            } catch { self.completed(error.localizedDescription) }
        }
    }

    func voice(_ data: Data, at start: Int, id: String) {
        queue.async {
            do {
                let clip = try self.stageVoice(data, at: start, id: id)
                try self.encode(clip)
            } catch { self.completed(error.localizedDescription) }
        }
    }

    func stageVoice(_ data: Data, at start: Int, id: String) throws -> Clip {
        try spool.verifySpace()
        let clip = Clip(id: id, started: date(at: start), mode: .voice, wake: false,
                        language: language, sampleRate: rate, timeZone: NexDate.timeZone,
                        liveHandled: watchConversation == nil, watchConversation: watchConversation)
        try spool.save(clip)
        try samples(data).withUnsafeBytes { try Data($0).write(to: spool.pcm(id), options: .atomic) }
        return clip
    }

    func encodeStaged(_ clip: Clip) {
        queue.async {
            do { try self.encode(clip) }
            catch { self.completed(error.localizedDescription) }
        }
    }

    private func encode(_ clip: Clip) throws {
        if try SegmentAudio.recover(clip: clip, in: spool) { completed(nil) }
    }

    private func date(at sample: Int) -> Date {
        clock.addingTimeInterval(Double(sample) / rate)
    }

    private func samples(_ data: Data) -> [Float] {
        data.withUnsafeBytes { bytes in
            stride(from: 0, to: bytes.count, by: 2).map { index in
                let value = Int16(littleEndian: bytes.loadUnaligned(fromByteOffset: index, as: Int16.self))
                return Float(value) / 32_768
            }
        }
    }
}
