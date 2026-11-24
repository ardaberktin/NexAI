#if os(iOS)
import AudioToolbox
import AVFoundation
import Foundation
import OSLog

@MainActor
final class LiveAudio {
    private var voice: PhoneVoiceIO?
    var usesEchoCancellation: Bool { voice?.usesEchoCancellation == true }
    var isAudible: Bool { voice?.isAudible == true }
    private let session = AVAudioSession.sharedInstance()
    private let log = Logger(subsystem: "com.ardaberktin.nex", category: "LiveAudio")

    var routeNotification: Notification.Name { AVAudioSession.routeChangeNotification }
    var routeObject: AnyObject { session }

    func needsRecovery(reason: UInt?) -> Bool {
        guard let reason, let change = AVAudioSession.RouteChangeReason(rawValue: reason) else { return false }
        switch change {
        case .newDeviceAvailable, .oldDeviceUnavailable, .noSuitableRouteForCategory:
            return true
        case .routeConfigurationChange:
            return voice?.rate != session.sampleRate
        default:
            return false
        }
    }

    func start(output: AsyncThrowingStream<Data, Error>.Continuation, mixPlayback: Bool = false) throws {
        var options: AVAudioSession.CategoryOptions = [.allowBluetoothHFP, .defaultToSpeaker]
        if mixPlayback { options.insert(.mixWithOthers) }
        if session.category != .playAndRecord || session.mode != .voiceChat || session.categoryOptions != options {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
        }
        try session.setActive(true)
        do {
            let next = try PhoneVoiceIO(rate: session.sampleRate, output: output, subtype: kAudioUnitSubType_VoiceProcessingIO)
            try next.start()
            voice = next
        } catch {
            // Some routes decline Voice Processing I/O. Remote I/O still provides live streaming.
            log.error("Voice processing I/O unavailable for this route")
            let next = try PhoneVoiceIO(rate: session.sampleRate, output: output, subtype: kAudioUnitSubType_RemoteIO)
            try next.start()
            voice = next
        }
    }

    func play(_ data: Data) { voice?.play(data) }
    func interrupt() { voice?.interrupt() }

    func stop() {
        voice?.stop()
        voice = nil
    }
}

private func captureCallback(
    _ context: UnsafeMutableRawPointer, _ flags: UnsafeMutablePointer<AudioUnitRenderActionFlags>,
    _ time: UnsafePointer<AudioTimeStamp>, _ bus: UInt32, _ count: UInt32,
    _ output: UnsafeMutablePointer<AudioBufferList>?
) -> OSStatus {
    Unmanaged<PhoneVoiceIO>.fromOpaque(context).takeUnretainedValue().capture(flags, time, count)
}

private func playCallback(
    _ context: UnsafeMutableRawPointer, _ flags: UnsafeMutablePointer<AudioUnitRenderActionFlags>,
    _ time: UnsafePointer<AudioTimeStamp>, _ bus: UInt32, _ count: UInt32,
    _ output: UnsafeMutablePointer<AudioBufferList>?
) -> OSStatus {
    if let output {
        Unmanaged<PhoneVoiceIO>.fromOpaque(context).takeUnretainedValue().render(output, count: count)
    }
    return noErr
}

private final class PhoneVoiceIO {
    private static let microphoneCapacity = 8_192
    let rate: Double
    let usesEchoCancellation: Bool
    var isAudible: Bool { playback.isAudible }
    private let microphone: UnsafeMutablePointer<Int16>
    private let encoder: LivePCMEncoder
    private let playback: PhonePlayback
    private var unit: AudioUnit?

    init(rate: Double, output: AsyncThrowingStream<Data, Error>.Continuation, subtype: OSType) throws {
        guard (8_000...96_000).contains(rate) else {
            throw NexError.capture("Live Conversation couldn’t open this audio route.")
        }
        self.rate = rate
        usesEchoCancellation = subtype == kAudioUnitSubType_VoiceProcessingIO
        encoder = try LivePCMEncoder(rate: rate, format: .pcmFormatInt16, output: output)
        playback = PhonePlayback(rate: rate)
        microphone = .allocate(capacity: Self.microphoneCapacity)
        try prepare(subtype: subtype)
    }

    private func prepare(subtype: OSType) throws {
        var description = AudioComponentDescription(
            componentType: kAudioUnitType_Output, componentSubType: subtype,
            componentManufacturer: kAudioUnitManufacturer_Apple, componentFlags: 0, componentFlagsMask: 0
        )
        guard let component = AudioComponentFindNext(nil, &description) else {
            throw NexError.capture("Live Conversation couldn’t find the iPhone audio unit.")
        }
        var instance: AudioUnit?
        try check(AudioComponentInstanceNew(component, &instance))
        guard let instance else { throw NexError.capture("Live Conversation couldn’t create the iPhone audio unit.") }
        unit = instance
        do {
            try configure(instance)
        } catch {
            stop()
            throw error
        }
    }

    private func configure(_ instance: AudioUnit) throws {
        var enabled: UInt32 = 1
        try set(instance, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &enabled)
        try set(instance, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &enabled)
        var format = AudioStreamBasicDescription(
            mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2, mChannelsPerFrame: 1,
            mBitsPerChannel: 16, mReserved: 0
        )
        try set(instance, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &format)
        try set(instance, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &format)

        let context = Unmanaged.passUnretained(self).toOpaque()
        var record = AURenderCallbackStruct(inputProc: captureCallback, inputProcRefCon: context)
        var render = AURenderCallbackStruct(inputProc: playCallback, inputProcRefCon: context)
        try set(instance, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 1, &record)
        try set(instance, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &render)
        try check(AudioUnitInitialize(instance))
    }

    private func set<T>(_ instance: AudioUnit, _ property: AudioUnitPropertyID,
                        _ scope: AudioUnitScope, _ bus: AudioUnitElement, _ value: inout T) throws {
        try withUnsafePointer(to: &value) { data in
            try check(AudioUnitSetProperty(instance, property, scope, bus, data, UInt32(MemoryLayout<T>.size)))
        }
    }

    private func check(_ code: OSStatus) throws {
        if code != noErr { throw NSError(domain: NSOSStatusErrorDomain, code: Int(code)) }
    }

    func start() throws {
        if let unit { try check(AudioOutputUnitStart(unit)) }
    }

    func stop() {
        if let unit {
            AudioOutputUnitStop(unit)
            AudioUnitUninitialize(unit)
            AudioComponentInstanceDispose(unit)
            self.unit = nil
        }
        encoder.stop()
        playback.clear()
    }

    deinit {
        stop()
        microphone.deallocate()
    }

    func capture(_ flags: UnsafeMutablePointer<AudioUnitRenderActionFlags>,
                 _ time: UnsafePointer<AudioTimeStamp>, _ count: UInt32) -> OSStatus {
        guard let unit, count > 0, count <= Self.microphoneCapacity else { return noErr }
        var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
            mNumberChannels: 1, mDataByteSize: count * 2, mData: microphone
        ))
        let status = AudioUnitRender(unit, flags, time, 1, count, &list)
        if status == noErr {
            if !usesEchoCancellation && playback.isAudible {
                microphone.update(repeating: 0, count: Int(count))
            }
            encoder.copy(&list, frames: Int(count))
        }
        return status
    }

    func play(_ data: Data) { playback.append(data) }
    func interrupt() { playback.clear() }

    func render(_ output: UnsafeMutablePointer<AudioBufferList>, count: UInt32) {
        guard let data = output.pointee.mBuffers.mData else { return }
        let frames = min(Int(count), Int(output.pointee.mBuffers.mDataByteSize) / 2)
        playback.read(into: data.assumingMemoryBound(to: Int16.self), frames: frames)
    }
}

/// Fixed-capacity storage keeps speaker rendering independent of network and memory allocations.
private final class PhonePlayback {
    private let rate: Double
    private let capacity: Int
    private let samples: UnsafeMutablePointer<Int16>
    private let guardLock = NSLock()
    private var head = 0
    private var size = 0
    private var tail = 0

    init(rate: Double) {
        self.rate = rate
        capacity = Int(rate * 45)
        samples = .allocate(capacity: capacity)
    }

    deinit { samples.deallocate() }

    var isAudible: Bool {
        guard guardLock.try() else { return true }
        defer { guardLock.unlock() }
        return size > 0 || tail > 0
    }

    func append(_ data: Data) {
        guard data.count >= 2, data.count.isMultiple(of: 2) else { return }
        let sourceFrames = data.count / 2
        let ratio = 24_000 / rate
        let length = Int((Double(sourceFrames) / ratio).rounded())
        let output = data.withUnsafeBytes { bytes -> [Int16] in
            func sample(_ i: Int) -> Double {
                Double(Int16(littleEndian: bytes.loadUnaligned(fromByteOffset: i * 2, as: Int16.self)))
            }
            return (0..<length).map { i in
                let position = Double(i) * ratio
                let start = min(Int(position), sourceFrames - 1)
                let end = min(start + 1, sourceFrames - 1)
                return Int16(sample(start) + (sample(end) - sample(start)) * (position - Double(start)))
            }
        }
        guardLock.lock()
        defer { guardLock.unlock() }
        output.withUnsafeBufferPointer { values in
            guard let base = values.baseAddress else { return }
            let n = min(capacity - size, values.count)
            let destination = (head + size) % capacity
            let first = min(n, capacity - destination)
            samples.advanced(by: destination).update(from: base, count: first)
            samples.update(from: base.advanced(by: first), count: n - first)
            size += n
        }
    }

    func read(into target: UnsafeMutablePointer<Int16>, frames: Int) {
        target.update(repeating: 0, count: frames)
        guard guardLock.try() else { return }
        defer { guardLock.unlock() }
        let n = min(size, frames)
        if n > 0 {
            let first = min(n, capacity - head)
            target.update(from: samples.advanced(by: head), count: first)
            target.advanced(by: first).update(from: samples, count: n - first)
            head = (head + n) % capacity
            size -= n
            tail = Int(rate * 0.20)
        } else {
            tail = max(0, tail - frames)
        }
    }

    func clear() {
        guardLock.lock()
        defer { guardLock.unlock() }
        head = 0
        size = 0
        tail = 0
    }
}
#endif
