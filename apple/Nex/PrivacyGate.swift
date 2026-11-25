import SwiftUI
#if os(macOS)
import AppKit
import Combine
#endif

struct PrivacyGate<Content: View>: View {
    @Environment(\.scenePhase) private var phase
    @ObservedObject var model: Controller
    @ObservedObject private var privacy: AppPrivacy
    @ObservedObject private var autoLock: AppAutoLock
    #if os(macOS)
    @State private var windowID = UUID()
    #endif
    private let content: () -> Content

    init(model: Controller, @ViewBuilder content: @escaping () -> Content) {
        self.model = model
        privacy = model.privacy
        autoLock = model.privacy.autoLock
        self.content = content
    }

    private var isActive: Bool { phase == .active && !autoLock.isAway }

    var body: some View {
        Group {
            if model.serverProfile == nil {
                NexServerSignInView(model: model)
                    .accessibilityHidden(!isActive)
                    .allowsHitTesting(isActive)
                    .overlay { if !isActive { Rectangle().fill(.background).ignoresSafeArea() } }
            } else if privacy.unlocked {
                content()
                    .privacySensitive()
                    .accessibilityHidden(!isActive)
                    .allowsHitTesting(isActive)
                    .overlay { if !isActive { Rectangle().fill(.background).ignoresSafeArea() } }
            } else {
                VStack(spacing: 14) {
                    Image(systemName: "lock.fill").font(.largeTitle)
                    Text("Nex is locked").font(.title2.bold())
                    AccountSignInUnlockHint(signIn: model.accountSignIn)
                    if let error = model.signOutError ?? privacy.error { Text(error).multilineTextAlignment(.center) }
                    Button("Unlock Nex") { Task { await privacy.unlock(model) } }
                        .buttonStyle(.borderedProminent).disabled(privacy.authenticating || model.signingOut)
                    if model.serverProfile != nil {
                        Button("Sign Out") { Task { await model.signOut() } }
                            .disabled(privacy.authenticating || model.signingOut)
                    }
                }.padding().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: isActive) {
            guard model.serverProfile != nil, isActive, !Task.isCancelled, !privacy.unlocked, !privacy.authenticating,
                  autoLock.takeAutomaticUnlockAttempt() else { return }
            Task { await privacy.unlock(model) }
        }
        #if os(macOS)
        .onAppear { privacy.macVisibility.showWindow(windowID) }
        .onDisappear { privacy.macVisibility.hideWindow(windowID) }
        #endif
    }
}

#if os(macOS)
@MainActor
final class MacWorkspaceVisibility {
    private weak var privacy: AppPrivacy?
    private var observers: Set<AnyCancellable> = []
    private var windows: Set<UUID> = []
    private var hasActiveScene = false

    init(privacy: AppPrivacy) {
        self.privacy = privacy
        let workspace = NSWorkspace.shared.notificationCenter
        Publishers.Merge3(
            workspace.publisher(for: NSWorkspace.sessionDidResignActiveNotification),
            workspace.publisher(for: NSWorkspace.screensDidSleepNotification),
            DistributedNotificationCenter.default().publisher(for: .init("com.apple.screenIsLocked"))
        )
        .receive(on: DispatchQueue.main)
        .sink { [weak privacy] _ in privacy?.lock() }
        .store(in: &observers)

        Publishers.Merge(
            NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification),
            NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)
        )
        .sink { [weak self] _ in self?.update() }
        .store(in: &observers)
    }

    func setSceneActive(_ active: Bool) {
        hasActiveScene = active
        update()
    }

    func showWindow(_ id: UUID) {
        windows.insert(id)
        update()
    }

    func hideWindow(_ id: UUID) {
        windows.remove(id)
        update()
    }

    private func update() {
        guard let privacy else { return }
        if hasActiveScene && NSApp.isActive && !windows.isEmpty {
            privacy.autoLock.enter { privacy.lock() }
        } else if !privacy.presentingBiometrics || windows.isEmpty {
            privacy.autoLock.leave { [weak privacy] in privacy?.lock() }
        }
    }
}
#endif

private struct AccountSignInUnlockHint: View {
    @ObservedObject var signIn: AccountSignIn

    var body: some View {
        if signIn.awaitingUnlock {
            Text("Unlock Nex to finish connecting your account.")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }
}
