import AVFoundation
import Foundation

/// Serializes segment rollover and bounds the amount of audio waiting to be written.
final class WatchMicWriter: @unchecked Sendable {
    enum Event: Sendable {
        case clipReady
        case voice(Bool)
        case finished(String?)
    }

    private let queue = DispatchQueue(label: "nex.watch.microphone", qos: .userInitiated)
    private let slots = DispatchSemaphore(value: 8)
    private let gate = NSLock()
    private let spool: Spool
    private let rate: Double
    private let base: RecordingOptions
    private let report: @Sendable (Event) -> Void
    private var accepting = true
    private var didFinish = false
    private var expectedSample: Date?
    private lazy var writer = makeWriter(base)

    init(spool: Spool, rate: Double, options: RecordingOptions, report: @escaping @Sendable (Event) -> Void) {
        self.spool = spool
        self.rate = rate
        base = options
        self.report = report
    }

    func receive(_ input: UnsafeBufferPointer<Float>, time: AVAudioTime) {
        let frames = input.count
        gate.withLock {
            guard accepting else { return }
            guard slots.wait(timeout: .now()) == .success else {
                accepting = false
                queue.async { self.finish("Recording paused because the Watch could not write audio fast enough.") }
                return
            }
            let samples = Array(input)
            let now = Date.now
            let delay = time.isHostTimeValid
                ? AVAudioTime.seconds(forHostTime: mach_absolute_time()) - AVAudioTime.seconds(forHostTime: time.hostTime)
                : .infinity
            let timestamp = now.addingTimeInterval(abs(delay) < 1 ? -delay : -Double(frames) / rate)
            queue.async {
                defer { self.slots.signal() }
                guard !self.didFinish else { return }
                do {
                    if let expectedSample = self.expectedSample, timestamp.timeIntervalSince(expectedSample) > 2 {
                        try self.writer.finish()
                        self.writer.resetTimeline()
                    }
                    self.expectedSample = timestamp.addingTimeInterval(Double(frames) / self.rate)
                    if try samples.withUnsafeBufferPointer({ try self.writer.write($0, started: timestamp) }) {
                        try self.finishVoice()
                    }
                } catch { self.finish(error.localizedDescription) }
            }
        }
    }

    func toggleVoice() {
        queue.async {
            guard !self.didFinish else { return }
            do {
                if self.writer.options.mode == .voice {
                    try self.finishVoice()
                } else {
                    try self.writer.finish()
                    self.writer = self.makeWriter(RecordingOptions(mode: .voice, wake: false,
                                                                  language: self.base.language, seconds: 20))
                    self.report(.voice(true))
                }
            } catch { self.finish(error.localizedDescription) }
        }
    }

    func splitAmbient() {
        queue.async {
            guard !self.didFinish else { return }
            do {
                try self.writer.finish()
                self.writer.resetTimeline()
                self.expectedSample = nil
            } catch { self.finish(error.localizedDescription) }
        }
    }

    func stop() {
        gate.withLock {
            accepting = false
            queue.async { self.finish(nil) }
        }
    }

    private func finishVoice() throws {
        try writer.finish()
        report(.voice(false))
        if base.mode == .voice {
            finish(nil)
        } else {
            writer = makeWriter(base)
        }
    }

    private func makeWriter(_ options: RecordingOptions) -> RecordingSegments {
        RecordingSegments(spool: spool, sampleRate: rate, options: options) { [report] in
            report(.clipReady)
        }
    }

    private func finish(_ issue: String?) {
        guard !didFinish else { return }
        didFinish = true
        gate.withLock { accepting = false }
        var message = issue
        do { try writer.finish() }
        catch { message = error.localizedDescription }
        report(.finished(message))
    }
}
