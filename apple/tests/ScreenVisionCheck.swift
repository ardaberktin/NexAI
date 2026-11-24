import AppKit
import Foundation

@main
enum ScreenVisionCheck {
    @MainActor
    static func main() async throws {
        let model = ScreenVision()
        defer { model.stop() }
        try await model.warm()
        let earlierFrame = fixture(build: "5482", circle: .systemOrange)
        let currentFrame = fixture(build: "6193", circle: .systemPurple)
        guard let earlier = await ScreenReader.read(earlierFrame), let current = await ScreenReader.read(currentFrame) else {
            preconditionFailure("Generated frames could not be read.")
        }
        precondition(earlier.text.contains("5482") && current.text.contains("6193"), "Screen OCR missed the build numbers.")
        var activity = ScreenActivity()
        let start = Date.now.addingTimeInterval(-3600)
        let firstID = activity.record(earlier, at: start)!
        precondition(activity.record(earlier, at: start.addingTimeInterval(10)) == nil, "Unchanged screens should be skipped.")
        let caption = try await model.describeActivity(frame: earlierFrame)
        precondition(caption.contains("5482") || caption.localizedCaseInsensitiveContains("orange"), caption)
        activity.annotate(firstID, with: caption)

        for index in 1...14 {
            let time = start.addingTimeInterval(Double(index * 45))
            let reading = ScreenReading(text: "Unrelated document number \(index)",
                                        colors: Array(repeating: index.isMultiple(of: 2) ? 245 : 60, count: 576))
            _ = activity.record(reading, at: time)
        }
        let currentID = activity.record(current, at: .now)!
        let question = "Which build number did I see earlier in the orange view?"
        let observations = activity.evidence(for: question)
        precondition(observations.contains { $0.id == firstID } && observations.count <= 9,
                     "Older relevant observations should be found within a bounded context.")
        let recalled = try await model.answer(question, frame: currentFrame, history: [],
                                              observations: observations, language: "en", spoken: true)
        precondition(recalled.contains("5482") && !recalled.contains("6193"), recalled)

        let now = try await model.answer("Which build number is on screen right now, and what color is the circle?",
                                         frame: currentFrame, history: [], observations: observations,
                                         language: "en", spoken: true)
        precondition(now.contains("6193") && now.localizedCaseInsensitiveContains("purple"), now)

        activity.prune(asOf: start.addingTimeInterval(24 * 3600 + 1))
        precondition(!activity.entries.contains { $0.id == firstID } && activity.entries.contains { $0.id == currentID })
        print("Screen OCR, change detection, bounded recall, the current frame, and retention passed.")

    }

    @MainActor
    private static func fixture(build: String, circle: NSColor) -> Data {
        let image = NSImage(size: NSSize(width: 960, height: 560), flipped: false) { bounds in
            NSColor.white.setFill()
            bounds.fill()
            NSColor.systemBlue.setFill()
            NSRect(x: 90, y: 90, width: 170, height: 130).fill()
            circle.setFill()
            NSBezierPath(ovalIn: NSRect(x: 600, y: 90, width: 130, height: 130)).fill()
            ("Build \(build) — deployment queued" as NSString).draw(at: NSPoint(x: 90, y: 370),
                withAttributes: [.font: NSFont.systemFont(ofSize: 36), .foregroundColor: NSColor.black])
            return true
        }
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        return bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.84])!
    }
}
