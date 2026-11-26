import SwiftUI

struct SettingsPage: View {
    @ObservedObject var model: Controller
    let category: SettingsCategory

    var body: some View {
        Form {
            switch category {
            case .workspace:
                WorkspaceFeatureSettings(model: model)
            case .speech:
                SpeechRecordingSettings(model: model)
                if model.serverProfile != nil {
                    VoiceEnrollment(model: model, privacy: model.privacy)
                }
            case .notifications:
                NexNotificationSettings(model: model)
            case .privacy:
                AuthenticationSettings(autoLock: model.privacy.autoLock, lock: model.lockWorkspace)
                ChatPrivacySettings(model: model, privacy: model.privacy)
                if model.serverProfile != nil {
                    WeatherSharingSettings(model: model, sharing: model.weatherSharing)
                }
            case .nexAccount:
                HostedAccountSettings(model: model)
            case .accounts:
                AccountConnectionsView(model: model)
            case .mcp:
                MCPSettings(model: model)
            case .excalibur:
                NexServerSettings(model: model)
            case .models:
                if model.serverProfile != nil {
                    ExcaliburModelSettings(model: model)
                } else {
                    Section("Excalibur language model") {
                        Text("Sign in to Nex to manage its language model.")
                            .foregroundStyle(.secondary)
                    }
                }
                #if os(macOS)
                MacGemmaSettings(gemma: model.gemma)
                #endif
            #if os(iOS)
            case .watch:
                PhoneWatchSettings(model: model)
            #endif
            }
        }
        .formStyle(.grouped)
        .navigationTitle(category.title)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
    }
}

private struct WeatherSharingSettings: View {
    let model: Controller
    @ObservedObject var sharing: WeatherSharing

    var body: some View {
        Section {
            Toggle("Share approximate location", isOn: Binding(get: { sharing.enabled }, set: { value in
                Task { await sharing.setEnabled(value, using: model) }
            }))
            .disabled(sharing.busy)
            if let status = sharing.status { Text(status).foregroundStyle(.secondary) }
        } header: {
            Text("Location sharing")
        } footer: {
            Text("Excalibur keeps your device’s latest approximate location for local weather and scheduled reports.")
            #if os(iOS)
            Text("Choose Always in iPhone Location Services to update it when Nex is in the background.")
            #endif
        }
    }
}

private struct NexNotificationSettings: View {
    @ObservedObject var model: Controller
    #if os(iOS)
    @ObservedObject private var push = IPhonePush.shared
    #endif

    var body: some View {
        Section {
            #if os(iOS)
            if #available(iOS 26, *), model.serverProfile != nil, model.workspaceFeatureEnabled(.goals) {
                IPhoneNativeAlarmSetting()
            }
            #endif
            RecordingNotificationPermission(onEnable: { await GoalNotifications.enable(using: model) }) {
                if model.serverProfile != nil && model.workspaceFeatureEnabled(.goals) {
                    #if os(iOS)
                    switch push.state {
                    case .registered: Text("Apple Push Notifications registered")
                    case .connecting: Text("Connecting to Apple Push Notifications…")
                    case .local: Text("On-device timed reminders enabled")
                    }
                    #else
                    Text("Timed reminders and Gmail alerts enabled while Nex is running")
                    #endif
                }
            }
        }
    }
}
