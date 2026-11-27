import Combine
import Foundation
import LocalAuthentication

@MainActor
final class AppPrivacy: ObservableObject {
    let autoLock = AppAutoLock()
    @Published private(set) var unlocked = false
    @Published private(set) var connected = false
    @Published private(set) var authenticating = false
    private(set) var presentingBiometrics = false
    @Published private(set) var items: [PrivateAnswer] = []
    @Published private(set) var resolvingRequest: String?
    @Published var error: String?
    private var context: LAContext?
    private weak var activeAPI: NexAPI?
    private var inboxFetch = 0
    private(set) var generation = UUID()
    #if os(macOS)
    private var screenWork: String?
    private(set) lazy var macVisibility = MacWorkspaceVisibility(privacy: self)
    #endif

    func lock() {
        autoLock.cancelExpiry()
        unlocked = false
        disconnect()
    }

    func disconnect() {
        generation = UUID()
        cancelBiometrics()
        if let api = activeAPI, let token = api.workspaceToken {
            api.workspaceToken = nil
            Task { _ = try? await api.bytes("/v1/privacy/lock", method: "POST", headers: ["X-Nex-Workspace": token]) }
        }
        activeAPI = nil
        #if os(macOS)
        screenWork = nil
        #endif
        connected = false
        items = []
        resolvingRequest = nil
        error = nil
    }

    func cancelBiometrics() {
        context?.invalidate()
        context = nil
        authenticating = false
        presentingBiometrics = false
    }

    func unlock(_ model: Controller) async {
        guard !authenticating, !autoLock.isAway else { return }
        let opening = generation
        do {
            try await withBiometrics("Open your Nex workspace") { context in
                self.unlocked = true
                if model.serverProfile != nil {
                    try await self.startSession(model.connection(), context: context)
                }
            }
        } catch NexError.api(401, _) {
            if opening == generation { await model.signOut(revoke: false) }
        } catch {
            if opening == generation, !autoLock.isAway, !Task.isCancelled {
                self.error = error.localizedDescription
            }
        }
    }

    private func startSession(_ api: NexAPI, context: LAContext) async throws {
        let opening = generation
        let key = try OwnerKey.load(using: context, slot: api.biometricSlot)
        let proof = try await OwnerKey.proof(api, key: key, purpose: "unlock")
        guard opening == generation else { throw CancellationError() }
        let reply: [String: String] = try await api.send("/v1/privacy/unlock", body: proof)
        guard opening == generation, let token = reply["token"] else { throw CancellationError() }
        api.workspaceToken = token
        activeAPI = api
        connected = true
    }

    func signIn(_ link: NexServerLink, username: String, password: String,
                install: (ServerProfile, NexAPI) throws -> Void) async throws {
        disconnect()
        try await withBiometrics("Register this device for your Nex workspace") { context in
            let key = try OwnerKey.load(using: context, slot: UUID().uuidString)
            let profile = try await link.signIn(username: username, password: password,
                                                publicKey: key.publicKey.x963Representation)
            let api = try NexAPI(profile)
            do {
                guard self.context === context, !Task.isCancelled else { throw CancellationError() }
                try OwnerKey.save(key, slot: profile.biometricSlot)
                try await self.startSession(api, context: context)
                guard self.context === context, !Task.isCancelled else { throw CancellationError() }
                try install(profile, api)
                self.unlocked = true
            } catch {
                self.lock()
                OwnerKey.discard(profile.biometricSlot)
                try? await api.signOut()
                throw error
            }
        }
    }

    func withBiometrics<Value>(_ reason: String, perform: (LAContext) async throws -> Value) async throws -> Value {
        guard !authenticating else { throw NexError.connection("Finish the current biometric request first.") }
        let current = LAContext()
        current.localizedFallbackTitle = ""
        current.touchIDAuthenticationAllowableReuseDuration = 0
        var failure: NSError?
        guard current.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &failure) else {
            throw failure ?? NexError.connection("Set up biometrics in system settings to use Nex.") as NSError
        }
        context = current
        authenticating = true
        presentingBiometrics = true
        error = nil
        defer {
            current.invalidate()
            if context === current { context = nil; authenticating = false; presentingBiometrics = false }
        }
        guard try await current.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason),
              context === current else { throw CancellationError() }
        presentingBiometrics = false
        return try await perform(current)
    }

    func resolve(_ item: PrivateAnswer, model: Controller) async {
        struct Resolution: Encodable {
            let id: String
            let latitude: Double?
            let longitude: Double?
        }
        guard unlocked, connected, resolvingRequest == nil, let api = activeAPI else { return }
        let opening = generation
        resolvingRequest = item.id
        error = nil
        defer { if generation == opening { resolvingRequest = nil } }
        do {
            let location = item.needsLocation ? await WeatherLocationResult.current(for: item.id) : nil
            guard generation == opening, unlocked, api.workspaceToken != nil else { return }
            let request = Resolution(id: item.id, latitude: location?.latitude, longitude: location?.longitude)
            let _: [String: Bool] = try await api.send("/v1/privacy/resolve", body: request, timeout: 220)
            guard generation == opening else { return }
            try await refresh(model)
            if let alert = items.first(where: { $0.id == item.id })?.clockAlert {
                Task { await ClockNotifications.enable(for: alert, using: model) }
            }
        } catch { if generation == opening, unlocked { self.error = error.localizedDescription } }
    }

    func dismiss(_ item: PrivateAnswer, model: Controller) async {
        do {
            let _: [String: Bool] = try await model.connection().send("/v1/privacy/dismiss", body: ["id": item.id])
            try await refresh(model)
        } catch { if unlocked { self.error = error.localizedDescription } }
    }

    func observeInbox(_ model: Controller) async {
        guard connected else { return }
        while !Task.isCancelled {
            do {
                try await refresh(model)
            } catch NexError.api(403, _) {
                lock()
                return
            } catch {
                if !Task.isCancelled { self.error = error.localizedDescription }
            }
            do { try await Task.sleep(for: .seconds(3)) } catch { return }
        }
    }

    func refresh(_ model: Controller) async throws {
        struct Inbox: Decodable { let items: [PrivateAnswer] }
        let opening = generation
        guard unlocked, connected, let api = activeAPI else { return }
        inboxFetch += 1
        let fetch = inboxFetch
        let result: Inbox
        do { result = try await api.get("/v1/privacy/inbox") }
        catch {
            guard !Task.isCancelled, opening == generation, fetch == inboxFetch else { return }
            throw error
        }
        guard !Task.isCancelled, opening == generation, fetch == inboxFetch, unlocked else { return }
        if error != nil { error = nil }
        if items != result.items { items = result.items }
        #if os(macOS)
        if screenWork == nil, let work = items.first(where: { $0.state == "screen" }) {
            screenWork = work.id
            defer { if opening == generation { screenWork = nil } }
            let answer: String
            if model.screenSharing.isSharing {
                answer = try await model.screenSharing.answer(work.question, language: model.language.rawValue, spoken: false).text
            } else {
                answer = "Start sharing your Mac screen and ask again."
            }
            guard opening == generation, unlocked else { return }
            let _: [String: Bool] = try await api.send("/v1/privacy/screen", body: ["id": work.id, "answer": answer])
        }
        #endif
    }
}
