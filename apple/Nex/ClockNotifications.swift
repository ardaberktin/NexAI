import OSLog
import SwiftUI
import UserNotifications

struct ClockNotificationAlert: Decodable {
    let id: String
    let kind: ClockAlert.Kind
    let dueAt: String
    let nativeInstalled: Bool?

    var due: Date? { WorkspaceDate.instant(dueAt) }
}

@MainActor
enum ClockNotifications {
    nonisolated static let prefix = "nex.clock."
    static let path = "/v1/workspace/clock-alerts"
    private static let log = Logger(subsystem: "com.ardaberktin.nex", category: "Clock notifications")

    private struct Schedule: Decodable {
        let items: [ClockNotificationAlert]
        let usesPush: Bool?
    }

    static func observe(using model: Controller) async {
        guard model.workspaceFeatureEnabled(.goals), model.pairing != nil else {
            await clear()
            return
        }
        let center = UNUserNotificationCenter.current()
        var known = Set<String>()
        while !Task.isCancelled {
            let authorization = await center.notificationSettings().authorizationStatus
            let allowed = authorization == .authorized || authorization == .provisional
            let installation = nativeInstallation
            if allowed || installation != nil, let api = try? model.connection() {
                do {
                    let query = installation.map { [URLQueryItem(name: "native_installation", value: $0)] } ?? []
                    let schedule: Schedule = try await api.get(path, query: query, timeout: 8)
                    try Task.checkCancellation()
                    let fallback = try await notificationFallback(schedule, using: api, installation: installation)
                    if allowed { try await sync(fallback, known: &known, center: center) }
                } catch NexError.api(404, _) {
                    return // An older Excalibur service has no clock schedule.
                } catch is CancellationError {
                    return
                } catch {
                    log.debug("Clock notification sync is temporarily unavailable.")
                }
            }
            do { try await Task.sleep(for: .seconds(allowed || installation != nil ? 2 : 5)) } catch { return }
        }
    }

    private static var nativeInstallation: String? {
        #if os(iOS)
        if #available(iOS 26, *) { return IPhonePush.shared.installation }
        #endif
        return nil
    }

    private static func notificationFallback(_ schedule: Schedule, using api: NexAPI,
                                             installation: String?) async throws -> [ClockNotificationAlert] {
        #if os(iOS)
        if #available(iOS 26, *), let usesPush = schedule.usesPush, let installation {
            let installed = try await IPhoneNativeAlarms.sync(schedule.items, using: api, installation: installation)
            return usesPush ? [] : schedule.items.filter { !installed.contains($0.id) }
        }
        #endif
        return schedule.items
    }

    static func enable(for alert: ClockAlert, using model: Controller) async {
        guard model.workspaceFeatureEnabled(.goals), alert.status == .scheduled, let due = alert.due, due > .now else { return }
        #if os(iOS)
        if #available(iOS 26, *), alert.kind == .alarm, await IPhoneNativeAlarms.requestAccess() { return }
        #endif
        await GoalNotifications.enable(using: model)
    }

    private static func sync(_ alerts: [ClockNotificationAlert], known: inout Set<String>,
                             center: UNUserNotificationCenter) async throws {
        let pending = await center.pendingNotificationRequests().filter { $0.identifier.hasPrefix(prefix) }
        let delivered = await center.deliveredNotifications().map(\.request.identifier)
        try Task.checkCancellation()
        // Keep space on iPhone for the existing goal and recording notification queues.
        let next = alerts.prefix(15)
        let ids = Set(next.map { prefix + $0.id })
        let waiting = Set(pending.map(\.identifier))
        let received = Set(delivered.filter { $0.hasPrefix(prefix) })
        known.formUnion(waiting.union(received))
        known.formIntersection(ids)
        center.removePendingNotificationRequests(withIdentifiers: Array(waiting.subtracting(ids)))
        for alert in next {
            try Task.checkCancellation()
            let id = prefix + alert.id
            guard !waiting.contains(id), let due = alert.due else { continue }
            let remaining = due.timeIntervalSinceNow
            if remaining <= 0 && known.contains(id) { continue }
            let content = UNMutableNotificationContent()
            content.title = "Nex"
            content.body = alert.kind == .timer ? String(localized: "Timer finished") : String(localized: "Alarm")
            content.sound = .default
            let trigger = remaining > 0
                ? UNTimeIntervalNotificationTrigger(timeInterval: max(1, remaining), repeats: false) : nil
            try await center.add(UNNotificationRequest(identifier: id, content: content, trigger: trigger))
            known.insert(id)
        }
    }

    static func removeFallback(for ids: Set<String>) {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ids.map { prefix + $0 })
    }

    static func remove(_ alert: ClockAlert) {
        removeFallback(for: [alert.id])
        #if os(iOS)
        if #available(iOS 26, *), alert.kind == .alarm { IPhoneNativeAlarms.cancel(alert.id) }
        #endif
    }

    static func clear() async {
        #if os(iOS)
        if #available(iOS 26, *) { IPhoneNativeAlarms.clear() }
        #endif
        let center = UNUserNotificationCenter.current()
        let pending = await center.pendingNotificationRequests().map(\.identifier).filter { $0.hasPrefix(prefix) }
        guard !Task.isCancelled else { return }
        center.removePendingNotificationRequests(withIdentifiers: pending)
    }
}

struct ClockNotificationMonitor: ViewModifier {
    @ObservedObject var model: Controller
    @Environment(\.scenePhase) private var phase

    private var observing: Bool {
        #if os(iOS)
        phase != .background
        #else
        true
        #endif
    }

    func body(content: Content) -> some View {
        content.task(id: Context(pairing: model.pairing?.pin, observing: observing,
                                 enabled: model.workspaceFeatureEnabled(.goals))) {
            guard observing else { return }
            await ClockNotifications.observe(using: model)
        }
    }

    private struct Context: Equatable {
        let pairing: String?
        let observing: Bool
        let enabled: Bool
    }
}
