import Combine
import Foundation
#if os(iOS)
import UIKit
#endif

@MainActor
final class RecordingReporter {
    private var reporting: Task<Void, Never>?

    func start(_ capture: Capture, connection: NexAPI?) {
        reporting?.cancel()
        reporting = connection.map { api in Task { await Self.report(capture, to: api) } }
    }

    deinit { reporting?.cancel() }

    private static func report(_ capture: Capture, to api: NexAPI) async {
        let (states, changes) = AsyncStream<Bool>.makeStream(bufferingPolicy: .bufferingNewest(1))
        let subscription = capture.$recording.sink { changes.yield($0) }
        let heartbeat = Task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(8)) } catch { return }
                changes.yield(capture.recording)
            }
        }
        defer {
            subscription.cancel()
            heartbeat.cancel()
            changes.finish()
        }
        // Serialize transitions and retain only the latest value during a network outage.
        for await recording in states {
            guard !Task.isCancelled else { return }
            let _: [String: Bool]? = try? await api.send(
                RecordingStatusSnapshot.path, method: "PUT", body: ["recording": recording], timeout: 4)
        }
    }
}

#if os(iOS)
@MainActor
final class WatchRecordingReporter {
    private let savedKey = "watch.recordingTransition"
    private var updates = WatchRecordingUpdates()
    private var pending = false
    private var reporting: Task<Void, Never>?

    init() {
        if let data = UserDefaults.standard.data(forKey: savedKey),
            let saved = try? JSONDecoder().decode(WatchRecordingUpdate.self, from: data)
        {
            updates = WatchRecordingUpdates(restoring: saved)
            pending = true
        }
    }

    func receive(_ report: WatchRecordingReport, using model: Controller?) {
        let previous = updates.persistentUpdate
        guard updates.receive(report, at: .now) else { return }
        if updates.persistentUpdate != previous { save() }
        pending = true
        sync(using: model)
    }

    func clear(using model: Controller?) {
        updates.clear(at: .now)
        save()
        pending = true
        sync(using: model)
    }

    private func save() {
        let data = updates.persistentUpdate.flatMap { try? JSONEncoder().encode($0) }
        UserDefaults.standard.set(data, forKey: savedKey)
    }

    func sync(using model: Controller?) {
        guard pending, reporting == nil, let connection = try? model?.connection() else { return }
        reporting = Task {
            let allowance = UIApplication.shared.beginBackgroundTask(withName: "Report Nex Watch recording") {
                [weak reporter = self] in
                reporter?.reporting?.cancel()
            }
            defer {
                reporting = nil
                if allowance != .invalid { UIApplication.shared.endBackgroundTask(allowance) }
            }
            var failures = 0
            while pending && !Task.isCancelled, let value = updates.payload(at: .now) {
                pending = false
                do {
                    let _: [String: Bool] = try await connection.send(
                        RecordingStatusSnapshot.path, method: "PUT", body: value, timeout: 4)
                    failures = 0
                } catch {
                    pending = true
                    failures += 1
                    guard failures < 3 else { return }
                    do { try await Task.sleep(for: .seconds(failures)) } catch { return }
                }
            }
        }
    }
}
#endif
