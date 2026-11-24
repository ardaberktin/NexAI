import Combine
import Foundation

@MainActor
final class AppPrivacy {
    @Published var connected = true
    private(set) var generation = UUID()

    func lock() {
        generation = UUID()
        connected = false
    }
}

@MainActor
final class Controller {
    let privacy = AppPrivacy()
    let api = NexAPI()
    private(set) var refreshes = 0

    func connection() throws -> NexAPI { api }
    func refreshStatus() async throws { refreshes += 1 }
}

@MainActor
final class NexAPI {
    static let redirect = "com.googleusercontent.apps.fixture:/oauth2redirect"
    var finishStatuses = [200]
    private(set) var callbacks: [String] = []

    func get<T: Decodable>(_ path: String) async throws -> T {
        precondition(path == "/v1/accounts/setup")
        return try decode(["google_native": true, "google_desktop": false])
    }

    func send<T: Decodable, Body: Encodable>(_ path: String, method: String = "POST",
                                               body: Body, timeout: Double = 12) async throws -> T {
        switch path {
        case "/v1/accounts/start":
            return try decode(["url": "https://accounts.google.com/authorize", "redirect": Self.redirect, "state": "fixture"])
        case "/v1/accounts/finish":
            let payload = try JSONDecoder().decode([String: String].self, from: JSONEncoder().encode(body))
            callbacks.append(payload["callback"]!)
            let status = finishStatuses.removeFirst()
            if status != 200 { throw NexError.api(status, "Unlock the Nex workspace with biometrics.") }
            return try decode(["connections": ["gmail": "connected", "calendar": "connected"]])
        default: preconditionFailure("Unexpected account request")
        }
    }

    private func decode<T: Decodable, Body: Encodable>(_ value: Body) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONEncoder().encode(value))
    }
}

enum NexError: Error {
    case api(Int, String)
}

@MainActor
final class AccountBrowser {
    static let callback = NexAPI.redirect + "?code=fixture&state=fixture"
    static var onAuthorize: () throws -> Void = {}

    func authenticate(_ url: URL, scheme: String) async throws -> URL {
        try Self.onAuthorize()
        return URL(string: Self.callback)!
    }
}

@main
enum AccountSignInCheck {
    @MainActor
    static func main() async throws {
        for rejectedSession in [false, true] {
            let model = Controller()
            let signIn = AccountSignIn()
            if rejectedSession {
                model.api.finishStatuses = [403, 200]
                AccountBrowser.onAuthorize = {}
            } else {
                AccountBrowser.onAuthorize = model.privacy.lock
            }
            signIn.start(.google, model: model)
            try await waitUntil { signIn.awaitingUnlock }
            precondition(!model.privacy.connected && model.api.callbacks.count == (rejectedSession ? 1 : 0))
            model.privacy.connected = true
            try await waitUntil { signIn.provider == nil }
            precondition(signIn.confirmation != nil && signIn.error == nil && model.refreshes == 1)
            precondition(model.api.callbacks == Array(repeating: AccountBrowser.callback, count: rejectedSession ? 2 : 1))
        }

        let model = Controller()
        let signIn = AccountSignIn()
        AccountBrowser.onAuthorize = model.privacy.lock
        signIn.start(.google, model: model)
        try await waitUntil { signIn.awaitingUnlock }
        signIn.cancel()
        try await waitUntil { signIn.provider == nil }
        model.privacy.connected = true
        precondition(model.api.callbacks.isEmpty)

        AccountBrowser.onAuthorize = { throw AccountSignInError.browserClosed }
        signIn.start(.google, model: model)
        try await waitUntil { signIn.provider == nil }
        precondition(signIn.error == AccountSignInError.browserClosed.localizedDescription)
        print("Google sign-in waited for workspace unlock, retried a revoked session, and handled cancellation.")
    }

    @MainActor
    private static func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(3)
        while !condition() {
            precondition(ContinuousClock.now < deadline, "Expected sign-in state was never reached")
            try await Task.sleep(for: .milliseconds(10))
        }
    }
}
