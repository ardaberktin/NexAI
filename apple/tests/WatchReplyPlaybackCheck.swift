import AVFoundation
import Foundation

@main
enum WatchReplyPlaybackCheck {
    @MainActor
    static func main() async throws {
        let file = FileManager.default.temporaryDirectory.appending(path: nexID() + ".wav")
        defer { try? FileManager.default.removeItem(at: file) }
        let format = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800)!
        buffer.frameLength = buffer.frameCapacity
        buffer.floatChannelData![0].initialize(repeating: 0, count: Int(buffer.frameLength))
        try AVAudioFile(forWriting: file, settings: format.settings).write(from: buffer)
        let fixture = try Data(contentsOf: file)
        try checkPackets()
        try await checkSelection(fixture)
        try await checkLateAudio(fixture)
        print("Nex voice preference, Apple fallback, replay, interruption, and duplicate delivery checks passed.")
    }

    private static func checkPackets() throws {
        let id = nexID()
        let source = Data(repeating: 50, count: WatchLink.immediateAudioLimit * 2 + 7)
        var clip = WatchReplyAudio(id: id)
        let first = WatchReplyAudio.packet(id: id, bytes: source, offset: 0)
        try clip.append(first)
        for invalid in [first, WatchReplyAudio.packet(id: nexID(), bytes: source, offset: clip.audio.count)] {
            do { try clip.append(invalid); fatalError("Accepted a duplicate or mismatched audio packet") }
            catch is NexError { }
        }
        while !clip.complete {
            try clip.append(WatchReplyAudio.packet(id: id, bytes: source, offset: clip.audio.count))
        }
        precondition(clip.audio == source)
    }

    @MainActor
    private static func checkSelection(_ fixture: Data) async throws {
        let speech = SpeechCounter()
        var players: [ModelPlayer] = []
        let playback = WatchSpokenReply(speech: speech) { data in
            guard data == fixture else { throw URLError(.cannotDecodeContentData) }
            let player = try ModelPlayer(data: data)
            players.append(player)
            return player
        }
        playback.speak("Model", language: "en", sharingMicrophone: true) { fixture }
        await settled(playback)
        precondition(players.count == 1 && players[0].starts == 1 && speech.lines.isEmpty)
        playback.audioPlayerDidFinishPlaying(players[0], successfully: true)
        await Task.yield()
        precondition(!playback.audible && speech.lines.isEmpty)

        playback.speak("Replay", language: "en", sharingMicrophone: true) { fixture }
        await settled(playback)
        playback.audioPlayerDecodeErrorDidOccur(players[0], error: nil)
        await Task.yield()
        precondition(playback.audible && players[1].starts == 1 && speech.lines.isEmpty)
        players[1].position = 1.0
        playback.audioPlayerDecodeErrorDidOccur(players[1], error: nil)
        await Task.yield()
        precondition(playback.interruptedAudio && !playback.audible && speech.lines.isEmpty)

        playback.speak("Decoder failed before output", language: "en", sharingMicrophone: true) { fixture }
        await settled(playback)
        playback.audioPlayerDecodeErrorDidOccur(players[2], error: nil)
        await Task.yield()
        precondition(speech.lines.count == 1)
        playback.speak("Unreadable clip", language: "en", sharingMicrophone: true) { Data() }
        await settled(playback)
        precondition(speech.lines.count == 2)
    }

    @MainActor
    private static func checkLateAudio(_ fixture: Data) async throws {
        let speech = SpeechCounter()
        var starts = 0
        let playback = WatchSpokenReply(speech: speech, wait: .milliseconds(50)) { data in
            starts += 1
            return try ModelPlayer(data: data)
        }
        var late: CheckedContinuation<Data, Never>?
        playback.speak("Slow clip", language: "en", sharingMicrophone: false) {
            await withCheckedContinuation { late = $0 }
        }
        while late == nil { await Task.yield() }
        await settled(playback)
        precondition(speech.lines.count == 1)
        playback.speechSynthesizer(speech, didFinish: speech.lines[0])
        late?.resume(returning: fixture)
        await Task.yield()
        precondition(!playback.audible && starts == 0 && speech.lines.count == 1)

        late = nil
        playback.speak("Interrupted", language: "en", sharingMicrophone: true) {
            await withCheckedContinuation { late = $0 }
        }
        while late == nil { await Task.yield() }
        playback.stop()
        late?.resume(returning: fixture)
        try await Task.sleep(for: .milliseconds(80))
        precondition(!playback.audible && starts == 0 && speech.lines.count == 1)
    }

    @MainActor
    private static func settled(_ playback: WatchSpokenReply) async {
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while playback.preparing && ContinuousClock.now < deadline { await Task.yield() }
        precondition(!playback.preparing)
    }
}

private final class ModelPlayer: AVAudioPlayer {
    var starts = 0
    var position = 0.0
    override var currentTime: TimeInterval {
        get { position }
        set { position = newValue }
    }
    override func prepareToPlay() -> Bool { true }
    override func play() -> Bool { starts += 1; return true }
    override func stop() {}
}

private final class SpeechCounter: AVSpeechSynthesizer {
    var lines: [AVSpeechUtterance] = []
    override func speak(_ utterance: AVSpeechUtterance) { lines.append(utterance) }
    override func stopSpeaking(at boundary: AVSpeechBoundary) -> Bool { true }
}
