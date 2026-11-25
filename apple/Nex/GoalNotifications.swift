import Foundation
import OSLog
import UserNotifications

private let scheduledAlertPrefix = "nex.goal."
private let privateAlertBody = "Unlock Nex to view your scheduled goal."
private let goalLog = Logger(subsystem: "com.ardaberktin.nex", category: "Goal notifications")

final class NexNotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NexNotificationPresenter()

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        if notification.request.identifier == RecordingNotifications.identifier { return [.banner, .list, .sound] }
        guard isGoalAlert(notification) else { return [] }
        await MainActor.run { NotificationCenter.default.post(name: .nexInboxChanged, object: nil) }
        goalLog.info("Presenting a Nex goal alert.")
        return [.banner, .list, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        guard isGoalAlert(response.notification) else { return }
        await MainActor.run { NotificationCenter.default.post(name: .nexOpenInbox, object: nil) }
    }

    private func isGoalAlert(_ notification: UNNotification) -> Bool {
        let request = notification.request
        if request.identifier.hasPrefix(scheduledAlertPrefix) || request.identifier.hasPrefix(ClockNotifications.prefix) {
            return true
        }
        guard let event = request.content.userInfo["nex_event"] as? String else { return false }
        return UUID(uuidString: event) != nil
    }
}

@MainActor
enum GoalNotifications {
    private static func cursorKey(accountScope: String) -> String { "nex.goalEmailAlertCursor." + accountScope }

    static func eraseCursor(accountScope: String) {
        UserDefaults.standard.removeObject(forKey: cursorKey(accountScope: accountScope))
    }

    static func protectExistingAlerts() async {
        let center = UNUserNotificationCenter.current()
        let delivered = await center.deliveredNotifications().map(\.request).filter(isOldLocalAlert)
        center.removeDeliveredNotifications(withIdentifiers: delivered.map(\.identifier))
        for request in await center.pendingNotificationRequests().filter(isOldLocalAlert) {
            do {
                try await center.add(UNNotificationRequest(identifier: request.identifier, content: scheduledContent(),
                                                            trigger: request.trigger))
            } catch {
                center.removePendingNotificationRequests(withIdentifiers: [request.identifier])
            }
        }
    }

    private static func isOldLocalAlert(_ request: UNNotificationRequest) -> Bool {
        request.identifier.hasPrefix(scheduledAlertPrefix) && request.content.body != privateAlertBody
    }

    private static func scheduledContent() -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = "Nex"
        content.body = privateAlertBody
        content.sound = .default
        return content
    }

    static func enable(using model: Controller) async {
        guard model.workspaceFeatureEnabled(.goals), await RecordingNotifications.requestAccess() else { return }
        #if os(iOS)
        await IPhonePush.shared.refresh(using: model, immediately: true)
        if let goals = try? await model.goals() { await sync(goals, using: model) }
        #endif
    }

    #if os(macOS)
    private struct GoalAlert: Decodable {
        let id: String
        let sequence: Int
    }

    static func observeAlerts(using model: Controller) async {
        while !Task.isCancelled {
            await deliverAlerts(using: model)
            do { try await Task.sleep(for: .seconds(12)) } catch { return }
        }
    }

    private static func deliverAlerts(using model: Controller) async {
        guard model.workspaceFeatureEnabled(.goals),
              let profile = model.serverProfile, let api = try? model.connection() else { return }
        let center = UNUserNotificationCenter.current()
        let authorization = await center.notificationSettings().authorizationStatus
        guard authorization == .authorized || authorization == .provisional else { return }
        let storage = UserDefaults.standard
        let key = cursorKey(accountScope: profile.workspaceScope)
        let cursor = storage.integer(forKey: key)
        let query = [URLQueryItem(name: "after", value: String(cursor))]
        guard let response: ListResponse<GoalAlert> = try? await api.get("/v1/workspace/goal-alerts", query: query),
              model.serverProfile?.workspaceScope == profile.workspaceScope, !Task.isCancelled else { return }
        for alert in response.items {
            guard model.workspaceFeatureEnabled(.goals), !Task.isCancelled else { return }
            let content = UNMutableNotificationContent()
            content.title = "Nex"
            content.body = "A goal update is ready."
            content.sound = .default
            content.userInfo = ["nex_event": alert.id]
            do {
                try await center.add(UNNotificationRequest(identifier: alert.id, content: content, trigger: nil))
                storage.set(alert.sequence, forKey: key)
            } catch {
                goalLog.error("Could not deliver a goal alert: \(error.localizedDescription, privacy: .private)")
                return
            }
        }
    }
    #endif

    static func sync(_ goals: [NexGoal], using model: Controller) async {
        guard !Task.isCancelled, model.workspaceFeatureEnabled(.goals) else { return }
        #if os(macOS)
        await removeAll()
        #else
        if IPhonePush.shared.state == .registered {
            await removeAll()
            return
        }
        let center = UNUserNotificationCenter.current()
        let authorization = await center.notificationSettings().authorizationStatus
        let pending = await center.pendingNotificationRequests()
        guard !Task.isCancelled, model.workspaceFeatureEnabled(.goals),
              authorization == .authorized || authorization == .provisional else { return }
        let now = Date.now
        let active = goals.filter { $0.status == .active && $0.frequency == .once }
        let candidates = active.compactMap { goal -> (NexGoal, Date)? in
            guard goal.nextRun != nil, let date = goal.scheduledAt.flatMap(WorkspaceDate.instant), date > now else {
                return nil
            }
            return (goal, date)
        }.sorted { $0.1 < $1.1 }.prefix(48)
        let firing = active.compactMap { goal -> String? in
            // Leave an alert due at this instant in Apple's queue long enough to be delivered.
            guard let due = goal.scheduledAt.flatMap(WorkspaceDate.instant),
                  due <= now, due > now.addingTimeInterval(-60) else { return nil }
            return scheduledAlertPrefix + goal.id
        }
        let ids = Set(candidates.map { scheduledAlertPrefix + $0.0.id }).union(firing)
        center.removePendingNotificationRequests(withIdentifiers: pending.map(\.identifier).filter {
            $0.hasPrefix(scheduledAlertPrefix) && !ids.contains($0)
        })
        for (goal, due) in candidates {
            guard !Task.isCancelled, model.workspaceFeatureEnabled(.goals) else { return }
            let identifier = scheduledAlertPrefix + goal.id
            var clock = Calendar(identifier: .gregorian)
            clock.timeZone = .gmt
            var components = clock.dateComponents([.year, .month, .day, .hour, .minute, .second], from: due)
            components.timeZone = .gmt
            let trigger = UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
            if pending.contains(where: {
                $0.identifier == identifier && $0.content.body == privateAlertBody &&
                    ($0.trigger as? UNCalendarNotificationTrigger)?.dateComponents == components
            }) { continue }
            do {
                try await center.add(UNNotificationRequest(identifier: identifier, content: scheduledContent(), trigger: trigger))
                if !model.workspaceFeatureEnabled(.goals) {
                    center.removePendingNotificationRequests(withIdentifiers: [identifier])
                    return
                }
                goalLog.info("Scheduled a local goal notification.")
            } catch {
                goalLog.error("Could not schedule a local goal notification: \(error.localizedDescription, privacy: .private)")
            }
        }
        #endif
    }

    static func removeScheduledAlert(for goalID: String) {
        UNUserNotificationCenter.current().removePendingNotificationRequests(
            withIdentifiers: [scheduledAlertPrefix + goalID]
        )
    }

    static func removeAll() async {
        let center = UNUserNotificationCenter.current()
        let pending = await center.pendingNotificationRequests()
        guard !Task.isCancelled else { return }
        let identifiers = pending.map(\.identifier).filter { $0.hasPrefix(scheduledAlertPrefix) }
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
    }
}

extension Notification.Name {
    static let nexInboxChanged = Self("NexInboxChanged")
    static let nexOpenInbox = Self("NexOpenInbox")
}
