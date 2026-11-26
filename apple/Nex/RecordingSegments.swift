import AVFoundation
#if os(macOS)
import CoreAudio
#endif
import Foundation

enum DeviceAudioPlayback {
    static var isPlaying: Bool {
        #if os(iOS)
        return AVAudioSession.sharedInstance().isOtherAudioPlaying
        #elseif os(macOS)
        var deviceID = AudioObjectID(kAudioObjectSystemObject)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
        ) == noErr, deviceID != kAudioObjectUnknown else {
            return false
        }
        var running: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        var runningAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(
            deviceID, &runningAddress, 0, nil, &size, &running
        ) == noErr else {
            return false
        }
        return running != 0
        #else
        return false
        #endif
    }
}

struct RecordingOptions: Sendable {
    let mode: CaptureMode
    let wake: Bool
    let language: String
    let seconds: Double
    var openingChimed = false
}

/// A single writer queue owns this instance. Raw mono samples make the open segment recoverable.
final class RecordingSegments {
    private final class Segment {
        let clip: Clip
        let handle: FileHandle
        let targetFrames: Int
        var frames = 0

        init(clip: Clip, handle: FileHandle, targetFrames: Int) {
            self.clip = clip
            self.handle = handle
            self.targetFrames = targetFrames
        }
    }

    let spool: Spool
    let sampleRate: Double
    private(set) var options: RecordingOptions
    let clipReady: () -> Void
    private var start: Date?
    private var current: Segment?
    private var framesWritten = 0
    private var lastSpaceCheck = ProcessInfo.processInfo.systemUptime

    init(spool: Spool, sampleRate: Double, options: RecordingOptions, clipReady: @escaping () -> Void) {
        self.spool = spool
        self.sampleRate = sampleRate
        self.options = options
        self.clipReady = clipReady
    }

    func configure(_ next: RecordingOptions) {
        guard options.mode == .ambient, next.mode == .ambient else { return }
        options = next
    }

    func resetTimeline() {
        start = nil
        framesWritten = 0
    }

    /// Returns true after a complete voice question. Ambient capture keeps rotating.
    func write(_ samples: UnsafeBufferPointer<Float>, started: Date) throws -> Bool {
        guard let address = samples.baseAddress, samples.count > 0 else { return false }
        if start == nil { start = started }
        var offset = 0
        while offset < samples.count {
            if current == nil { try openSegment(started: start ?? started) }
            guard let segment = current else {
                throw NexError.capture("The recording segment could not be opened.")
            }
            let count = min(samples.count - offset, segment.targetFrames - segment.frames)
            let data = Data(bytes: address.advanced(by: offset), count: count * MemoryLayout<Float>.size)
            try segment.handle.write(contentsOf: data)
            segment.frames += count
            framesWritten += count
            offset += count

            if segment.frames == segment.targetFrames {
                try finish()
                if segment.clip.mode == .voice { return true }
            }
        }
        return false
    }

    func finish(suppressWake: Bool = false) throws {
        guard let segment = current else { return }
        current = nil
        try segment.handle.synchronize()
        try segment.handle.close()
        var clip = segment.clip
        if suppressWake { clip.wake = false }
        if clip.mode == .ambient && clip.mediaPlaying != true && DeviceAudioPlayback.isPlaying {
            clip.mediaPlaying = true
        }
        if try SegmentAudio.recover(clip: clip, in: spool) {
            clipReady()
        } else {
            try spool.remove(segment.clip)
        }
    }

    private func openSegment(started: Date) throws {
        let uptime = ProcessInfo.processInfo.systemUptime
        if uptime - lastSpaceCheck > 60 {
            try spool.verifySpace()
            lastSpaceCheck = uptime
        }
        let timestamp = started.addingTimeInterval(Double(framesWritten) / sampleRate)
        let clip = Clip(id: nexID(), started: timestamp, mode: options.mode, wake: options.wake,
                        language: options.language, sampleRate: sampleRate, timeZone: NexDate.timeZone,
                        openingChimed: options.openingChimed ? true : nil,
                        mediaPlaying: options.mode == .ambient && DeviceAudioPlayback.isPlaying ? true : nil)
        try spool.save(clip)
        let raw = spool.pcm(clip.id)
        let created = spool.fm.createFile(atPath: raw.path, contents: nil, attributes: [.posixPermissions: 0o600])
        guard created else { throw NexError.capture("The local queue could not write a recording segment.") }
        current = Segment(clip: clip, handle: try FileHandle(forWritingTo: raw),
                          targetFrames: max(1, Int(sampleRate * options.seconds)))
    }
}

enum SegmentAudio {
    /// Handles raw files from the continuous recorder and uncommitted AAC files from older versions.
    static func recover(clip: Clip, in spool: Spool) throws -> Bool {
        let raw = spool.pcm(clip.id)
        var ready = clip
        if let rate = clip.sampleRate, (8_000...192_000).contains(rate),
           spool.fm.fileExists(atPath: raw.path) {
            let byteCount = try raw.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            let frames = min(byteCount / MemoryLayout<Float>.size, Int(rate * 65))
            guard frames > 0 else { return false }
            let staging = spool.encoding(clip.id)
            if spool.fm.fileExists(atPath: staging.path) { try spool.fm.removeItem(at: staging) }
            try AACSegmentEncoder.encode(raw: raw, target: staging, rate: rate, frames: frames)
            let audio = spool.audio(clip.id)
            if spool.fm.fileExists(atPath: audio.path) { try spool.fm.removeItem(at: audio) }
            try spool.fm.moveItem(at: staging, to: audio)
            let file = try FileHandle(forWritingTo: audio)
            defer { try? file.close() }
            try file.synchronize()
            ready.duration = Double(frames) / rate
            try spool.fm.removeItem(at: raw)
        } else if let duration = spool.playableDuration(clip.id) {
            ready.duration = duration
        } else {
            return false
        }
        try spool.save(ready)
        return true
    }
}
