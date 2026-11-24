import SwiftUI
import UserNotifications

@main
struct NexApp: App {
    @Environment(\.scenePhase) private var phase
    #if os(iOS)
    @UIApplicationDelegateAdaptor(NexPhoneDelegate.self) private var phoneDelegate
    #endif
    @State private var setup = AppSetup()
    #if os(macOS)
    @State private var goalAlerts: Task<Void, Never>?
    #endif

    init() {
        UNUserNotificationCenter.current().delegate = NexNotificationPresenter.shared
        Task { await GoalNotifications.protectExistingAlerts() }
    }

    var body: some Scene {
        WindowGroup {
            if let model = setup.model {
                PrivacyGate(model: model) {
                    NexRoot(model: model).id(model.pairing?.workspaceScope)
                }
                    .modifier(ClockNotificationMonitor(model: model))
                    .task { await model.loop() }
                    #if os(macOS)
                    .frame(minWidth: 620, minHeight: 440)
                    #endif
            } else {
                ContentUnavailableView("Nex could not open its local queue", systemImage: "externaldrive.badge.exclamationmark",
                                       description: Text(setup.error ?? "The recording directory is unavailable."))
            }
        }
        .onChange(of: phase, initial: true) { _, next in updateWorkspaceVisibility(next) }
        #if os(macOS)
        .defaultSize(width: 1160, height: 760)
        .windowToolbarStyle(.unified)
        .commands {
            SidebarCommands()
            MacWorkspaceCommands()
        }
        #endif

        #if os(macOS)
        Settings {
            if let model = setup.model {
                PrivacyGate(model: model) { SettingsView(model: model) }
                    .frame(width: 720, height: 540)
                    .tint(WorkspaceStyle.accent)
            }
        }

        MenuBarExtra("Nex", systemImage: "waveform") {
            if let model = setup.model {
                CaptureMenu(model: model, capture: model.capture, privacy: model.privacy)
            }
        }
        #endif
    }

    private func updateWorkspaceVisibility(_ phase: ScenePhase) {
        guard let model = setup.model else { return }
        model.weatherSharing.setForeground(phase == .active)
        let privacy = model.privacy
        #if os(iOS)
        switch phase {
        case .active:
            privacy.autoLock.enter { model.lockWorkspace() }
        case .inactive where privacy.presentingBiometrics:
            break
        case .background, .inactive:
            if privacy.presentingBiometrics { privacy.cancelBiometrics() }
            privacy.autoLock.leave { [weak model] in model?.lockWorkspace() }
        @unknown default:
            model.lockWorkspace()
        }
        #else
        if goalAlerts == nil {
            goalAlerts = Task { await GoalNotifications.observeAlerts(using: model) }
        }
        privacy.macVisibility.setSceneActive(phase == .active)
        #endif
    }
}

@MainActor
private struct AppSetup {
    var model: Controller?
    var error: String?

    init() {
        do {
            let controller = try Controller(spool: Spool(), profile: PairingKeychain.load())
            model = controller
            #if os(iOS)
            PhoneWatchRelay.shared.attach(controller)
            #endif
        }
        catch { self.error = error.localizedDescription }
    }
}

#if os(macOS)
private struct CaptureMenu: View {
    @ObservedObject var model: Controller
    @ObservedObject var capture: Capture
    @ObservedObject var privacy: AppPrivacy

    var body: some View {
        if privacy.unlocked {
            Text(capture.status)
            Button(capture.ambient && !capture.interrupted ? "Pause capture" : "Start capture") {
                Task { await model.toggleRecording() }
            }
            Divider()
            ScreenSharingMenu(sharing: model.screenSharing)
        } else {
            Text("Open Nex to unlock")
        }
    }
}
#endif
