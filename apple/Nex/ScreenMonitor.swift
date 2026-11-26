#if os(macOS)
import Foundation

@MainActor
final class ScreenMonitor {
    private let vision: ScreenVision
    private let observed: (Date) -> Void
    var activity: ((String?, Date) -> Void)?
    private var timeline = ScreenActivity()
    private var sampler: Task<Void, Never>?
    private var caption: Task<Void, Never>?
    private var lastRevision: UInt64?
    private var lastCaption = Date.distantPast
    private var answering = false
    private var latestDescription: String?
    private var lastReport = Date.distantPast

    init(vision: ScreenVision, observed: @escaping (Date) -> Void) {
        self.vision = vision
        self.observed = observed
    }

    func start(_ frames: ScreenFrames) {
        pause()
        sampler = Task(priority: .utility) { [weak self] in
            while !Task.isCancelled, let self {
                await self.inspect(frames)
                do { try await Task.sleep(for: .seconds(10)) }
                catch { break }
            }
        }
    }

    func pause() {
        sampler?.cancel()
        sampler = nil
        cancelCaption()
        lastRevision = nil
        latestDescription = nil
        lastReport = .distantPast
    }

    func reset() {
        pause()
        timeline = ScreenActivity()
        lastCaption = .distantPast
        answering = false
    }

    func setAnswering(_ active: Bool) {
        answering = active
        if active { cancelCaption() }
    }

    func evidence(for question: String) -> [ScreenObservation] {
        timeline.prune(asOf: .now)
        return timeline.evidence(for: question)
    }

    private func inspect(_ frames: ScreenFrames) async {
        timeline.prune(asOf: .now)
        let now = Date.now
        let needsCaption = latestDescription == nil && caption == nil && !answering
            && now.timeIntervalSince(lastCaption) >= 35
        guard let frame = await frames.snapshot(after: needsCaption ? nil : lastRevision) else {
            if let latestDescription, now.timeIntervalSince(lastReport) >= 45,
               await frames.hasVisibleFrame(revision: lastRevision), !Task.isCancelled {
                report(latestDescription, at: .now)
            }
            return
        }
        guard !Task.isCancelled else { return }
        lastRevision = frame.revision
        guard let reading = await ScreenReader.read(frame.jpeg), !Task.isCancelled else { return }
        let changed = timeline.record(reading, at: frame.time) != nil
        if changed {
            observed(frame.time)
        } else if let latestDescription {
            if frame.time.timeIntervalSince(lastReport) >= 45 {
                report(latestDescription, at: frame.time)
            }
            return
        }

        if let latestDescription, frame.time.timeIntervalSince(lastReport) >= 45 {
            report(latestDescription, at: frame.time)
        }
        guard let id = timeline.entries.last?.id, !answering, caption == nil,
              Date.now.timeIntervalSince(lastCaption) >= 35 else { return }
        lastCaption = .now
        caption = Task(priority: .utility) { [weak self] in
            guard let self else { return }
            defer { if !Task.isCancelled { self.caption = nil } }
            guard let description = try? await self.vision.describeActivity(frame: frame.jpeg),
                  !Task.isCancelled else { return }
            let summary = String(description.prefix(360))
            self.timeline.annotate(id, with: summary)
            self.report(summary, at: frame.time)
        }
    }

    private func report(_ summary: String, at time: Date) {
        latestDescription = summary
        lastReport = time
        activity?(summary, time)
    }

    private func cancelCaption() {
        caption?.cancel()
        caption = nil
    }
}
#endif
