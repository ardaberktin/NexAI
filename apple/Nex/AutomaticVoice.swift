import AVFoundation
import Foundation
#if os(macOS)
import CoreAudio
#endif

struct AutomaticOpening {
    let ticket: String
    var selective = false
    var namedOverMedia = false
    var alreadyChimed = false
}

@MainActor
final class RecordingAttention {
    typealias Context = () -> (api: NexAPI, language: String)?
    typealias Open = (AutomaticOpening) async -> Void
    private let standby = ContinuousVoice()
    private let clips = AutomaticVoice()
    private var generation = 0

    func configure(capture: Capture, enabled: Bool, continuous: Bool, context: @escaping Context, open: @escaping Open) {
        cancel()
        generation += 1
        let version = generation
        capture.standbyAudio = nil
        capture.ambientSpeech = nil
        guard enabled else { return }
        if continuous {
            let chime = capture.questionChime
            capture.standbyAudio = { [weak self] frame in
                Task { @MainActor in
                    self?.sendStandby(frame, version: version, chime: chime, context: context, open: open)
                }
            }
        }
        capture.ambientSpeech = { [weak self] sample, ended in
            Task { @MainActor in
                guard let self, self.generation == version, !self.standby.ready, let current = context() else { return }
                self.clips.consider(sample, ended: ended, api: current.api, language: current.language, isCurrent: {
                    guard let latest = context() else { return false }
                    return self.generation == version && latest.api === current.api
                        && latest.language == current.language && !self.standby.ready
                }, open: { ticket in await open(AutomaticOpening(ticket: ticket)) })
            }
        }
    }

    private func sendStandby(_ frame: Data, version: Int, chime: QuestionChime,
                             context: @escaping Context, open: @escaping Open) {
        guard generation == version, let current = context() else { return }
        let isCurrent = { [weak self] in self?.generation == version && context()?.api === current.api }
        standby.send(frame, connect: {
            try current.api.liveSocket(path: "/v1/listen", language: current.language, wakeChime: chime.available)
        }, onWake: {
            isCurrent() && chime.play()
        }) { ticket, named, chimed in
            let media = OtherAudio.isPlaying
            guard isCurrent(), named || !media else {
                await current.api.declineAttention(ticket)
                return
            }
            await open(AutomaticOpening(ticket: ticket, selective: true, namedOverMedia: media, alreadyChimed: chimed))
        }
    }

    func cancel() {
        clips.cancel()
        standby.stop()
    }
}

@MainActor
final class AutomaticVoice {
    private var checking: Task<Void, Never>?
    private var attempt: UUID?
    private var pending: (sample: AmbientUtterance, ended: Date)?

    func consider(_ sample: AmbientUtterance, ended: Date, api: NexAPI, language: String,
                  isCurrent: @escaping () -> Bool, open: @escaping (String) async -> Void) {
        guard Date.now.timeIntervalSince(ended) < 8, !OtherAudio.isPlaying else { return }
        if checking != nil {
            pending = (sample, ended)
            return
        }
        let identifier = UUID()
        attempt = identifier
        checking = Task { [weak self] in
            defer {
                if let self, self.attempt == identifier { self.clear() }
            }
            let (audio, context) = await Task.detached(priority: .utility) {
                (sample.speech.pcm(), sample.context.pcm())
            }.value
            guard !Task.isCancelled else { return }
            let ticket = try? await api.attention(audio, language: language, context: context)
            guard !Task.isCancelled, let self, self.attempt == identifier, isCurrent(), !OtherAudio.isPlaying else {
                if let ticket { await api.declineAttention(ticket) }
                return
            }
            let next = self.pending
            self.clear()
            if let ticket {
                await open(ticket)
            } else if let next {
                self.consider(next.sample, ended: next.ended, api: api, language: language,
                              isCurrent: isCurrent, open: open)
            }
        }
    }

    func cancel() {
        checking?.cancel()
        clear()
    }

    private func clear() {
        checking = nil
        attempt = nil
        pending = nil
    }
}

enum OtherAudio {
    static var isPlaying: Bool {
        #if os(iOS)
        AVAudioSession.sharedInstance().isOtherAudioPlaying
        #else
        if #available(macOS 14.2, *) { externalMacAudio() } else { false }
        #endif
    }

    #if os(macOS)
    @available(macOS 14.2, *)
    private static func externalMacAudio() -> Bool {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                                  mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
        let system = AudioObjectID(kAudioObjectSystemObject)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr,
              size > 0, Int(size) % MemoryLayout<AudioObjectID>.stride == 0 else { return false }
        var processes = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.stride)
        let status = processes.withUnsafeMutableBytes { bytes in
            AudioObjectGetPropertyData(system, &address, 0, nil, &size, bytes.baseAddress!)
        }
        guard status == noErr else { return false }
        let ownPID = ProcessInfo.processInfo.processIdentifier
        return processes.contains { process in
            var pid = pid_t(0)
            guard read(process, kAudioProcessPropertyPID, &pid), pid > 0, pid != ownPID else { return false }
            var output: UInt32 = 0
            return read(process, kAudioProcessPropertyIsRunningOutput, &output) && output != 0
        }
    }

    private static func read<T>(_ process: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) -> Bool {
        var property = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                   mElement: kAudioObjectPropertyElementMain)
        var bytes = UInt32(MemoryLayout<T>.size)
        return withUnsafeMutableBytes(of: &value) {
            AudioObjectGetPropertyData(process, &property, 0, nil, &bytes, $0.baseAddress!) == noErr
        }
    }
    #endif
}
