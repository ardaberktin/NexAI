import AVFoundation
import Combine
import Foundation
#if os(macOS)
import AppKit
#endif

@MainActor
final class Capture: ObservableObject {
    @Published private(set) var status = "Paused"
    @Published private(set) var ambient = false
    @Published private(set) var voice = false
    @Published private(set) var listeningForQuestion = false
    @Published private(set) var interrupted = false
    @Published private(set) var recording = false

    var notify: (() -> Void)?
    let questionChime = QuestionChime()
    var liveWake: LiveWakeHandler? {
        didSet { stream?.useLiveWake(liveWake) }
    }
    var ambientSpeech: AmbientSpeechHandler? {
        didSet { stream?.useAmbientSpeech(ambientSpeech) }
    }
    var standbyAudio: StandbyAudioHandler? {
        didSet { stream?.useStandbyAudio(standbyAudio) }
    }
    var liveInterrupted: (() -> Void)?
    private var live = false
    var liveMicrophoneActive = false {
        didSet {
            updateRecording()
            if ambient && liveMicrophoneActive { alerts.clear() }
        }
    }
    var language: NexLanguage = .auto {
        didSet { stream?.configure(options(for: .ambient)) }
    }
    var wake = false {
        didSet { stream?.configure(options(for: .ambient)) }
    }
    private var spool: Spool
    private var spoolGeneration = UUID()
    #if os(iOS)
    private let alerts = RecordingNotifications(device: .iphone)
    #else
    private let alerts = RecordingNotifications(device: .mac)
    #endif
    private var hasRecordedAmbient = false
    private var stream: MicrophoneStream? {
        didSet { updateRecording() }
    }
    private var streamID: UUID?
    private var interruptedAt: Date?
    private var playback = false
    private var observers = [AnyCancellable]()

    private func updateRecording() {
        let active = stream != nil || liveMicrophoneActive
        if active != recording { recording = active }
    }

    init(spool: Spool) {
        self.spool = spool
        observe(.default, .AVAudioEngineConfigurationChange) { [weak self] note in
            guard let self, let engine = note.object as? AVAudioEngine, engine === self.stream?.engine else { return }
            self.suspend("The microphone configuration changed.")
            self.resume()
        }
        #if os(iOS)
        observe(.default, AVAudioSession.interruptionNotification) { [weak self] note in
            let value = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            if value == AVAudioSession.InterruptionType.began.rawValue {
                self?.suspend("Recording interrupted by another audio session.")
            } else if value == AVAudioSession.InterruptionType.ended.rawValue {
                let options = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
                if AVAudioSession.InterruptionOptions(rawValue: options).contains(.shouldResume) {
                    self?.resume()
                }
            }
        }
        observe(.default, AVAudioSession.mediaServicesWereResetNotification) { [weak self] _ in
            self?.suspend("Recording interrupted when iOS reset audio services.")
            self?.resume()
        }
        observe(.default, AVAudioSession.mediaServicesWereLostNotification) { [weak self] _ in
            self?.suspend("The microphone became unavailable.")
        }
        #else
        observe(NSWorkspace.shared.notificationCenter, NSWorkspace.willSleepNotification) { [weak self] _ in
            self?.suspend("Mac went to sleep.")
        }
        observe(NSWorkspace.shared.notificationCenter, NSWorkspace.didWakeNotification) { [weak self] _ in
            self?.resume()
        }
        #endif
        recover()
    }

    func useSpool(_ next: Spool) {
        guard spool.directory != next.directory else { return }
        stopForAccountChange()
        spool = next
        recover()
    }

    func stopForAccountChange() {
        spoolGeneration = UUID()
        ambient = false
        voice = false
        live = false
        interrupted = false
        interruptedAt = nil
        hasRecordedAmbient = false
        alerts.clear()
        end()
        if !playback { deactivate() }
        status = "Paused"
    }

    private func observe(_ center: NotificationCenter, _ name: Notification.Name,
                         action: @escaping @MainActor (Notification) -> Void) {
        center.publisher(for: name).receive(on: DispatchQueue.main).sink { note in action(note) }
            .store(in: &observers)
    }

    private func recover() {
        do {
            for clip in try spool.clips() where clip.duration == nil {
                do {
                    if try SegmentAudio.recover(clip: clip, in: spool) {
                        reportGap("Recovered a partial clip after the recorder stopped unexpectedly.", at: clip.started)
                    } else {
                        reportGap("The app closed before this segment could be recovered.", at: clip.started)
                        try spool.remove(clip)
                    }
                } catch {
                    status = "Could not recover a local clip. " + error.localizedDescription
                }
            }
        } catch {
            status = "Could not open the local recording queue. " + error.localizedDescription
        }
    }

    private func permission() async -> Bool {
        #if os(iOS)
        await AVAudioApplication.requestRecordPermission()
        #else
        await AVCaptureDevice.requestAccess(for: .audio)
        #endif
    }

    func resumeRecording() async {
        let opening = spoolGeneration
        guard await permission() else {
            status = "Allow microphone access in system settings."
            return
        }
        guard opening == spoolGeneration else { return }
        ambient = true
        if interrupted {
            resume()
        } else if playback {
            status = "Capture will resume after playback"
        } else if !voice && stream == nil {
            start(.ambient)
        }
    }

    func pauseAmbient() {
        guard ambient else { return }
        ambient = false
        hasRecordedAmbient = false
        alerts.clear()
        interrupted = false
        interruptedAt = nil
        reportGap("Ambient capture paused.")
        guard !voice, !live else { return }
        end()
        if !playback { deactivate() }
        status = "Paused"
    }

    func toggleVoice(seed: LiveSeed? = nil) async {
        if voice {
            voice = false
            end()
            resumeAmbient()
            return
        }
        guard !playback, !live else { return }
        let opening = spoolGeneration
        guard await permission() else {
            status = "Allow microphone access in system settings."
            return
        }
        guard opening == spoolGeneration else { return }
        end()
        interrupted = false
        voice = true
        start(.voice, seed: seed)
    }

    func beginLive(wake: LiveWake?, isCurrent: () -> Bool) async throws -> LiveSeed? {
        guard await permission(), isCurrent(), !playback, !voice, !live else {
            throw NexError.capture("Live Conversation needs the microphone.")
        }
        live = true
        status = "Live Conversation"
        if let wake {
            return await withCheckedContinuation { continuation in
                end { continuation.resume(returning: wake.finish()) }
            }
        }
        end()
        return nil
    }

    func endLive() {
        guard live else { return }
        live = false
        resumeAmbient()
    }

    func beginPlayback() {
        playback = true
        if stream != nil {
            voice = false
            end()
            status = "Capture paused for playback"
            reportGap("Capture paused for local playback.")
        }
        #if os(iOS)
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
        } catch { status = error.localizedDescription }
        #endif
    }

    func endPlayback() {
        playback = false
        resumeAmbient()
    }

    private func activate(mode: CaptureMode) throws {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        var options: AVAudioSession.CategoryOptions = [.allowBluetoothHFP, .defaultToSpeaker]
        if mode == .ambient { options.insert(.mixWithOthers) }
        try session.setCategory(.playAndRecord, mode: .default, options: options)
        try session.setActive(true)
        #endif
    }

    private func deactivate() {
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private func start(_ mode: CaptureMode, seed: LiveSeed? = nil) {
        let identifier = UUID()
        do {
            try spool.verifySpace()
            try activate(mode: mode)
            let next = MicrophoneStream()
            try next.start(spool: spool, options: options(for: mode, seed: seed), clipReady: { [weak self] in
                Task { @MainActor in self?.notify?() }
            }, listening: { [weak self] active in
                Task { @MainActor in self?.wakeChanged(active, stream: identifier) }
            }, ended: { [weak self] error in
                Task { @MainActor in self?.captureEnded(identifier, error: error) }
            }, seed: seed, liveWake: liveWake, ambientSpeech: ambientSpeech, standbyAudio: standbyAudio)
            streamID = identifier
            stream = next
            if mode == .ambient {
                hasRecordedAmbient = true
                questionChime.prepare()
            }
            alerts.clear()
            status = mode == .ambient ? "Recording" : "Listening for your question"
        } catch {
            voice = false
            interrupted = ambient
            deactivate()
            status = error.localizedDescription
            if ambient && interruptedAt == nil { interruptedAt = .now }
            if ambient && hasRecordedAmbient { alerts.interrupted() }
            reportGap(status)
        }
    }

    private func wakeChanged(_ active: Bool, stream identifier: UUID) {
        guard streamID == identifier, ambient else { return }
        listeningForQuestion = active
        status = active ? "Listening for your question" : "Recording"
    }

    private func options(for mode: CaptureMode, seed: LiveSeed? = nil) -> RecordingOptions {
        RecordingOptions(mode: mode, wake: mode == .ambient ? wake : seed != nil,
                         language: language.rawValue, seconds: mode == .voice ? 20 : (wake ? 12 : 25),
                         openingChimed: seed?.openingChimed == true)
    }

    private func end(onStopped: (@Sendable () -> Void)? = nil) {
        let old = stream
        stream = nil
        streamID = nil
        listeningForQuestion = false
        if let old { old.stop(onStopped: onStopped) }
        else { onStopped?() }
    }

    private func captureEnded(_ identifier: UUID, error: String?) {
        guard streamID == identifier else { return }
        if let error {
            suspend(error)
        } else {
            end()
            voice = false
            resumeAmbient()
        }
    }

    private func suspend(_ message: String) {
        guard stream != nil || live, !interrupted else { return }
        if stream != nil || (ambient && liveMicrophoneActive) { alerts.interrupted() }
        end()
        voice = false
        interrupted = true
        interruptedAt = .now
        status = message
        reportGap(message)
        if live { liveInterrupted?() }
    }

    private func resume() {
        guard interrupted else { return }
        let started = interruptedAt
        interrupted = false
        resumeAmbient()
        if !interrupted { interruptedAt = nil }
        if stream != nil, let started {
            let elapsed = max(0, Int(Date.now.timeIntervalSince(started)))
            reportGap("Capture resumed after an interruption of approximately \(elapsed) seconds.")
        }
    }

    private func resumeAmbient() {
        guard !live else { return }
        if ambient && !interrupted && !playback && stream == nil {
            start(.ambient)
        } else if stream == nil && !interrupted && !playback {
            status = "Paused"
            deactivate()
        }
    }

    private func reportGap(_ message: String, at time: Date = .now) {
        do {
            try spool.save(Gap(id: nexID(), started: NexDate.wire(time), text: String(message.prefix(320))))
            notify?()
        } catch { status = "Could not save the recording event. " + error.localizedDescription }
    }
}
