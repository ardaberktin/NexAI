import AVFoundation

@MainActor
final class WatchSpokenReply: NSObject, AVSpeechSynthesizerDelegate, AVAudioPlayerDelegate {
    private struct Line {
        let id = UUID()
        let text: String
        let language: String?
        let sharingMicrophone: Bool
    }

    private let speech: AVSpeechSynthesizer
    private let makePlayer: (Data) throws -> AVAudioPlayer
    private let wait: Duration
    private var pending: Line?
    private var download: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var utterance: AVSpeechUtterance?
    private var player: AVAudioPlayer?
    private var modelLine: Line?
    private var modelStarted = ContinuousClock.now
    private var ownsSession = false
    var preparing: Bool { pending != nil }
    var audible: Bool { preparing || player != nil || utterance != nil }
    private(set) var interruptedAudio = false

    init(speech: AVSpeechSynthesizer = AVSpeechSynthesizer(), wait: Duration = .seconds(5),
         makePlayer: @escaping (Data) throws -> AVAudioPlayer = { try AVAudioPlayer(data: $0) }) {
        self.speech = speech
        self.wait = wait
        self.makePlayer = makePlayer
        super.init()
        speech.delegate = self
    }

    func speak(_ text: String, language: String?, sharingMicrophone: Bool,
               voice: @escaping @MainActor () async throws -> Data) {
        stop()
        guard !text.isEmpty else { return }
        let line = Line(text: text, language: language, sharingMicrophone: sharingMicrophone)
        let deadline = ContinuousClock.now.advanced(by: wait)
        pending = line
        timeout = Task { [weak self] in
            do { try await Task.sleep(until: deadline, clock: .continuous) } catch { return }
            self?.choose(nil, for: line)
        }
        download = Task { [weak self] in
            let bytes = try? await voice()
            guard !Task.isCancelled else { return }
            self?.choose(ContinuousClock.now < deadline ? bytes : nil, for: line)
        }
    }

    private func choose(_ bytes: Data?, for line: Line) {
        // The timeout and the transfer compete for the same line. Only the winner can start sound.
        guard pending?.id == line.id else { return }
        cancelWait()
        if let bytes, startModel(bytes, sharingMicrophone: line.sharingMicrophone) {
            modelLine = line
            modelStarted = .now
        } else { speakLocally(line) }
    }

    private func speakLocally(_ line: Line) {
        #if os(watchOS)
        speech.usesApplicationAudioSession = line.sharingMicrophone
        #endif
        let spoken = AVSpeechUtterance(string: line.text)
        if let language = line.language { spoken.voice = AVSpeechSynthesisVoice(language: language) }
        utterance = spoken
        speech.speak(spoken)
    }

    private func startModel(_ bytes: Data, sharingMicrophone: Bool) -> Bool {
        do {
            let audio = try makePlayer(bytes)
            audio.delegate = self
            #if os(watchOS)
            if !sharingMicrophone {
                let session = AVAudioSession.sharedInstance()
                try session.setCategory(.playback, mode: .spokenAudio)
                try session.setActive(true)
                ownsSession = true
            }
            #endif
            guard audio.prepareToPlay(), audio.play() else { releaseSession(); return false }
            player = audio
            return true
        } catch {
            releaseSession()
            return false
        }
    }

    func stop() {
        cancelWait()
        interruptedAudio = false
        modelLine = nil
        player?.stop()
        player = nil
        releaseSession()
        if utterance != nil {
            utterance = nil
            speech.stopSpeaking(at: .immediate)
        }
    }

    private func cancelWait() {
        pending = nil
        timeout?.cancel()
        timeout = nil
        download?.cancel()
        download = nil
    }

    private func releaseSession() {
        #if os(watchOS)
        if ownsSession { try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
        #endif
        ownsSession = false
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        finishedAudio(player, success: flag)
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        finishedAudio(player, success: false)
    }

    private nonisolated func finishedAudio(_ finished: AVAudioPlayer, success: Bool) {
        Task { @MainActor [weak self] in
            guard let self, player === finished, let line = modelLine else { return }
            player = nil
            modelLine = nil
            releaseSession()
            guard !success else { return }
            // A decoder can reject the clip immediately after play() accepted it.
            // After output has begun, only an explicit replay may restart the answer.
            if finished.currentTime == 0, ContinuousClock.now - modelStarted < .milliseconds(150) {
                speakLocally(line)
            } else { interruptedAudio = true }
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        completed(ObjectIdentifier(utterance))
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        completed(ObjectIdentifier(utterance))
    }

    private nonisolated func completed(_ id: ObjectIdentifier) {
        Task { @MainActor [weak self] in
            if let current = self?.utterance, ObjectIdentifier(current) == id { self?.utterance = nil }
        }
    }
}
