import Foundation

/// Endpoints a 16 kHz mono Int16 turn locally, irrespective of phone availability.
struct WatchQuestionDetector {
    enum Boundary: Equatable { case began(Int), ended(Int) }

    private(set) var position = 0
    private var candidateFrames = 0
    private var quietFrames = 0
    private var start: Int?
    private var floor = 0.0015
    private var listenFrom = 0
    var safeThrough: Int { max(0, position - 12_800) }

    mutating func accept(_ pcm: Data, listening: Bool) -> Boundary? {
        let frames = pcm.count / 2
        guard frames > 0 else { return nil }
        let first = position
        position += frames
        guard listening else {
            listenFrom = position
            candidateFrames = 0
            return nil
        }
        let energy = pcm.withUnsafeBytes { bytes in
            bytes.bindMemory(to: Int16.self).reduce(0.0) { sum, sample in
                let value = Double(Int16(littleEndian: sample)) / 32_768
                return sum + value * value
            }
        }
        let rms = (energy / Double(frames)).squareRoot()
        let threshold = max(0.005, floor * 2.7)
        let voiced = rms >= (start == nil ? threshold : threshold * 0.65)
        if let start {
            quietFrames = voiced ? 0 : quietFrames + frames
            if quietFrames >= 16_000 || position - start >= 16_000 * 60 { return finish() }
        } else if voiced {
            candidateFrames += frames
            if candidateFrames >= 2_880 {
                let sample = max(listenFrom, first + frames - candidateFrames - 4_800)
                start = sample
                return .began(sample)
            }
        } else {
            candidateFrames = 0
            floor += (rms - floor) * min(1, Double(frames) / 32_000)
        }
        return nil
    }

    mutating func finish() -> Boundary? {
        guard start != nil else { return nil }
        start = nil
        quietFrames = 0
        candidateFrames = 0
        listenFrom = position
        return .ended(position)
    }
}
