import AVFoundation
import Foundation

/// Each Watch message contains independently decodable Apple IMA4 packets. The sample count
/// excludes padding on the final 64-frame packet so server and archive positions stay aligned.
final class WatchRelayAudioCodec {
    private static let framesPerPacket = 64
    private static let bytesPerPacket = 34
    private let encoder: AVAudioConverter
    private let decoder: AVAudioConverter

    init?(encodingRate: Double, decodingRate: Double) {
        guard let encoder = Self.converter(rate: encodingRate, compress: true),
              let decoder = Self.converter(rate: decodingRate, compress: false) else { return nil }
        self.encoder = encoder
        self.decoder = decoder
    }

    func encode(_ pcm: Data) throws -> Data {
        let count = pcm.count / 2
        guard count > 0, pcm.count.isMultiple(of: 2), let length = UInt32(exactly: count) else { throw invalid() }
        var wire = withUnsafeBytes(of: length.littleEndian) { Data($0) }
        // During speaker playback the Watch sends exact silence to suppress echo.
        if pcm.allSatisfy({ $0 == 0 }) { return wire }
        let packets = packetCount(frames: count)
        let padded = packets * Self.framesPerPacket
        guard let input = AVAudioPCMBuffer(pcmFormat: encoder.inputFormat, frameCapacity: AVAudioFrameCount(padded)),
              let samples = input.int16ChannelData?[0] else { throw invalid() }
        input.frameLength = input.frameCapacity
        pcm.copyBytes(to: UnsafeMutableRawBufferPointer(start: samples, count: pcm.count))
        samples.advanced(by: count).initialize(repeating: 0, count: padded - count)
        let output = packetBuffer(format: encoder.outputFormat, packets: packets)
        try convert(encoder, input: input, output: output)
        guard output.packetCount == packets, output.byteLength == packets * Self.bytesPerPacket else { throw invalid() }
        wire.append(output.data.assumingMemoryBound(to: UInt8.self), count: Int(output.byteLength))
        return wire
    }

    func decode(_ wire: Data, maximumPCMBytes: Int) throws -> Data {
        let header = MemoryLayout<UInt32>.size
        guard wire.count >= header else { throw invalid() }
        let length = wire.withUnsafeBytes { UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self)) }
        guard let count = Int(exactly: length), count > 0, count <= maximumPCMBytes / 2 else { throw invalid() }
        if wire.count == header { return Data(count: count * 2) }
        let packets = packetCount(frames: count)
        let bytes = packets * Self.bytesPerPacket
        guard wire.count - header == bytes else { throw invalid() }
        let input = packetBuffer(format: decoder.inputFormat, packets: packets)
        input.packetCount = AVAudioPacketCount(packets)
        input.byteLength = UInt32(bytes)
        wire.dropFirst(header).copyBytes(to: UnsafeMutableRawBufferPointer(start: input.data, count: bytes))
        let frames = AVAudioFrameCount(packets * Self.framesPerPacket)
        guard let output = AVAudioPCMBuffer(pcmFormat: decoder.outputFormat, frameCapacity: frames) else { throw invalid() }
        try convert(decoder, input: input, output: output)
        guard output.frameLength == frames, let samples = output.int16ChannelData?[0] else { throw invalid() }
        return Data(bytes: samples, count: count * 2)
    }

    private static func converter(rate: Double, compress: Bool) -> AVAudioConverter? {
        var description = AudioStreamBasicDescription(mSampleRate: rate, mFormatID: kAudioFormatAppleIMA4,
            mFormatFlags: 0, mBytesPerPacket: UInt32(bytesPerPacket), mFramesPerPacket: UInt32(framesPerPacket),
            mBytesPerFrame: 0, mChannelsPerFrame: 1, mBitsPerChannel: 0, mReserved: 0)
        guard let packed = AVAudioFormat(streamDescription: &description),
              let pcm = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate, channels: 1,
                                      interleaved: true) else { return nil }
        return AVAudioConverter(from: compress ? pcm : packed, to: compress ? packed : pcm)
    }

    private func packetCount(frames: Int) -> Int { (frames + Self.framesPerPacket - 1) / Self.framesPerPacket }

    private func packetBuffer(format: AVAudioFormat, packets: Int) -> AVAudioCompressedBuffer {
        AVAudioCompressedBuffer(format: format, packetCapacity: AVAudioPacketCount(packets),
                                maximumPacketSize: Self.bytesPerPacket)
    }

    private func convert(_ converter: AVAudioConverter, input: AVAudioBuffer, output: AVAudioBuffer) throws {
        converter.reset()
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            state.pointee = supplied ? .endOfStream : .haveData
            if supplied { return nil }
            supplied = true
            return input
        }
        if status == .error { throw error ?? invalid() }
    }

    private func invalid() -> NexError { .capture("Nex couldn’t process the live relay audio.") }
}
