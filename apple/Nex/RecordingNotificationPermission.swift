import SwiftUI
import UserNotifications

struct RecordingNotificationPermission<Details: View>: View {
    @Environment(\.scenePhase) private var phase
    @State private var authorization: UNAuthorizationStatus?
    var onEnable: () async -> Void = {}
    @ViewBuilder var details: () -> Details

    var body: some View {
        Group {
            switch authorization {
            case nil:
                ProgressView("Checking notification permissions…")
                    .controlSize(.small)
            case .notDetermined:
                Button("Enable device notifications", systemImage: "bell") {
                    Task {
                        let granted = await RecordingNotifications.requestAccess()
                        await refresh()
                        if granted { await onEnable() }
                    }
                }
            case .denied:
                Text("Allow Nex notifications in this device’s system settings.")
                    .foregroundStyle(.secondary)
            case .authorized, .ephemeral:
                Text("Recording interruption alerts enabled")
                details()
            case .provisional:
                Text("Recording alerts are delivered quietly")
                details()
            default:
                EmptyView()
            }
        }
        .task(id: phase) { await refresh() }
    }

    private func refresh() async {
        authorization = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }
}
