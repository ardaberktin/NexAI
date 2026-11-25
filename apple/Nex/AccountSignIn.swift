import Combine
import Foundation
#if os(macOS)
import AppKit
#endif

@MainActor
final class AccountSignIn: ObservableObject {
    @Published private(set) var provider: AccountProvider?
    @Published private(set) var disconnecting: AccountProvider?
    @Published private(set) var changingWriteAccess = false
    @Published private(set) var awaitingUnlock = false
    @Published private(set) var error: String?
    @Published private(set) var confirmation: String?
    @Published var showGoogleSetup = false
    private(set) var setupWriteAccount: ConnectedAccount?
    private var task: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    var busy: Bool { provider != nil || disconnecting != nil || changingWriteAccess }
    #if os(iOS)
    private var returnToSettings = false

    func workspaceWillLock() {
        if busy { returnToSettings = true }
    }

    func takeSettingsReturn() -> Bool {
        defer { returnToSettings = false }
        return returnToSettings
    }
    #endif

    func start(_ provider: AccountProvider, model: Controller, client: GoogleAccountClient? = nil,
               enableWrite: ConnectedAccount? = nil) {
        guard task == nil else { return }
        self.provider = provider
        error = nil
        confirmation = nil
        task = Task {
            defer {
                timeout?.cancel()
                timeout = nil
                task = nil
                self.provider = nil
                awaitingUnlock = false
            }
            do {
                startTimeout()
                try await waitForWorkspace(model.privacy)
                let connection = try model.connection()
                guard let callback = try await authorize(provider, connection: connection, client: client,
                                                         enableWrite: enableWrite) else { return }
                let connections = try await finish(provider, callback: callback, connection: connection, privacy: model.privacy)
                try? await model.refreshStatus()
                try Task.checkCancellation()
                if provider == .google && ["gmail", "calendar"].contains(where: { connections[$0] != "connected" }) {
                    error = AccountSignInError.googlePermissions.localizedDescription
                } else if enableWrite != nil {
                    confirmation = String(localized: "Write access enabled.")
                } else {
                    confirmation = provider == .google ? "Gmail and Google Calendar connected." : "TickTick connected."
                }
            } catch {
                if !Task.isCancelled { self.error = error.localizedDescription }
            }
        }
    }

    func disconnect(_ provider: AccountProvider, model: Controller) {
        update(model: model, disconnecting: provider) { connection in
            try await connection.disconnectAccount(provider)
            return provider == .google ? String(localized: "Gmail and Google Calendar disconnected.")
                : String(localized: "TickTick disconnected.")
        }
    }

    func updateWriteAccess(_ account: ConnectedAccount, mode: AccountWriteMode, model: Controller) {
        update(model: model) { connection in
            try await connection.updateAccountWriteAccess(account, mode: mode)
            return nil
        }
    }

    func clearSetupWriteAccount() { setupWriteAccount = nil }

    private func update(model: Controller, disconnecting: AccountProvider? = nil,
                        operation: @escaping (NexAPI) async throws -> String?) {
        guard task == nil else { return }
        self.disconnecting = disconnecting
        changingWriteAccess = disconnecting == nil
        error = nil
        confirmation = nil
        task = Task {
            defer {
                task = nil
                self.disconnecting = nil
                changingWriteAccess = false
            }
            do {
                try await waitForWorkspace(model.privacy)
                let connection = try model.connection()
                let result = try await operation(connection)
                try? await model.refreshStatus()
                try Task.checkCancellation()
                confirmation = result
            } catch {
                if !Task.isCancelled { self.error = error.localizedDescription }
            }
        }
    }

    func cancel() { task?.cancel() }

    func reset() {
        cancel()
        error = nil
        confirmation = nil
        showGoogleSetup = false
        clearSetupWriteAccount()
        #if os(iOS)
        returnToSettings = false
        #endif
    }

    private func startTimeout() {
        timeout = Task {
            do { try await Task.sleep(for: .seconds(600)) }
            catch { return }
            error = AccountSignInError.expired.localizedDescription
            cancel()
        }
    }

    private func waitForWorkspace(_ privacy: AppPrivacy) async throws {
        try Task.checkCancellation()
        guard !privacy.connected else { return }
        awaitingUnlock = true
        defer { awaitingUnlock = false }
        for await connected in privacy.$connected.values where connected {
            try Task.checkCancellation()
            return
        }
        throw CancellationError()
    }

    private func finish(_ provider: AccountProvider, callback: URL, connection: NexAPI,
                        privacy: AppPrivacy) async throws -> [String: String] {
        try await waitForWorkspace(privacy)
        let generation = privacy.generation
        do {
            return try await connection.finishAccountSignIn(provider, callback: callback)
        } catch NexError.api(403, _) {
            // A lock can revoke the workspace session before Excalibur receives the callback.
            try Task.checkCancellation()
            if privacy.generation == generation {
                #if os(iOS)
                workspaceWillLock()
                #endif
                privacy.lock()
            }
            try await waitForWorkspace(privacy)
            return try await connection.finishAccountSignIn(provider, callback: callback)
        }
    }

    private func authorize(_ provider: AccountProvider, connection: NexAPI,
                           client: GoogleAccountClient?, enableWrite: ConnectedAccount?) async throws -> URL? {
        if provider == .google {
            if let client { try await connection.saveGoogleClient(client) }
            let options = try await connection.googleClientOptions()
            #if os(macOS)
            if options.googleDesktop && (client?.desktop == true || !options.googleNative) {
                return try await authorizeDesktop(using: connection, enableWrite: enableWrite)
            }
            #endif
            if !options.googleNative {
                setupWriteAccount = enableWrite
                showGoogleSetup = true
                return nil
            }
        }
        let attempt = try await connection.startAccountSignIn(provider, enableWrite: enableWrite)
        guard let scheme = attempt.redirect.scheme,
              provider == .google ? scheme.hasPrefix("com.googleusercontent.apps.") : scheme == "com.ardaberktin.nex.oauth" else {
            throw AccountSignInError.invalidProvider
        }
        return try await AccountBrowser().authenticate(attempt.url, scheme: scheme)
    }

    #if os(macOS)
    private func authorizeDesktop(using connection: NexAPI, enableWrite: ConnectedAccount?) async throws -> URL {
        let listener = try AccountLoopback()
        defer { listener.close() }
        var events = listener.events.makeAsyncIterator()
        guard case .ready(let port) = try await events.next() else { throw AccountSignInError.localCallback }
        let attempt = try await connection.startAccountSignIn(.google, port: port, enableWrite: enableWrite)
        listener.expect(state: attempt.state)
        try Task.checkCancellation()
        guard NSWorkspace.shared.open(attempt.url) else { throw AccountSignInError.localCallback }
        guard case .callback(let url) = try await events.next() else { throw AccountSignInError.expired }
        NSApp.activate()
        return url
    }
    #endif
}
