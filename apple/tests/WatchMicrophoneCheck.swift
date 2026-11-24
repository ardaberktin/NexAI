import AVFoundation
import Foundation
import os

@main
enum WatchMicrophoneCheck {
    @MainActor
    static func main() async throws {
        try await checkHandoff(rate: 16_000)
        try await checkHandoff(rate: 48_000)
        await checkSpokenAnswer()
        print("Watch microphone handoff and ambient recording resumption passed.")
    }

    @MainActor private static func checkSpokenAnswer() async {
        let speech = SpeechProbe()
        let playback = WatchSpokenReply(speech: speech)
        playback.speak("Saved answer", language: "en", sharingMicrophone: true) { throw URLError(.notConnectedToInternet) }
        while playback.preparing { await Task.yield() }
        precondition(speech.lines.map(\.speechString) == ["Saved answer"] && playback.audible)
        let first = speech.lines[0]
        playback.stop()
        precondition(speech.stops == 1 && !playback.audible)

        playback.speak("Recovered answer", language: "en", sharingMicrophone: false) { throw URLError(.notConnectedToInternet) }
        while playback.preparing { await Task.yield() }
        playback.speechSynthesizer(speech, didCancel: first)
        await Task.yield()
        precondition(speech.lines.count == 2 && playback.audible)
        playback.speechSynthesizer(speech, didFinish: speech.lines[1])
        await Task.yield()
        precondition(!playback.audible)
        print("Spoken answers, interruption, and late speech callbacks passed.")
    }

    private static func checkHandoff(rate: Double) async throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: "nex-watch-mic-" + nexID())
        defer { try? FileManager.default.removeItem(at: directory) }
        let spool = try Spool(directory: directory)
        let events = AsyncStream<WatchMicWriter.Event>.makeStream()
        let live = AsyncThrowingStream<Data, Error>.makeStream()
        let timeout = Task {
            do { try await Task.sleep(for: .seconds(5)) }
            catch { return }
            live.continuation.finish(throwing: NexError.capture("The Watch test did not receive microphone PCM."))
            events.continuation.finish()
        }
        defer { timeout.cancel() }

        let options = RecordingOptions(mode: .ambient, wake: false, language: "en", seconds: 45)
        let writer = WatchMicWriter(spool: spool, rate: rate, options: options) { event in
            events.continuation.yield(event)
            if case .finished = event { events.continuation.finish() }
        }
        let progress = OSAllocatedUnfairLock(initialState: [ContinuousClock.Instant]())
        let input = WatchMicrophoneInput(writer: writer, rate: rate) { receivedAt in
            progress.withLock { $0.append(receivedAt) }
        }
        let frames = Int(rate * 0.20)
        precondition(input.lastAudioAt == nil)
        feed(input, frames: frames)
        guard let firstFrame = input.lastAudioAt else { preconditionFailure("The Watch missed an audio frame.") }
        feed(input, frames: 0)
        precondition(input.lastAudioAt == firstFrame)
        try input.openLive(live.continuation)
        feed(input, frames: frames)
        precondition(input.lastAudioAt.map { $0 > firstFrame } == true)
        precondition(progress.withLock { $0 } == [firstFrame])

        var microphone = live.stream.makeAsyncIterator()
        guard let pcm = try await microphone.next() else {
            throw NexError.capture("The Watch test got an empty microphone stream.")
        }
        let samples = pcm.withUnsafeBytes { payload in
            stride(from: 0, to: payload.count, by: 2).map {
                Int16(littleEndian: payload.loadUnaligned(fromByteOffset: $0, as: Int16.self))
            }
        }
        precondition((3_000...3_200).contains(samples.count) && samples.contains { $0 > 1_000 })
        precondition(input.closeLive())
        feed(input, frames: frames)
        writer.stop()

        var finished = false
        for await event in events.stream {
            if case .finished(let error) = event {
                precondition(error == nil, error ?? "")
                finished = true
            }
        }
        precondition(finished)
        let clips = try spool.clips()
        precondition(clips.count == 2 && clips.allSatisfy { $0.mode == .ambient && $0.duration == 0.20 })
    }

    private static func feed(_ input: WatchMicrophoneInput, frames: Int) {
        var samples = [Float](repeating: 0.25, count: frames)
        samples.withUnsafeMutableBufferPointer { buffer in
            // The tap's frame count is authoritative for the same Float32 channel used by the recorder.
            var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
                mNumberChannels: 1, mDataByteSize: 0, mData: buffer.baseAddress
            ))
            input.receive(&list, frames: frames, time: AVAudioTime(hostTime: mach_absolute_time()))
        }
    }
}

private final class SpeechProbe: AVSpeechSynthesizer {
    var lines: [AVSpeechUtterance] = []
    var stops = 0

    override func speak(_ utterance: AVSpeechUtterance) { lines.append(utterance) }
    override func stopSpeaking(at boundary: AVSpeechBoundary) -> Bool {
        stops += 1
        return true
    }
}
