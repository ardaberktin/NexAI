import AVFoundation
import Foundation
import Speech

@MainActor
final class OfflineRecognition {
    private var continuation: CheckedContinuation<String, any Error>?
    private var task: SFSpeechRecognitionTask?
    private var deadline: Task<Void, Never>?

    func transcribe(_ audio: URL, language: NexLanguage) async throws -> String {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: language.locale)),
              recognizer.supportsOnDeviceRecognition else { throw NexError.offlineSpeech }
        let permission = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0) }
        }
        guard permission == .authorized,
              recognizer.supportsOnDeviceRecognition, recognizer.isAvailable else { throw NexError.offlineSpeech }
        let request = SFSpeechURLRecognitionRequest(url: audio)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            task = recognizer.recognitionTask(with: request) { [weak self] result, error in
                let text = result?.isFinal == true ? result?.bestTranscription.formattedString : nil
                Task { @MainActor [weak self] in
                    if let text { self?.finish(.success(text)) }
                    else if let error { self?.finish(.failure(error)) }
                }
            }
            deadline = Task { [weak self] in
                try? await Task.sleep(for: .seconds(18))
                guard !Task.isCancelled else { return }
                self?.finish(.failure(NexError.offlineSpeech))
            }
        }
    }

    private func finish(_ result: Result<String, any Error>) {
        guard let continuation else { return }
        self.continuation = nil
        deadline?.cancel()
        deadline = nil
        task?.cancel()
        task = nil
        continuation.resume(with: result)
    }
}

@MainActor
final class DeviceSpeech: NSObject, AVSpeechSynthesizerDelegate, AVAudioPlayerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var player: AVAudioPlayer?
    private var queuedAudio = [Data]()
    private var receivingAudio = false
    private var utteranceID: ObjectIdentifier?
    private var onEnd: (() -> Void)?
    var active: Bool { onEnd != nil }

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    private func voice(for language: String) -> AVSpeechSynthesisVoice? {
        let locale = language == "tr" ? "tr-TR" : "en-US"
        let voices = AVSpeechSynthesisVoice.speechVoices()
        return voices.filter { $0.language.hasPrefix(language + "-") }.sorted {
            if $0.quality != $1.quality { return $0.quality.rawValue > $1.quality.rawValue }
            if ($0.language == locale) != ($1.language == locale) { return $0.language == locale }
            return $0.identifier < $1.identifier
        }.first
    }

    func speak(_ text: String, language: String, onEnd: @escaping () -> Void) -> Bool {
        guard let voice = voice(for: language) else { return false }
        self.onEnd = onEnd
        let line = AVSpeechUtterance(string: text)
        line.voice = voice
        utteranceID = ObjectIdentifier(line)
        synthesizer.speak(line)
        return true
    }

    func play(_ data: Data, onEnd: @escaping () -> Void) throws {
        try startAudio(data)
        self.onEnd = onEnd
    }

    func beginAudioStream(onEnd: @escaping () -> Void) {
        receivingAudio = true
        self.onEnd = onEnd
    }

    func enqueueAudio(_ data: Data) throws {
        if player != nil {
            queuedAudio.append(data)
        } else {
            try startAudio(data)
        }
    }

    func endAudioStream() {
        receivingAudio = false
        if player == nil { finish() }
    }

    private func startAudio(_ data: Data) throws {
        let next = try AVAudioPlayer(data: data)
        next.delegate = self
        guard next.play() else { throw NexError.capture("Could not play this audio.") }
        player = next
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
        player?.stop()
        finish()
    }

    private func finish() {
        let callback = onEnd
        onEnd = nil
        utteranceID = nil
        player = nil
        receivingAudio = false
        queuedAudio.removeAll()
        callback?()
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let id = ObjectIdentifier(utterance)
        Task { @MainActor [weak self] in
            guard let self, self.utteranceID == id else { return }
            self.finish()
        }
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let id = ObjectIdentifier(player)
        Task { @MainActor [weak self] in self?.audioFinished(id) }
    }

    private func audioFinished(_ id: ObjectIdentifier) {
        guard let player, ObjectIdentifier(player) == id else { return }
        self.player = nil
        while !queuedAudio.isEmpty {
            if (try? startAudio(queuedAudio.removeFirst())) != nil { return }
        }
        if !receivingAudio { finish() }
    }
}
