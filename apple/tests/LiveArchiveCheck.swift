import Foundation

@main
enum LiveArchiveCheck {
    @MainActor
    static func main() async throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: "nex-live-test-" + nexID())
        defer { try? FileManager.default.removeItem(at: directory) }
        let spool = try Spool(directory: directory)
        let events = AsyncStream<String?>.makeStream()
        let rate = 16_000
        let seed = Data(repeating: 0, count: rate * 2)
        let archive = LiveArchive(spool: spool, language: "en", seed: seed, ambient: true,
                                  completed: { events.continuation.yield($0) })

        try archive.append(Data(repeating: 0, count: 6 * rate))
        archive.progress(to: 2 * rate)
        let id = nexID()
        archive.start(id: id, sample: 3 * rate)
        try archive.append(Data(repeating: 0, count: 6 * rate))
        archive.finish(id: id, sample: 5 * rate)
        let interrupted = nexID()
        archive.start(id: interrupted, sample: 6 * rate)
        archive.close()

        let watchdog = Task {
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled else { return }
            events.continuation.finish()
        }
        defer { watchdog.cancel() }
        var completed = 0
        for await error in events.stream {
            precondition(error == nil, error ?? "")
            completed += 1
            if completed == 4 { break }
        }
        let clips = try spool.clips().sorted { $0.started < $1.started }
        precondition(completed == 4 && clips.count == 4, "All ranges should be saved before and after a turn.")
        precondition(clips.map(\.mode) == [.ambient, .voice, .ambient, .voice])
        precondition(clips.map(\.duration) == [2, 2, 1, 1])
        precondition(clips[1].id == id && clips[1].liveHandled == true)
        precondition(clips[3].id == interrupted && clips[3].liveHandled == true)
        for (first, next) in zip(clips, clips.dropFirst()) {
            precondition(abs(next.started.timeIntervalSince(first.started) - (first.duration ?? 0)) < 0.001)
        }
        print("Live archive sample boundaries, wake seed exclusion, and interrupted-turn recovery passed.")
    }
}
