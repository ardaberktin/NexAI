import AVFoundation
import Foundation

struct LiveSeed: Sendable {
    let samples: [Float]
    let rate: Double
    var openingChimed = false

    func pcm() -> Data {
        let values = resample(to: 16_000).map { Int16(max(-1, min(1, $0)) * 32_767).littleEndian }
        return values.withUnsafeBytes { Data($0) }
    }

    func resample(to targetRate: Double) -> [Float] {
        guard rate > 0, samples.count > 1 else { return [] }
        if rate == targetRate { return samples }
        let count = Int(Double(samples.count) * targetRate / rate)
        return (0..<count).map { index in
            let position = Double(index) * rate / targetRate
            let left = min(Int(position), samples.count - 1)
            let right = min(left + 1, samples.count - 1)
            let fraction = Float(position - Double(left))
            return samples[left] * (1 - fraction) + samples[right] * fraction
        }
    }
}

final class LivePCMEncoder: @unchecked Sendable {
    private let source: AVAudioFormat
    private let target: AVAudioFormat
    private let converter: AVAudioConverter
    private let output: AsyncThrowingStream<Data, Error>.Continuation
    private let queue = DispatchQueue(label: "nex.live.resample", qos: .userInitiated)
    private let available = DispatchSemaphore(value: 8)
    private let gate = NSLock()
    private var stopped = false

    init(rate: Double, format: AVAudioCommonFormat = .pcmFormatFloat32,
         output: AsyncThrowingStream<Data, Error>.Continuation) throws {
        guard let source = AVAudioFormat(commonFormat: format, sampleRate: rate, channels: 1,
                                         interleaved: format == .pcmFormatInt16),
              let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1,
                                         interleaved: true),
              let converter = AVAudioConverter(from: source, to: target) else {
            throw NexError.capture("Live voice couldn’t convert microphone audio.")
        }
        self.source = source
        self.target = target
        self.converter = converter
        self.output = output
    }

    func copy(_ list: UnsafePointer<AudioBufferList>, frames: Int, silence: Bool = false) {
        let count = frames * Int(source.streamDescription.pointee.mBytesPerFrame)
        guard frames > 0, let data = list.pointee.mBuffers.mData,
              list.pointee.mBuffers.mDataByteSize >= count else { return }
        enqueue(data, count: count, frames: frames, silence: silence)
    }

    func copy(_ samples: UnsafeBufferPointer<Float>) {
        guard source.commonFormat == .pcmFormatFloat32, let address = samples.baseAddress,
              !samples.isEmpty else { return }
        enqueue(UnsafeRawPointer(address), count: samples.count * MemoryLayout<Float>.size,
                frames: samples.count, silence: false)
    }

    private func enqueue(_ data: UnsafeRawPointer, count: Int, frames: Int, silence: Bool) {
        guard isActive else { return }
        guard available.wait(timeout: .now()) == .success else {
            finish(NexError.capture("The microphone could not keep up with Live Conversation."))
            return
        }
        let samples = silence ? Data(count: count) : Data(bytes: data, count: count)
        queue.async {
            defer { self.available.signal() }
            guard self.isActive else { return }
            self.convert(samples, frames: frames)
        }
    }

    func stop() {
        gate.lock()
        stopped = true
        gate.unlock()
    }

    private var isActive: Bool {
        gate.lock()
        defer { gate.unlock() }
        return !stopped
    }

    private func finish(_ error: Error) {
        gate.lock()
        defer { gate.unlock() }
        guard !stopped else { return }
        stopped = true
        output.finish(throwing: error)
    }

    private func convert(_ samples: Data, frames: Int) {
        guard let input = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(frames)) else {
            return
        }
        input.frameLength = input.frameCapacity
        guard let address = input.mutableAudioBufferList.pointee.mBuffers.mData else { return }
        samples.copyBytes(to: UnsafeMutableRawBufferPointer(start: address, count: samples.count))
        let capacity = AVAudioFrameCount((Double(frames) * 16_000 / source.sampleRate).rounded(.up) + 64)
        guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }

        var supplied = false
        var error: NSError?
        let status = converter.convert(to: converted, error: &error) { _, state in
            state.pointee = supplied ? .noDataNow : .haveData
            if supplied { return nil }
            supplied = true
            return input
        }
        if status == .error {
            finish(error ?? NexError.capture("Live audio conversion stopped."))
            return
        }
        let rendered = converted.audioBufferList.pointee.mBuffers
        let count = Int(converted.frameLength) * MemoryLayout<Int16>.size
        guard let renderedData = rendered.mData, count > 0, rendered.mDataByteSize >= count else { return }
        gate.lock()
        defer { gate.unlock() }
        guard !stopped else { return }
        if case .dropped = output.yield(Data(bytes: renderedData, count: count)) {
            stopped = true
            output.finish(throwing: NexError.capture("The live connection could not keep up with the microphone."))
        }
    }
}
