#if os(macOS)
import AVFoundation
import Foundation
import OSLog

@MainActor
final class LiveAudio {
    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private let output = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!
    private let playback = PlaybackGate()
    private let log = Logger(subsystem: "com.ardaberktin.nex", category: "LiveAudio")
    private var installed = false
    private var stopped = false
    private var encoder: LivePCMEncoder?
    private(set) var usesEchoCancellation = false
    var isAudible: Bool { playback.isAudible }

    func start(output: AsyncThrowingStream<Data, Error>.Continuation) async throws {
        let version = ProcessInfo.processInfo.operatingSystemVersion
        // On macOS 27.0, failed voice processing can leave a replacement graph running
        // without microphone frames. Start with the standard route on that release.
        if version.majorVersion != 27 || version.minorVersion != 0 {
            do {
                try startRoute(output: output, voiceProcessing: true)
                return
            } catch {
                let detail = error as NSError
                log.notice("Using the standard microphone route after voice processing failed: \(detail.domain, privacy: .public) (\(detail.code, privacy: .public))")
                closeGraph()
                if engine.inputNode.isVoiceProcessingEnabled {
                    try? engine.inputNode.setVoiceProcessingEnabled(false)
                }
                engine = AVAudioEngine()
                player = AVAudioPlayerNode()
                // Core Audio restores the default devices asynchronously after releasing the aggregate.
                try await Task.sleep(for: .seconds(1))
                guard !stopped else { throw CancellationError() }
            }
        }
        do {
            try startRoute(output: output, voiceProcessing: false)
        } catch {
            stop()
            throw error
        }
    }

    private func startRoute(output: AsyncThrowingStream<Data, Error>.Continuation, voiceProcessing: Bool) throws {
        let input = engine.inputNode
        if voiceProcessing { try input.setVoiceProcessingEnabled(true) }
        let format = input.outputFormat(forBus: 0)
        guard (8_000...192_000).contains(format.sampleRate), format.channelCount > 0,
              format.commonFormat == .pcmFormatFloat32, !format.isInterleaved else {
            throw NexError.capture("Live voice could not open this microphone format.")
        }

        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: self.output)
        let encoder = try LivePCMEncoder(rate: format.sampleRate, output: output)
        self.encoder = encoder
        try MicrophoneTap.install(on: input, sampleRate: format.sampleRate,
                                  legacyBufferSeconds: 0.032) { [playback] list, frames, _ in
            encoder.copy(list, frames: frames, silence: !voiceProcessing && playback.isAudible)
        }
        installed = true
        engine.prepare()
        try engine.start()
        usesEchoCancellation = voiceProcessing
    }

    func play(_ data: Data) {
        guard !data.isEmpty, data.count.isMultiple(of: 2),
              let buffer = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: AVAudioFrameCount(data.count / 2)),
              let samples = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = buffer.frameCapacity
        data.withUnsafeBytes { bytes in
            for index in 0..<Int(buffer.frameLength) {
                let sample = Int16(littleEndian: bytes.loadUnaligned(fromByteOffset: index * 2, as: Int16.self))
                samples[index] = Float(sample) / 32_768
            }
        }
        let generation = playback.enqueue()
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [playback] _ in
            playback.complete(generation)
        }
        if !player.isPlaying { player.play() }
    }

    var routeNotification: Notification.Name { .AVAudioEngineConfigurationChange }
    var routeObject: AnyObject { engine }

    func needsRecovery(reason _: UInt?) -> Bool { !engine.isRunning }

    func interrupt() {
        player.stop()
        playback.clear()
    }

    func stop() {
        stopped = true
        closeGraph()
    }

    private func closeGraph() {
        encoder?.stop()
        encoder = nil
        interrupt()
        engine.stop()
        if installed {
            engine.inputNode.removeTap(onBus: 0)
            installed = false
        }
    }
}

/// Keep the live stream clock moving with silence while speakers play without echo cancellation.
private final class PlaybackGate: @unchecked Sendable {
    private let lock = NSLock()
    private var generation = 0
    private var pending = 0
    private var tail = DispatchTime(uptimeNanoseconds: 0)

    var isAudible: Bool {
        guard lock.try() else { return true }
        defer { lock.unlock() }
        return pending > 0 || DispatchTime.now() < tail
    }

    func enqueue() -> Int {
        lock.withLock {
            pending += 1
            return generation
        }
    }

    func complete(_ ticket: Int) {
        lock.withLock {
            guard ticket == generation, pending > 0 else { return }
            pending -= 1
            if pending == 0 { tail = .now() + .milliseconds(180) }
        }
    }

    func clear() {
        lock.withLock {
            generation += 1
            if pending > 0 { tail = .now() + .milliseconds(180) }
            pending = 0
        }
    }
}
#endif
