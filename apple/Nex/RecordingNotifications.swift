import Foundation
import OSLog
import UserNotifications

@MainActor
final class RecordingNotifications {
    nonisolated static let identifier = "nex.recording.interruption"
    private static let recoveryGrace: Duration = .seconds(10)
    private static let watchTimeout: Duration = .seconds(60)
    private static let log = Logger(subsystem: "com.ardaberktin.nex", category: "Recording notifications")

    private struct Alert: Equatable {
        let deadline: ContinuousClock.Instant
        let confirmed: Bool
    }

    private let device: RecordingDevice
    private let now: () -> ContinuousClock.Instant
    private let schedule: (UNNotificationRequest) async throws -> Void
    private let cancel: (String) -> Void
    private var pending: Alert?
    private var installed: Alert?
    private var needsUpdate = false
    private var updating = false

    init(device: RecordingDevice, now: @escaping () -> ContinuousClock.Instant = { .now },
         schedule: @escaping (UNNotificationRequest) async throws -> Void = {
             let center = UNUserNotificationCenter.current()
             switch await center.notificationSettings().authorizationStatus {
             case .authorized, .provisional, .ephemeral: try await center.add($0)
             default: throw UNError(.notificationsNotAllowed)
             }
         }, cancel: @escaping (String) -> Void = {
             UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: [$0])
         }) {
        self.device = device
        self.now = now
        self.schedule = schedule
        self.cancel = cancel
    }

    static func requestAccess() async -> Bool {
        (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) == true
    }

    static func requestAccessIfNeeded() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        if settings.authorizationStatus == .notDetermined { _ = await requestAccess() }
    }

    func interrupted() {
        let time = now()
        // An expired Watch fallback may already have alerted the user while Nex was suspended.
        guard pending?.confirmed != true else { return }
        if pending != nil, let installed, installed.deadline <= time { return }
        let deadline = time + Self.recoveryGrace
        update(Alert(deadline: min(deadline, pending?.deadline ?? deadline), confirmed: true))
    }

    func clear() { update(nil) }

    func watchRecordingStarted(at time: ContinuousClock.Instant) {
        if pending == nil { watchAudioReceived(at: time) }
    }

    func watchAudioReceived(at time: ContinuousClock.Instant) {
        let deadline = time + Self.watchTimeout
        guard deadline > now() else { return }
        if let pending, !pending.confirmed, pending.deadline >= deadline { return }
        update(Alert(deadline: deadline, confirmed: false))
    }

    private func update(_ alert: Alert?) {
        guard alert == nil || alert != pending else { return }
        pending = alert
        needsUpdate = true
        guard !updating else { return }
        updating = true
        Task { await applyUpdates() }
    }

    private func applyUpdates() async {
        // Apply a pause or recovery after any in-flight add finishes, so it cannot leave a stale alert behind.
        while needsUpdate {
            needsUpdate = false
            let next = pending
            if let next {
                do { try await schedule(request(for: next)) }
                catch {
                    if (error as? UNError)?.code != .notificationsNotAllowed {
                        Self.log.error("Could not schedule a recording alert: \(error.localizedDescription)")
                    }
                    if pending == next {
                        // Recovery is already confirmed by incoming audio even if rearming the Watch fallback fails.
                        if !next.confirmed && installed?.confirmed == true {
                            cancel(Self.identifier)
                            installed = nil
                        }
                        pending = installed
                    }
                    continue
                }
            } else {
                cancel(Self.identifier)
            }
            installed = next
        }
        updating = false
    }

    private func request(for alert: Alert) -> UNNotificationRequest {
        let content = UNMutableNotificationContent()
        content.title = alert.confirmed ? "Recording stopped on \(device.title)" : "Check your Watch recording"
        content.body = alert.confirmed ? "Open Nex to resume recording." : "Open Nex to check whether recording is still active."
        content.sound = .default
        let seconds = max(1, (alert.deadline - now()) / .seconds(1))
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: seconds, repeats: false)
        return UNNotificationRequest(identifier: Self.identifier, content: content, trigger: trigger)
    }
}

#if os(watchOS)
final class WatchNotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    static let shared = WatchNotificationPresenter()

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        notification.request.identifier == RecordingNotifications.identifier ? [.banner, .list, .sound] : []
    }
}
#endif
