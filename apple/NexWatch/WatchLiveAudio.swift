import AVFoundation
import Foundation

/// Switches the existing microphone tap between the ambient writer and the live encoder.
final class WatchMicrophoneInput: @unchecked Sendable {
    private let gate = NSLock()
    private let writer: WatchMicWriter?
    private let rate: Double
    private var live: LivePCMEncoder?
    private let openedAt = ContinuousClock.now
    private var lastFrame: ContinuousClock.Instant?
    private var lastProgress: ContinuousClock.Instant?
    private let audioProgress: (@Sendable (ContinuousClock.Instant) -> Void)?

    var isLive: Bool { gate.withLock { live != nil } }
    var lastAudioAt: ContinuousClock.Instant? { gate.withLock { lastFrame } }

    init(writer: WatchMicWriter?, rate: Double, audioProgress: (@Sendable (ContinuousClock.Instant) -> Void)? = nil) {
        self.writer = writer
        self.rate = rate
        self.audioProgress = audioProgress
    }

    func stalled(for duration: Duration) -> Bool { ContinuousClock.now - (lastAudioAt ?? openedAt) > duration }

    func openLive(_ stream: AsyncThrowingStream<Data, Error>.Continuation) throws {
        let encoder = try LivePCMEncoder(rate: rate, output: stream)
        gate.withLock {
            writer?.splitAmbient()
            live = encoder
        }
    }

    func closeLive() -> Bool {
        gate.withLock {
            guard let live else { return false }
            live.stop()
            self.live = nil
            return true
        }
    }

    func receive(_ list: UnsafePointer<AudioBufferList>, frames: Int, time: AVAudioTime) {
        guard frames > 0, let address = list.pointee.mBuffers.mData else { return }
        let samples = UnsafeBufferPointer(start: address.assumingMemoryBound(to: Float.self), count: frames)
        let receivedAt = ContinuousClock.now
        let shouldReport = gate.withLock {
            lastFrame = receivedAt
            if let live { live.copy(samples) }
            else { writer?.receive(samples, time: time) }
            guard audioProgress != nil, lastProgress.map({ receivedAt - $0 >= .seconds(8) }) ?? true else {
                return false
            }
            lastProgress = receivedAt
            return true
        }
        if shouldReport { audioProgress?(receivedAt) }
    }
}
