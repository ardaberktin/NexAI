#if os(iOS)
import Combine
import Foundation
import OSLog
import UIKit
import UserNotifications

struct PushRegistration: Encodable {
    let installation: String
    let token: String?
    let environment: String?
    let enabled: Bool
}

struct PushStatus: Decodable {
    let active: Bool
}

@MainActor
final class IPhonePush: ObservableObject {
    static let shared = IPhonePush()

    enum State {
        case local, connecting, registered
    }

    @Published private(set) var state: State = .local
    private let log = Logger(subsystem: "com.ardaberktin.nex", category: "Push")
    let installation: String
    private var nextAttempt = Date.distantPast
    private var busy = false
    private var token: String?
    private weak var model: Controller?

    private init() {
        let settings = UserDefaults.standard
        let identifier = settings.string(forKey: "nexPushInstallation") ?? nexID()
        settings.set(identifier, forKey: "nexPushInstallation")
        installation = identifier
    }

    private var environment: String? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "NexAPNsEnvironment") as? String,
              ["development", "production"].contains(value) else { return nil }
        return value
    }

    func refresh(using model: Controller, immediately: Bool = false) async {
        self.model = model
        guard model.workspaceFeatureEnabled(.goals), let environment, !busy, immediately || nextAttempt <= .now,
              let api = try? model.connection() else { return }
        busy = true
        defer { busy = false }
        nextAttempt = .now.addingTimeInterval(30)
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard !Task.isCancelled, isCurrent(api, in: model) else { return }
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
            if token != nil || UIApplication.shared.isRegisteredForRemoteNotifications { await disable(using: api) }
            return
        }
        guard let token else {
            state = .connecting
            UIApplication.shared.registerForRemoteNotifications()
            return
        }
        let request = PushRegistration(installation: installation, token: token, environment: environment, enabled: true)
        do {
            let status: PushStatus = try await api.send("/v1/workspace/push", method: "PUT", body: request)
            if !Task.isCancelled && isCurrent(api, in: model) {
                state = status.active ? .registered : .local
                nextAttempt = .now.addingTimeInterval(status.active ? 300 : 30)
            }
        } catch {
            if !Task.isCancelled { log.debug("Excalibur push registration is temporarily unavailable.") }
        }
    }

    func received(_ data: Data) {
        token = data.map { String(format: "%02x", $0) }.joined()
        nextAttempt = .distantPast
        if let model { Task { await refresh(using: model, immediately: true) } }
    }

    func registrationFailed() {
        nextAttempt = .now.addingTimeInterval(30)
        if token == nil { state = .local }
        log.error("iOS could not register Nex for remote notifications.")
    }

    func unpair(_ pairing: Pairing) {
        state = .local
        token = nil
        nextAttempt = .distantPast
        UIApplication.shared.unregisterForRemoteNotifications()
        guard environment != nil, let api = try? NexAPI(pairing) else { return }
        Task { await disable(using: api, updateState: false) }
    }

    private func disable(using api: NexAPI, updateState: Bool = true) async {
        if updateState {
            state = .local
            token = nil
            UIApplication.shared.unregisterForRemoteNotifications()
        }
        let request = PushRegistration(installation: installation, token: nil, environment: nil, enabled: false)
        let _: PushStatus? = try? await api.send("/v1/workspace/push", method: "PUT", body: request)
    }

    private func isCurrent(_ api: NexAPI, in model: Controller) -> Bool {
        switch try? model.connection() {
        case let connection?: return api === connection
        case nil: return false
        }
    }
}

@MainActor
final class NexPhoneDelegate: NSObject, UIApplicationDelegate {
    private var covers: [UIView] = []

    func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(coverWindows), name: UIApplication.willResignActiveNotification, object: nil)
        center.addObserver(self, selector: #selector(revealWindows), name: UIApplication.didBecomeActiveNotification, object: nil)
        return true
    }

    @objc private func coverWindows() {
        guard covers.isEmpty else { return }
        for scene in UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }) {
            for window in scene.windows where window.windowLevel == .normal && !window.isHidden {
                let cover = UIView(frame: window.bounds)
                cover.backgroundColor = .systemBackground
                cover.autoresizingMask = [.flexibleWidth, .flexibleHeight]
                cover.isUserInteractionEnabled = false
                window.addSubview(cover)
                covers.append(cover)
            }
        }
    }

    @objc private func revealWindows() {
        covers.forEach { $0.removeFromSuperview() }
        covers.removeAll()
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        IPhonePush.shared.received(token)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        IPhonePush.shared.registrationFailed()
    }
}
#endif
