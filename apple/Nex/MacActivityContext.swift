#if os(macOS)
import AppKit
import Combine
import Foundation

@MainActor
final class MacActivityContext {
    private struct Snapshot: Equatable {
        let app: String?
        let mediaPlaying: Bool
    }

    private struct Payload: Encodable {
        let observed: String
        let app: String?
        let mediaPlaying: Bool

        private enum CodingKeys: String, CodingKey {
            case observed, app
            case mediaPlaying = "media_playing"
        }
    }

    private struct Result: Decodable {
        let ok: Bool
    }

    private var connection: NexAPI?
    private var enabled = false
    private var sleeping = false
    private var displaysSleeping = false
    private var inactiveSession = false
    private var monitor: Task<Void, Never>?
    private var lastSent: Snapshot?
    private var lastSentAt = Date.distantPast
    private var observers = Set<AnyCancellable>()

    init() {
        observe(NSWorkspace.didActivateApplicationNotification) { $0.sample(force: false) }
        observe(NSWorkspace.willSleepNotification) { $0.sleeping = true; $0.updateAvailability() }
        observe(NSWorkspace.didWakeNotification) { $0.sleeping = false; $0.updateAvailability() }
        observe(NSWorkspace.screensDidSleepNotification) { $0.displaysSleeping = true; $0.updateAvailability() }
        observe(NSWorkspace.screensDidWakeNotification) { $0.displaysSleeping = false; $0.updateAvailability() }
        observe(NSWorkspace.sessionDidResignActiveNotification) { $0.inactiveSession = true; $0.updateAvailability() }
        observe(NSWorkspace.sessionDidBecomeActiveNotification) { $0.inactiveSession = false; $0.updateAvailability() }
    }

    deinit {
        monitor?.cancel()
    }

    func configure(connection: NexAPI?, enabled: Bool) {
        let wasActive = self.connection != nil && self.enabled && !unavailable
        let previousConnection = self.connection
        self.connection = connection
        self.enabled = enabled
        let active = connection != nil && enabled && !unavailable
        if active {
            if previousConnection !== connection {
                lastSent = nil
                lastSentAt = .distantPast
            }
            startPolling()
            sample(force: true)
        } else {
            stopPolling()
            if wasActive, let target = connection ?? previousConnection {
                send(Snapshot(app: nil, mediaPlaying: false), to: target)
            }
            lastSent = nil
            lastSentAt = .distantPast
        }
    }

    private var unavailable: Bool {
        sleeping || displaysSleeping || inactiveSession
    }

    private func observe(_ name: Notification.Name, action: @escaping (MacActivityContext) -> Void) {
        NSWorkspace.shared.notificationCenter.publisher(for: name)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                action(self)
            }
            .store(in: &observers)
    }

    private func updateAvailability() {
        guard let connection, enabled else { return }
        if unavailable {
            stopPolling()
            send(Snapshot(app: nil, mediaPlaying: false), to: connection)
            lastSent = nil
            lastSentAt = .distantPast
        } else {
            startPolling()
            sample(force: true)
        }
    }

    private func startPolling() {
        guard monitor == nil else { return }
        monitor = Task(priority: .utility) { [weak self] in
            while !Task.isCancelled, let self {
                do { try await Task.sleep(for: .seconds(15)) }
                catch { break }
                self.sample(force: false)
            }
        }
    }

    private func stopPolling() {
        monitor?.cancel()
        monitor = nil
    }

    private func sample(force: Bool) {
        guard let connection, enabled, !unavailable else { return }
        let app = NSWorkspace.shared.frontmostApplication?.localizedName
        let snapshot = Snapshot(app: app.map { String($0.prefix(120)) },
                                mediaPlaying: DeviceAudioPlayback.isPlaying)
        let now = Date.now
        guard force || snapshot != lastSent || now.timeIntervalSince(lastSentAt) >= 45 else { return }
        lastSent = snapshot
        lastSentAt = now
        send(snapshot, to: connection)
    }

    private func send(_ snapshot: Snapshot, to api: NexAPI) {
        let payload = Payload(observed: NexDate.wire(.now), app: snapshot.app, mediaPlaying: snapshot.mediaPlaying)
        Task {
            let _: Result? = try? await api.send("/v1/context/device", method: "PUT", body: payload, timeout: 4)
        }
    }
}
#endif
