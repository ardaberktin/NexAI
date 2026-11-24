import AlarmKit
import OSLog
import SwiftUI
import UIKit

@available(iOS 26, *)
@MainActor
enum IPhoneNativeAlarms {
    private struct Metadata: AlarmMetadata {}

    private static let manager = AlarmManager.shared
    private static let log = Logger(subsystem: "com.ardaberktin.nex", category: "AlarmKit")
    private static var cancelled = Set<UUID>()
    private static var generation = 0

    static var authorization: AlarmManager.AuthorizationState { manager.authorizationState }

    static func requestAccess() async -> Bool {
        if authorization == .notDetermined, UIApplication.shared.applicationState == .active {
            return (try? await manager.requestAuthorization()) == .authorized
        }
        return authorization == .authorized
    }

    static func sync(_ alerts: [ClockNotificationAlert], using api: NexAPI, installation: String) async throws -> Set<String> {
        let alarms = alerts.filter { $0.kind == .alarm }
        let now = Date.now
        let future = alarms.filter { ($0.due ?? .distantPast) > now }
        let reported = Set(future.filter { $0.nativeInstalled == true }.map(\.id))
        let recent = Set(alarms.filter { $0.nativeInstalled == true && ($0.due ?? .distantFuture) <= now }.map(\.id))
        let installed: Set<String>
        do {
            installed = try await reconcile(future)
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            log.error("Could not read system alarms.")
            return reported.union(recent)
        }
        if !installed.isEmpty { ClockNotifications.removeFallback(for: installed) }
        if installed != reported {
            do {
                let update = NativeAlarmReport(installation: installation, alarms: installed.sorted())
                let _: [String: Bool] = try await api.send(ClockNotifications.path, method: "PUT", body: update, timeout: 8)
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                log.debug("Could not confirm system alarms with Excalibur.")
            }
        }
        return installed.union(recent)
    }

    private static func reconcile(_ upcoming: [ClockNotificationAlert]) async throws -> Set<String> {
        guard authorization == .authorized else { return [] }
        let system = Dictionary(uniqueKeysWithValues: try manager.alarms.map { ($0.id, $0) })
        let upcomingIDs = Set(upcoming.compactMap { UUID(uuidString: $0.id) })
        cancelled.formIntersection(upcomingIDs)
        for alarm in system.values where !upcomingIDs.contains(alarm.id) && alarm.state != .alerting {
            if case let .fixed(due) = alarm.schedule, due > .now {
                try? manager.cancel(id: alarm.id)
            }
        }
        var installed = Set<String>()
        let opening = generation
        for alert in upcoming {
            try Task.checkCancellation()
            guard let id = UUID(uuidString: alert.id), !cancelled.contains(id), let due = alert.due, due > .now else { continue }
            if system[id] != nil {
                installed.insert(alert.id)
                continue
            }
            do {
                _ = try await manager.schedule(id: id, configuration: configuration(due))
                if generation == opening && !cancelled.contains(id) {
                    installed.insert(alert.id)
                } else {
                    try? manager.cancel(id: id)
                }
            } catch {
                log.error("Could not schedule a system alarm.")
            }
        }
        try Task.checkCancellation()
        return installed
    }

    private static func configuration(_ due: Date) -> AlarmManager.AlarmConfiguration<Metadata> {
        let alert: AlarmPresentation.Alert
        if #available(iOS 26.1, *) {
            alert = .init(title: "Nex alarm")
        } else {
            alert = .init(title: "Nex alarm", stopButton: AlarmButton(text: "Stop", textColor: .white,
                                                                      systemImageName: "stop.circle"))
        }
        let attributes = AlarmAttributes(presentation: .init(alert: alert), metadata: Metadata(), tintColor: .orange)
        return .alarm(schedule: .fixed(due), attributes: attributes, sound: .default)
    }

    static func cancel(_ identifier: String) {
        guard let id = UUID(uuidString: identifier) else { return }
        cancelled.insert(id)
        try? manager.cancel(id: id)
    }

    static func clear() {
        generation += 1
        for alarm in (try? manager.alarms) ?? [] {
            if alarm.state == .alerting { try? manager.stop(id: alarm.id) }
            else { try? manager.cancel(id: alarm.id) }
        }
    }

    private struct NativeAlarmReport: Encodable {
        let installation: String
        let alarms: [String]
    }
}

@available(iOS 26, *)
struct IPhoneNativeAlarmSetting: View {
    @Environment(\.openURL) private var openURL
    @State private var authorization = IPhoneNativeAlarms.authorization

    var body: some View {
        Group {
            switch authorization {
            case .authorized:
                Label("iPhone alarm access allowed", systemImage: "alarm")
            case .notDetermined:
                Button("Enable iPhone alarms", systemImage: "alarm") {
                    Task { _ = await IPhoneNativeAlarms.requestAccess() }
                }
            case .denied:
                Button("Open iPhone alarm settings", systemImage: "gear") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                }
            @unknown default:
                EmptyView()
            }
        }
        .task {
            authorization = IPhoneNativeAlarms.authorization
            for await state in AlarmManager.shared.authorizationUpdates {
                authorization = state
            }
        }
    }
}
