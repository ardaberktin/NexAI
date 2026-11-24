import Foundation

typealias AmbientSpeechHandler = @Sendable (AmbientUtterance, Date) -> Void

struct AmbientUtterance: Sendable {
    let speech: LiveSeed
    let context: LiveSeed
}

/// Collect short utterances without interrupting the journal or opening a live session.
struct AmbientSpeech {
    private var leading: [Float] = []
    private var utterance: [Float] = []
    private var roomAudio: [Float] = []
    private var recent = RecentAudio()
    private var voicedFrames = 0
    private var quietFrames = 0
    private var discard = false
    private var noiseRMS = 0.0015

    mutating func accept(_ samples: UnsafeBufferPointer<Float>, rate: Double) -> AmbientUtterance? {
        guard !samples.isEmpty, rate > 0 else { return nil }
        let contextFrames = Int(rate * 8)
        defer { recent.append(samples, limit: contextFrames) }
        let power = samples.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(samples.count)
        let rms = power.squareRoot()
        let onset = max(0.0045, noiseRMS * 2.8)
        let voiced = rms >= (utterance.isEmpty ? onset : onset * 0.75)

        if discard || (utterance.isEmpty && !voiced) {
            let weight = min(1, Double(samples.count) / (2 * rate))
            noiseRMS += (rms - noiseRMS) * weight
        }
        if utterance.isEmpty && !voiced && !discard {
            leading.append(contentsOf: samples)
            let extra = leading.count - Int(rate * 0.22)
            if extra > 0 { leading.removeFirst(extra) }
            return nil
        }
        quietFrames = voiced ? 0 : quietFrames + samples.count

        if discard {
            if quietFrames >= Int(rate * 0.7) { resetUtterance() }
            return nil
        }
        if utterance.isEmpty {
            roomAudio = recent.samples(limit: contextFrames, excluding: leading.count)
            utterance = leading
            leading.removeAll(keepingCapacity: true)
        }
        utterance.append(contentsOf: samples)
        if voiced { voicedFrames += samples.count }

        if utterance.count > Int(rate * 8.7) {
            resetUtterance()
            discard = true
            return nil
        }
        guard quietFrames >= Int(rate * 0.7) else { return nil }
        defer { resetUtterance() }
        guard voicedFrames >= Int(rate * 0.18) else { return nil }
        return AmbientUtterance(speech: LiveSeed(samples: utterance, rate: rate),
                                context: LiveSeed(samples: roomAudio, rate: rate))
    }

    mutating func reset() {
        resetUtterance()
        recent = RecentAudio()
        noiseRMS = 0.0015
    }

    private mutating func resetUtterance() {
        leading.removeAll(keepingCapacity: true)
        utterance.removeAll(keepingCapacity: true)
        roomAudio.removeAll(keepingCapacity: true)
        voicedFrames = 0
        quietFrames = 0
        discard = false
    }
}

/// Keep the eight seconds before a potential request without shifting sample data on each tap.
private struct RecentAudio {
    private var blocks: [[Float]] = []
    private var frames = 0

    mutating func append(_ samples: UnsafeBufferPointer<Float>, limit: Int) {
        let block = Array(samples.suffix(limit))
        blocks.append(block)
        frames += block.count
        while let first = blocks.first, frames - first.count >= limit {
            blocks.removeFirst()
            frames -= first.count
        }
    }

    func samples(limit: Int, excluding tail: Int) -> [Float] {
        let history = blocks.joined()
        let window = history.prefix(max(0, frames - tail))
        return Array(window.dropFirst(max(0, frames - limit)))
    }
}
