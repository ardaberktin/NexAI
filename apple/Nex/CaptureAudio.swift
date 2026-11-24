import Foundation
import NexVoiceCapture

/// Owns the journal writer and the active voice turn on the microphone's serial queue.
final class CaptureAudio {
    private let journal: RecordingSegments
    private let listening: @Sendable (Bool) -> Void
    var liveWake: LiveWakeHandler? {
        didSet { configureWakeDetector() }
    }
    var ambientSpeech: AmbientSpeechHandler? {
        didSet { speech.reset() }
    }
    var standbyAudio: StandbyAudioHandler?
    private var speech = AmbientSpeech()
    private var lastLiveWake = Date.distantPast
    private var pendingLiveWake: LiveWake?
    private var options: RecordingOptions
    private var wakeWord: WakeWord?
    private var turn: RecordingSegments?
    private var endpoint = VoiceEndpoint()
    private var preRoll: [Float] = []
    private let preRollFrames: Int

    var sampleRate: Double { journal.sampleRate }

    init(writer: RecordingSegments, listening: @escaping @Sendable (Bool) -> Void) {
        journal = writer
        options = writer.options
        self.listening = listening
        preRollFrames = Int(writer.sampleRate * 1.6)
        configure(options)
    }

    func configure(_ next: RecordingOptions) {
        guard options.mode == .ambient else { return }
        options = next
        configureWakeDetector()
        journal.configure(next)
    }

    private func configureWakeDetector() {
        if options.mode == .ambient && (options.wake || liveWake != nil) {
            if wakeWord == nil { wakeWord = WakeWord() }
        } else {
            wakeWord = nil
            preRoll.removeAll(keepingCapacity: true)
        }
    }

    func write(_ samples: UnsafeBufferPointer<Float>, started: Date) throws -> Bool {
        if let turn {
            if try writeVoice(turn, samples: samples, started: started) {
                self.turn = nil
                listening(false)
                wakeWord?.reset()
            }
            return false
        }
        if options.mode == .voice {
            return try writeVoice(journal, samples: samples, started: started)
        }
        if let pendingLiveWake, pendingLiveWake.append(samples) {
            return try journal.write(samples, started: started)
        }
        pendingLiveWake = nil
        if let standbyAudio {
            let pcm = LiveSeed(samples: Array(samples), rate: sampleRate).pcm()
            if !pcm.isEmpty { standbyAudio(pcm) }
        }
        if let wakeWord {
            remember(samples)
            if wakeWord.accept(samples, rate: Int32(sampleRate)) {
                speech.reset()
                let ending = started.addingTimeInterval(Double(samples.count) / sampleRate)
                if let liveWake {
                    let opened = startLiveWake(liveWake, ending: ending)
                    _ = try journal.write(samples, started: started)
                    if opened {
                        try journal.finish(suppressWake: true)
                        journal.resetTimeline()
                    }
                } else {
                    try startRecordedTurn(ending: ending)
                }
                return false
            }
        }
        if let ambientSpeech, let utterance = speech.accept(samples, rate: sampleRate) {
            ambientSpeech(utterance, started.addingTimeInterval(Double(samples.count) / sampleRate))
        }
        return try journal.write(samples, started: started)
    }

    func finish() throws {
        try journal.finish(suppressWake: turn != nil || pendingLiveWake != nil)
        if let turn {
            self.turn = nil
            try turn.finish()
            listening(false)
        }
    }

    private func remember(_ samples: UnsafeBufferPointer<Float>) {
        preRoll.append(contentsOf: samples)
        if preRoll.count > preRollFrames { preRoll.removeFirst(preRoll.count - preRollFrames) }
    }

    func prepend(_ seed: LiveSeed) throws {
        guard options.mode == .voice else { return }
        let samples = seed.resample(to: sampleRate)
        _ = try samples.withUnsafeBufferPointer {
            try journal.write($0, started: Date.now.addingTimeInterval(-Double(samples.count) / sampleRate))
        }
    }

    private func startLiveWake(_ notify: LiveWakeHandler, ending: Date) -> Bool {
        guard ending.timeIntervalSince(lastLiveWake) > 3 else { return false }
        lastLiveWake = ending
        let wake = LiveWake(samples: preRoll, rate: sampleRate)
        pendingLiveWake = wake
        notify(wake)
        preRoll.removeAll(keepingCapacity: true)
        wakeWord?.reset()
        return true
    }

    private func startRecordedTurn(ending: Date) throws {
        try journal.finish(suppressWake: true)
        journal.resetTimeline()
        let voice = RecordingOptions(mode: .voice, wake: true, language: options.language, seconds: 20)
        let writer = RecordingSegments(spool: journal.spool, sampleRate: sampleRate, options: voice,
                                       clipReady: journal.clipReady)
        endpoint = VoiceEndpoint()
        turn = writer
        listening(true)
        let starting = ending.addingTimeInterval(-Double(preRoll.count) / sampleRate)
        _ = try preRoll.withUnsafeBufferPointer { try writer.write($0, started: starting) }
        preRoll.removeAll(keepingCapacity: true)
    }

    private func writeVoice(_ writer: RecordingSegments, samples: UnsafeBufferPointer<Float>,
                            started: Date) throws -> Bool {
        if try writer.write(samples, started: started) { return true }
        guard endpoint.accept(samples, rate: sampleRate) else { return false }
        try writer.finish()
        return true
    }
}

typealias LiveWakeHandler = @Sendable (LiveWake) -> Void

/// Keeps the wake phrase and the audio recorded while the live socket connects.
final class LiveWake: @unchecked Sendable {
    private let lock = NSLock()
    private let rate: Double
    private let capacity: Int
    private var samples: [Float]
    private var collecting = true

    init(samples: [Float], rate: Double) {
        self.samples = samples
        self.rate = rate
        capacity = Int(rate * 8)
    }

    func append(_ next: UnsafeBufferPointer<Float>) -> Bool {
        lock.withLock {
            guard collecting, samples.count < capacity else { return false }
            samples.append(contentsOf: next.prefix(capacity - samples.count))
            return true
        }
    }

    func finish() -> LiveSeed {
        lock.withLock {
            collecting = false
            return LiveSeed(samples: samples, rate: rate)
        }
    }
}

/// Ends a turn after sustained quiet, with time for the question to begin after the wake phrase.
struct VoiceEndpoint {
    private var elapsed = 0.0
    private var voiced = 0.0
    private var quiet = 0.0

    mutating func accept(_ samples: UnsafeBufferPointer<Float>, rate: Double) -> Bool {
        guard !samples.isEmpty, rate > 0 else { return false }
        let duration = Double(samples.count) / rate
        let energy = samples.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(samples.count)
        elapsed += duration
        if energy >= 0.000064 {
            voiced += duration
            quiet = 0
        } else {
            quiet += duration
        }
        return voiced >= 0.1 ? quiet >= 0.65 : elapsed >= 3.5
    }
}
