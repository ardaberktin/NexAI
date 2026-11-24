import AVFoundation
import Foundation

enum AACSegmentEncoder {
    static let sampleRate = 24_000.0
    private static let bufferSize: AVAudioFrameCount = 4096

    static func encode(raw: URL, target: URL, rate: Double, frames: Int) throws {
        let input = try Samples(file: raw, frames: frames)
        let output = try AVAudioFile(forWriting: target, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 32_000
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
        guard let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: bufferSize) else {
            throw NexError.capture("The AAC encoder could not allocate a sample buffer.")
        }
        if sampleRate == rate {
            while try input.read(into: buffer, maxFrames: bufferSize) { try output.write(from: buffer) }
        } else {
            try resample(input, buffer: buffer, to: output)
        }
        if #available(macOS 15.0, iOS 18.0, watchOS 11.0, *) { output.close() }
    }

    private static func resample(_ input: Samples, buffer: AVAudioPCMBuffer, to file: AVAudioFile) throws {
        guard let converter = AVAudioConverter(from: buffer.format, to: file.processingFormat),
              let output = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: bufferSize) else {
            throw NexError.capture("The microphone sample rate could not be converted to AAC.")
        }
        while true {
            var conversionError: NSError?
            var readError: (any Error)?
            output.frameLength = 0
            let result = converter.convert(to: output, error: &conversionError) { requested, status in
                do {
                    if try input.read(into: buffer, maxFrames: requested) {
                        status.pointee = .haveData
                        return buffer
                    }
                } catch { readError = error }
                status.pointee = .endOfStream
                return nil
            }
            if let readError { throw readError }
            if let conversionError { throw conversionError }
            if output.frameLength > 0 { try file.write(from: output) }
            if result == .endOfStream { return }
            guard result == .haveData, output.frameLength > 0 else {
                throw NexError.capture("Audio resampling stopped before the end of the segment.")
            }
        }
    }

    private final class Samples {
        private let input: FileHandle
        private var remaining: Int

        init(file: URL, frames: Int) throws {
            input = try FileHandle(forReadingFrom: file)
            remaining = frames
        }

        deinit { try? input.close() }

        func read(into buffer: AVAudioPCMBuffer, maxFrames: AVAudioFrameCount) throws -> Bool {
            guard remaining > 0 else { return false }
            let maxAvailable = min(buffer.frameCapacity, max(1, maxFrames))
            let count = min(remaining, Int(maxAvailable))
            let bytes = count * MemoryLayout<Float>.size
            guard let data = try input.read(upToCount: bytes), data.count == bytes,
                  let samples = buffer.floatChannelData?[0] else {
                throw NexError.capture("The local audio segment ended while encoding.")
            }
            data.copyBytes(to: UnsafeMutableRawBufferPointer(start: samples, count: data.count))
            buffer.frameLength = AVAudioFrameCount(count)
            remaining -= count
            return true
        }
    }
}
