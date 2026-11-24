import AVFoundation
import Darwin
import Foundation

@main
enum RecordingSegmentsCheck {
    private static let rate = 24_000.0
    private static let start = Date(timeIntervalSince1970: 1_800_000_000)
    private static let ambient = RecordingOptions(mode: .ambient, wake: false, language: "en", seconds: 1)

    static func main() throws {
        if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--leave-partial" {
            let spool = try Spool(directory: URL(fileURLWithPath: CommandLine.arguments[2]))
            let writer = RecordingSegments(spool: spool, sampleRate: rate, options: ambient, clipReady: {})
            _ = try tone(880, frames: 8_000).withUnsafeBufferPointer { try writer.write($0, started: start) }
            // End without sealing the segment or running Swift destructors.
            _exit(0)
        }

        try withSpool(checkRollover)
        try withSpool(checkVoiceLimit)
        try withSpool(checkRecovery)
        try withSpool(checkInputRates)
        print("AAC rollover, sample boundaries, voice limit, abrupt-exit recovery, and common input rates passed.")
    }

    private static func checkRollover(_ spool: Spool) throws {
        var ready = 0
        let writer = RecordingSegments(spool: spool, sampleRate: rate, options: ambient) { ready += 1 }
        _ = try tone(440, frames: 12_000).withUnsafeBufferPointer { try writer.write($0, started: start) }
        writer.configure(RecordingOptions(mode: .ambient, wake: true, language: "tr", seconds: 1))
        let crossesBoundary = tone(440, frames: 12_000, offset: 12_000) + tone(1_600, frames: 20_000)
        try crossesBoundary.withUnsafeBufferPointer {
            let done = try writer.write($0, started: start.addingTimeInterval(0.5))
            require(!done, "Ambient capture must remain active across rollover.")
        }
        try writer.finish()

        let clips = try spool.clips().sorted { $0.started < $1.started }
        require(ready == 2 && clips.count == 2, "There should be two sealed segments.")
        require(clips[0].duration == 1 && clips[1].duration == 20_000 / rate, "Segment frame counts changed.")
        require(clips[1].started == start.addingTimeInterval(1), "Segments did not share a sample boundary.")
        require(clips[0].language == "en" && clips[1].language == "tr" && clips[1].wake,
                "Updated settings must apply at the next segment boundary.")
        let low = try frequency(spool.audio(clips[0].id))
        let high = try frequency(spool.audio(clips[1].id))
        require(abs(low - 440) < 20 && abs(high - 1_600) < 30, "Samples crossed the wrong segment boundary.")
    }

    private static func checkVoiceLimit(_ spool: Spool) throws {
        let options = RecordingOptions(mode: .voice, wake: false, language: "en", seconds: 1)
        let writer = RecordingSegments(spool: spool, sampleRate: rate, options: options, clipReady: {})
        let complete = try tone(440, frames: 30_000).withUnsafeBufferPointer { try writer.write($0, started: start) }
        try writer.finish()
        let clips = try spool.clips()
        require(complete && clips.count == 1 && clips[0].duration == 1 && clips[0].mode == .voice,
                "A voice request must end once and obey its sample limit.")
    }

    private static func checkRecovery(_ spool: Spool) throws {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = ["--leave-partial", spool.directory.path]
        try child.run()
        child.waitUntilExit()
        require(child.terminationStatus == 0, "The fixture did not leave a partial recording.")

        let pending = try spool.clips()
        require(pending.count == 1 && pending[0].duration == nil, "The partial recording was not kept locally.")
        let saved = try SegmentAudio.recover(clip: pending[0], in: spool)
        require(saved, "The raw recording did not recover after process exit.")
        let result = try spool.clips()
        require(result[0].duration == 8_000 / rate, "The recovered recording lost frames.")
        let pitch = try frequency(spool.audio(result[0].id))
        require(abs(pitch - 880) < 25, "The recovered audio did not contain the original tone.")
        require(!spool.fm.fileExists(atPath: spool.pcm(result[0].id).path), "The temporary raw samples were not cleared.")
    }

    private static func checkInputRates(_ spool: Spool) throws {
        let rates = [8_000.0, 16_000, 44_100, 48_000, 96_000]
        for sampleRate in rates {
            let writer = RecordingSegments(spool: spool, sampleRate: sampleRate, options: ambient, clipReady: {})
            let samples = tone(440, frames: Int(sampleRate / 2), sampleRate: sampleRate)
            _ = try samples.withUnsafeBufferPointer { try writer.write($0, started: start) }
            try writer.finish()
        }
        let clips = try spool.clips().sorted { ($0.sampleRate ?? 0) < ($1.sampleRate ?? 0) }
        require(clips.count == rates.count, "A supported input rate failed to produce AAC audio.")
        for (sampleRate, clip) in zip(rates, clips) {
            let audio = try AVAudioFile(forReading: spool.audio(clip.id))
            require(clip.duration == 0.5 && clip.sampleRate == sampleRate &&
                    audio.fileFormat.sampleRate == AACSegmentEncoder.sampleRate, "The output sample rate was inconsistent.")
            let pitch = try frequency(spool.audio(clip.id))
            require(abs(pitch - 440) < 20, "Resampling changed the recorded tone.")
        }
    }

    private static func withSpool(_ check: (Spool) throws -> Void) throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: "nex-audio-" + nexID())
        let spool = try Spool(directory: directory)
        defer { try? FileManager.default.removeItem(at: directory) }
        try check(spool)
    }

    private static func tone(_ frequency: Double, frames: Int, offset: Int = 0, sampleRate: Double = rate) -> [Float] {
        (0..<frames).map { Float(sin(2 * Double.pi * frequency * Double($0 + offset) / sampleRate) * 0.5) }
    }

    private static func frequency(_ url: URL) throws -> Double {
        let file = try AVAudioFile(forReading: url)
        let frames: AVAudioFrameCount = 3_000
        guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: frames),
              let samples = buffer.floatChannelData?[0] else {
            throw NexError.capture("The fixture AAC file did not decode to float samples.")
        }
        file.framePosition = 3_000
        try file.read(into: buffer, frameCount: frames)
        let count = Int(buffer.frameLength)
        require(count == frames, "The fixture AAC file was truncated.")
        let rising = (1..<count).count { samples[$0 - 1] <= 0 && samples[$0] > 0 }
        return Double(rising) * file.processingFormat.sampleRate / Double(count)
    }

    private static func require(_ condition: Bool, _ message: String) {
        guard condition else {
            FileHandle.standardError.write(Data((message + "\n").utf8))
            exit(1)
        }
    }
}
