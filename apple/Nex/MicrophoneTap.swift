import AVFoundation

enum MicrophoneTap {
    static func install(on node: AVAudioNode, sampleRate: Double, legacyBufferSeconds: Double,
                        receive: @escaping @Sendable (UnsafePointer<AudioBufferList>, Int, AVAudioTime) -> Void) throws {
        if #available(macOS 27.0, iOS 27.0, watchOS 27.0, *) {
            // The throwing tap API accepts buffers between 100 and 400 ms.
            let minimumFrames = AVAudioFrameCount((sampleRate / 10).rounded(.up))
            try node.installAudioTap(onBus: 0, bufferSize: minimumFrames, format: nil) { buffer, time in
                buffer.withUnsafeAudioBufferList { receive($0, buffer.frameLength, time) }
            }
        } else {
            let frames = AVAudioFrameCount((sampleRate * legacyBufferSeconds).rounded(.up))
            node.installTap(onBus: 0, bufferSize: frames, format: nil) { buffer, time in
                receive(buffer.audioBufferList, Int(buffer.frameLength), time)
            }
        }
    }
}
