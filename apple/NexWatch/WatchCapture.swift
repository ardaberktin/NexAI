import AVFoundation
import Combine
import Foundation
import OSLog

@MainActor
final class WatchCapture: ObservableObject {
    @Published private(set) var started: Date?
    @Published private(set) var voice = false
    @Published private(set) var busy = false
    @Published private(set) var needsResume = UserDefaults.standard.bool(forKey: "watch.recording")
    @Published private(set) var issue: String?
    @Published private(set) var voiceQueued = false

    var clipReady: (() -> Void)?
    var liveInterrupted: ((String) -> Void)?
    var recordingChanged: (() -> Void)?
    var isForeground = false
    var isRecording: Bool { started != nil }
    private(set) var spool: Spool
    private var engine: AVAudioEngine?
    private var writer: WatchMicWriter?
    private var microphone: WatchMicrophoneInput?
    private let alerts = RecordingNotifications(device: .watch)
    private var ambientRecording = false
    private let log = Logger(subsystem: "com.ardaberktin.nex.watchkitapp", category: "WatchCapture")
    private var captureLanguage: NexLanguage?
    private var ambientRecovery: Task<Void, Never>?
    private var token: UUID?
    private var observers: [NSObjectProtocol] = []
    private var preparation: Task<String?, Never>?
    private var playback: (() -> Void)?

    init(spool: Spool) {
        self.spool = spool
        prepare()
        observe(.interruption) { [weak self] in self?.interrupt("Recording was interrupted. Tap Resume recording.") }
        observe(AVAudioSession.mediaServicesWereLostNotification) { [weak self] in
            self?.interrupt("The microphone became unavailable.")
        }
        observe(AVAudioSession.mediaServicesWereResetNotification) { [weak self] in
            self?.interrupt("The microphone restarted. Tap Resume recording.")
        }
        observe(.AVAudioEngineConfigurationChange) { [weak self] in self?.checkEngine() }
    }

    func useSpool(_ next: Spool) {
        pause()
        spool = next
        prepare()
    }

    private func prepare() {
        let spool = self.spool
        preparation = Task.detached(priority: .utility) {
            do {
                var issue: String?
                for clip in try spool.clips() where clip.duration == nil {
                    do {
                        if try !SegmentAudio.recover(clip: clip, in: spool) { try spool.remove(clip) }
                    } catch { issue = error.localizedDescription }
                }
                return issue
            } catch { return error.localizedDescription }
        }
        Task {
            let result = await preparation?.value
            guard self.spool.directory == spool.directory else { return }
            issue = result
            clipReady?()
        }
    }

    func start(language: NexLanguage, mode: CaptureMode = .ambient) async {
        await startInput(language: language, mode: mode, live: nil)
    }

    func startRecording(language: NexLanguage) async {
        await start(language: language)
        guard ambientRecording else { return }
        await RecordingNotifications.requestAccessIfNeeded()
        refreshRecordingAlert()
    }

    func openLive(language: NexLanguage, stream: AsyncThrowingStream<Data, Error>.Continuation) async throws -> Bool {
        guard isForeground, !busy, !voice else {
            throw NexError.capture("Finish the current recording action and try Ask Nex again.")
        }
        if let microphone, let engine, engine.isRunning {
            try microphone.openLive(stream)
            return true
        }
        await startInput(language: language, mode: .ambient, live: stream)
        guard engine?.isRunning == true else {
            throw NexError.capture(issue ?? "Nex couldn’t open the Watch microphone.")
        }
        return false
    }

    func closeLive() {
        guard let microphone, microphone.closeLive() else { return }
        if writer == nil {
            stopInput()
        } else if engine?.isRunning != true || microphone.stalled(for: .seconds(4)) {
            interrupt("The Watch microphone stopped during the conversation.", recoverAmbient: true)
        }
    }

    private func startInput(language: NexLanguage, mode: CaptureMode,
                            live: AsyncThrowingStream<Data, Error>.Continuation?) async {
        guard isForeground, engine == nil, writer == nil, !busy, !Task.isCancelled else { return }
        busy = true
        defer { busy = false }
        let spool = self.spool
        _ = await preparation?.value
        guard await AVAudioApplication.requestRecordPermission() else {
            issue = "Allow microphone access for Nex in Watch Settings."
            return
        }
        guard self.spool.directory == spool.directory, isForeground, !Task.isCancelled else { return }
        playback?()
        do {
            try spool.verifySpace()
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .default)
            try session.setActive(true)
            let engine = AVAudioEngine()
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard (8_000...192_000).contains(format.sampleRate), format.channelCount > 0,
                  format.commonFormat == .pcmFormatFloat32, !format.isInterleaved else {
                throw NexError.capture("The Watch microphone is unavailable.")
            }
            let token = UUID()
            let options = RecordingOptions(mode: mode, wake: false, language: language.rawValue,
                                           seconds: mode == .ambient ? 45 : 20)
            let writer = live == nil
                ? WatchMicWriter(spool: spool, rate: format.sampleRate, options: options) { [weak self] event in
                    Task { @MainActor in self?.receive(event, token: token) }
                }
                : nil
            let microphone = makeMicrophone(writer: writer, rate: format.sampleRate, token: token)
            if let live { try microphone.openLive(live) }
            try MicrophoneTap.install(on: input, sampleRate: format.sampleRate, legacyBufferSeconds: 0.1) { list, frames, time in
                microphone.receive(list, frames: frames, time: time)
            }
            do { try engine.start() }
            catch {
                input.removeTap(onBus: 0)
                writer?.stop()
                throw error
            }
            self.engine = engine
            self.writer = writer
            self.microphone = microphone
            captureLanguage = language
            self.token = token
            voice = mode == .voice
            voiceQueued = false
            started = .now
            ambientRecording = live == nil && mode == .ambient
            if ambientRecording {
                needsResume = false
                UserDefaults.standard.set(true, forKey: "watch.recording")
                alerts.watchRecordingStarted(at: .now)
            } else if writer != nil && !needsResume {
                alerts.clear()
            }
            recordingChanged?()
            issue = nil
        } catch {
            deactivate()
            issue = error.localizedDescription
        }
    }

    private func makeMicrophone(writer: WatchMicWriter?, rate: Double, token: UUID) -> WatchMicrophoneInput {
        WatchMicrophoneInput(writer: writer, rate: rate) { [weak self] receivedAt in
            Task { @MainActor in
                guard let self, self.token == token, self.ambientRecording else { return }
                self.alerts.watchAudioReceived(at: receivedAt)
            }
        }
    }

    func pause() {
        ambientRecovery?.cancel()
        UserDefaults.standard.set(false, forKey: "watch.recording")
        needsResume = false
        alerts.clear()
        stopInput()
    }

    func toggleVoice(language: NexLanguage) async {
        voiceQueued = false
        if let writer, isRecording {
            writer.toggleVoice()
        } else {
            await start(language: language, mode: .voice)
        }
    }

    func checkEngine(checkForStall: Bool = false) {
        guard let engine, let microphone else { return }
        if !engine.isRunning || (checkForStall && ambientRecording && microphone.stalled(for: .seconds(12))) {
            interrupt("The microphone stopped. Tap Resume recording.", recoverAmbient: microphone.isLive)
        }
    }

    func refreshRecordingAlert() {
        guard ambientRecording else { return }
        if let receivedAt = microphone?.lastAudioAt {
            alerts.watchAudioReceived(at: receivedAt)
        } else {
            alerts.watchRecordingStarted(at: .now)
        }
    }

    func setPlaybackStop(_ action: @escaping () -> Void) { playback = action }

    private func interrupt(_ message: String, recoverAmbient: Bool = false) {
        guard isRecording else { return }
        let recoveryLanguage = recoverAmbient && writer != nil ? captureLanguage : nil
        if writer != nil { alerts.interrupted() }
        issue = message
        needsResume = UserDefaults.standard.bool(forKey: "watch.recording")
        stopInput()
        if let recoveryLanguage { restartAmbient(language: recoveryLanguage) }
    }

    private func restartAmbient(language: NexLanguage) {
        guard ambientRecovery == nil, needsResume, isForeground else { return }
        log.notice("Reopening ambient capture after the Watch live input stopped.")
        ambientRecovery = Task { [weak self] in
            guard let self else { return }
            defer { ambientRecovery = nil }
            let deadline = ContinuousClock.now.advanced(by: .seconds(4))
            while writer != nil && ContinuousClock.now < deadline {
                do { try await Task.sleep(for: .milliseconds(50)) }
                catch { return }
            }
            guard needsResume, !Task.isCancelled else { return }
            await start(language: language)
        }
    }

    private func stopInput() {
        guard let engine else { return }
        let wasLive = microphone?.closeLive() == true
        microphone = nil
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        self.engine = nil
        captureLanguage = nil
        ambientRecording = false
        started = nil
        recordingChanged?()
        voice = false
        writer?.stop()
        if writer == nil { token = nil }
        deactivate()
        if wasLive { liveInterrupted?(issue ?? "The Watch microphone stopped.") }
    }

    private func receive(_ event: WatchMicWriter.Event, token: UUID) {
        if case .clipReady = event {
            clipReady?()
            return
        }
        guard token == self.token else { return }
        switch event {
        case .clipReady: break
        case .voice(let isVoice):
            if isRecording { voice = isVoice }
            if !isVoice { voiceQueued = true }
        case .finished(let error):
            if let error, isRecording { interrupt(error) }
            else {
                if let error { issue = error }
                stopInput()
            }
            writer = nil
            self.token = nil
            clipReady?()
        }
    }

    private func deactivate() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func observe(_ name: Notification.Name, action: @escaping @MainActor () -> Void) {
        observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { note in
            if name == AVAudioSession.interruptionNotification,
               note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt != AVAudioSession.InterruptionType.began.rawValue {
                return
            }
            Task { @MainActor in action() }
        })
    }
}

private extension Notification.Name {
    static let interruption = AVAudioSession.interruptionNotification
}
