import Foundation

@main
enum AmbientSpeechCheck {
    private static let rate = 16_000.0
    private static let blockFrames = 1_600

    static func main() {
        checkBoundedContext()
        checkMediaBeforeRequest()
        checkQuietRequest()
        checkSteadyBackground()
        print("Ambient speech passed room context, reset, quiet speech, and noise checks.")
    }

    private static func checkBoundedContext() {
        var endpoint = AmbientSpeech()
        precondition(feed(0.003, blocks: 50, to: &endpoint).isEmpty)
        precondition(feed(0.002, blocks: 80, to: &endpoint).isEmpty)
        let sample = question(to: &endpoint)
        precondition(!sample.context.samples.isEmpty)
        precondition(sample.context.samples.count <= Int(rate * 8))
        precondition(sample.context.samples.allSatisfy { $0 == 0.002 })
        precondition(sample.speech.samples.contains(0.07))

        endpoint.reset()
        precondition(question(to: &endpoint).context.samples.isEmpty)
    }

    private static func checkMediaBeforeRequest() {
        var endpoint = AmbientSpeech()
        precondition(feed(0.04, blocks: 100, to: &endpoint).isEmpty)
        precondition(feed(0, blocks: 8, to: &endpoint).isEmpty)
        let sample = question(to: &endpoint)
        precondition(sample.context.samples.contains(0.04))
        precondition(!sample.context.samples.contains(0.07))
        precondition(!sample.speech.samples.contains(0.04))
    }

    private static func question(to endpoint: inout AmbientSpeech) -> AmbientUtterance {
        precondition(feed(0.07, blocks: 5, to: &endpoint).isEmpty)
        let results = feed(0, blocks: 8, to: &endpoint)
        precondition(results.count == 1)
        return results[0]
    }

    private static func checkQuietRequest() {
        var endpoint = AmbientSpeech()
        precondition(feed(0.0015, blocks: 40, to: &endpoint).isEmpty)
        precondition(feed(0.006, blocks: 3, to: &endpoint).isEmpty)
        precondition(feed(0.0015, blocks: 8, to: &endpoint).count == 1)
        precondition(feed(0.006, blocks: 1, to: &endpoint).isEmpty)
        precondition(feed(0.0015, blocks: 8, to: &endpoint).isEmpty)
    }

    private static func checkSteadyBackground() {
        var endpoint = AmbientSpeech()
        precondition(feed(0.008, blocks: 180, to: &endpoint).isEmpty)
        precondition(feed(0.028, blocks: 4, to: &endpoint).isEmpty)
        precondition(feed(0.008, blocks: 8, to: &endpoint).count == 1)
    }

    private static func feed(_ value: Float, blocks: Int, to endpoint: inout AmbientSpeech) -> [AmbientUtterance] {
        let samples = [Float](repeating: value, count: blockFrames)
        return samples.withUnsafeBufferPointer { buffer in
            (0..<blocks).compactMap { _ in endpoint.accept(buffer, rate: rate) }
        }
    }
}
