import AVFoundation
import Foundation

@MainActor
final class MicrophoneStream {
    let engine = AVAudioEngine()
    private var inputQueue: AudioInputQueue?

    func start(
        spool: Spool, options: RecordingOptions, clipReady: @escaping @Sendable () -> Void,
        listening: @escaping @Sendable (Bool) -> Void,
        ended: @escaping @Sendable (String?) -> Void, seed: LiveSeed? = nil,
        liveWake: LiveWakeHandler? = nil, ambientSpeech: AmbientSpeechHandler? = nil,
        standbyAudio: StandbyAudioHandler? = nil
    ) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard (8_000...192_000).contains(format.sampleRate), format.channelCount > 0,
              format.commonFormat == .pcmFormatFloat32, !format.isInterleaved else {
            throw NexError.capture("The microphone did not provide a supported audio format.")
        }
        let segments = RecordingSegments(spool: spool, sampleRate: format.sampleRate,
                                         options: options, clipReady: clipReady)
        let writer = CaptureAudio(writer: segments, listening: listening)
        writer.liveWake = liveWake
        writer.ambientSpeech = ambientSpeech
        writer.standbyAudio = standbyAudio
        if let seed { try writer.prepend(seed) }
        let receiver = AudioInputQueue(writer: writer, ended: ended)
        try MicrophoneTap.install(on: input, sampleRate: format.sampleRate, legacyBufferSeconds: 0.06) { list, frames, time in
            receiver.receive(list, frames: frames, time: time)
        }
        inputQueue = receiver
        do {
            engine.prepare()
            try engine.start()
        } catch {
            stop()
            throw error
        }
    }

    func stop(onStopped: (@Sendable () -> Void)? = nil) {
        guard let inputQueue else {
            onStopped?()
            return
        }
        self.inputQueue = nil
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        inputQueue.stop(onStopped: onStopped)
    }

    func configure(_ options: RecordingOptions) {
        inputQueue?.configure(options)
    }

    func useLiveWake(_ handler: LiveWakeHandler?) {
        inputQueue?.useLiveWake(handler)
    }

    func useAmbientSpeech(_ handler: AmbientSpeechHandler?) {
        inputQueue?.useAmbientSpeech(handler)
    }

    func useStandbyAudio(_ handler: StandbyAudioHandler?) {
        inputQueue?.useStandbyAudio(handler)
    }
}

/// The tap only copies samples into a fixed pool. File writes and AAC encoding run on the writer queue.
private final class AudioInputQueue: @unchecked Sendable {
    private final class Block: @unchecked Sendable {
        let samples: UnsafeMutablePointer<Float>
        private let capacity: Int

        init(capacity: Int) {
            self.capacity = capacity
            samples = .allocate(capacity: capacity)
            samples.initialize(repeating: 0, count: capacity)
        }

        deinit {
            samples.deinitialize(count: capacity)
            samples.deallocate()
        }
    }

    private let capacity: Int
    private let queue = DispatchQueue(label: "nex.capture.writer", qos: .userInitiated)
    private let gate = NSLock()
    private let writer: CaptureAudio
    private let ended: @Sendable (String?) -> Void
    // Available blocks and accepting are lock-protected. The serial writer owns didFinish.
    private var available: [Block]
    private var accepting = true
    private var didFinish = false

    init(writer: CaptureAudio, ended: @escaping @Sendable (String?) -> Void) {
        self.writer = writer
        self.ended = ended
        let capacity = max(8_192, Int(writer.sampleRate * 0.42))
        self.capacity = capacity
        self.available = (0..<12).map { _ in Block(capacity: capacity) }
    }

    func receive(_ list: UnsafePointer<AudioBufferList>, frames: Int, time: AVAudioTime) {
        guard frames > 0 else { return }
        gate.lock()
        defer { gate.unlock() }
        guard accepting else { return }
        guard frames <= capacity, let block = available.popLast() else {
            accepting = false
            queue.async {
                self.finish(error: "The microphone buffer overflowed. Resume capture to continue.")
            }
            return
        }
        let channels = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: list))
        let expectedBytes = frames * MemoryLayout<Float>.size
        guard !channels.isEmpty, channels.allSatisfy({ $0.mData != nil && $0.mDataByteSize >= expectedBytes }) else {
            available.append(block)
            accepting = false
            queue.async { self.finish(error: "The microphone returned an incomplete audio buffer.") }
            return
        }
        copy(channels: channels, frames: frames, into: block.samples)
        let started = frameStart(time, fallbackFrames: frames)
        queue.async {
            defer {
                self.gate.lock()
                self.available.append(block)
                self.gate.unlock()
            }
            guard !self.didFinish else { return }
            do {
                let data = UnsafeBufferPointer(start: block.samples, count: frames)
                if try self.writer.write(data, started: started) { self.finish() }
            } catch {
                self.finish(error: error.localizedDescription)
            }
        }
    }

    private func copy(channels: UnsafeMutableAudioBufferListPointer, frames: Int,
                      into destination: UnsafeMutablePointer<Float>) {
        guard let first = channels[0].mData?.assumingMemoryBound(to: Float.self) else { return }
        destination.update(from: first, count: frames)
        let count = channels.count
        guard count > 1 else { return }
        for channel in channels.dropFirst() {
            guard let values = channel.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for frame in 0..<frames { destination[frame] += values[frame] }
        }
        let scale = 1 / Float(count)
        for frame in 0..<frames { destination[frame] *= scale }
    }

    private func frameStart(_ time: AVAudioTime, fallbackFrames: Int) -> Date {
        let anchor = Date.now
        if time.isHostTimeValid {
            let current = AVAudioTime.seconds(forHostTime: mach_absolute_time())
            let first = AVAudioTime.seconds(forHostTime: time.hostTime)
            let distance = current - first
            if abs(distance) < 1 { return anchor.addingTimeInterval(-distance) }
        }
        return anchor.addingTimeInterval(-Double(fallbackFrames) / writer.sampleRate)
    }

    func stop(onStopped: (@Sendable () -> Void)?) {
        gate.lock()
        defer { gate.unlock() }
        accepting = false
        queue.async {
            self.finish()
            onStopped?()
        }
    }

    func configure(_ options: RecordingOptions) {
        queue.async { self.writer.configure(options) }
    }

    func useLiveWake(_ handler: LiveWakeHandler?) {
        queue.async { self.writer.liveWake = handler }
    }

    func useAmbientSpeech(_ handler: AmbientSpeechHandler?) {
        queue.async { self.writer.ambientSpeech = handler }
    }

    func useStandbyAudio(_ handler: StandbyAudioHandler?) {
        queue.async { self.writer.standbyAudio = handler }
    }

    private func finish(error: String? = nil) {
        guard !didFinish else { return }
        didFinish = true
        gate.lock()
        accepting = false
        gate.unlock()
        do {
            try writer.finish()
            ended(error)
        } catch let failure {
            ended(error ?? failure.localizedDescription)
        }
    }
}
