import Foundation

enum AccountProvider: String, Encodable {
    case google, ticktick

    var signInHost: String {
        switch self {
        case .google: "accounts.google.com"
        case .ticktick: "ticktick.com"
        }
    }
}

enum ConnectedAccount: String, Encodable {
    case gmail, calendar, ticktick

    var provider: AccountProvider { self == .ticktick ? .ticktick : .google }
}

enum AccountWriteMode: String, Encodable {
    case off, interactive, scheduled
}

struct GoogleClientOptions: Decodable {
    let googleNative: Bool
    let googleDesktop: Bool
}

struct GoogleAccountClient: Encodable {
    let clientId: String
    let clientSecret: String?
    var desktop: Bool { clientSecret != nil }

    private enum CodingKeys: String, CodingKey {
        case clientId = "client_id"
        case clientSecret = "client_secret"
        case desktop
    }

    func encode(to encoder: any Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(clientId, forKey: .clientId)
        try values.encodeIfPresent(clientSecret, forKey: .clientSecret)
        try values.encode(desktop, forKey: .desktop)
    }

    #if os(macOS)
    static func readDesktopFile(_ url: URL) throws -> Self {
        let accessing = url.startAccessingSecurityScopedResource()
        defer { if accessing { url.stopAccessingSecurityScopedResource() } }
        let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard size > 0 && size <= 8192 else { throw AccountSignInError.invalidClientFile }
        struct FileContents: Decodable {
            struct Client: Decodable {
                let clientId: String
                let clientSecret: String
            }
            let installed: Client
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let file = try? decoder.decode(FileContents.self, from: Data(contentsOf: url)) else {
            throw AccountSignInError.invalidClientFile
        }
        return Self(clientId: file.installed.clientId, clientSecret: file.installed.clientSecret)
    }
    #endif
}

struct AccountAuthorization: Decodable {
    let url: URL
    let state: String
    let redirect: URL
}

enum AccountSignInError: LocalizedError {
    case invalidClientFile, invalidProvider, localCallback, expired, browserUnavailable, browserClosed, googlePermissions

    var errorDescription: String? {
        switch self {
        case .invalidClientFile: "Choose the Desktop OAuth client JSON downloaded from Google Cloud."
        case .invalidProvider: "Excalibur returned an unexpected sign-in address."
        case .localCallback: "Nex couldn’t open the local Google sign-in callback."
        case .expired: "This sign-in expired. Try again from Nex Settings."
        case .browserUnavailable: "Nex couldn’t open the sign-in browser. Open Settings and try again."
        case .browserClosed: "Sign-in closed before Nex received a response. Try again and finish approving access in the browser."
        case .googlePermissions: "Google granted only some of the required access. Reconnect Google and approve Gmail and both Calendar permissions."
        }
    }
}

extension NexAPI {
    private struct AccountConnections: Decodable {
        let connections: [String: String]
    }

    func googleClientOptions() async throws -> GoogleClientOptions {
        try await get("/v1/accounts/setup")
    }

    func saveGoogleClient(_ client: GoogleAccountClient) async throws {
        let _: GoogleClientOptions = try await send("/v1/accounts/setup", method: "PUT", body: client)
    }

    func startAccountSignIn(_ provider: AccountProvider, port: UInt16? = nil,
                            enableWrite: ConnectedAccount? = nil) async throws -> AccountAuthorization {
        struct Start: Encodable {
            let provider: AccountProvider
            let port: UInt16?
            let enableWrite: ConnectedAccount?

            enum CodingKeys: String, CodingKey {
                case provider, port
                case enableWrite = "enable_write"
            }
        }
        let attempt: AccountAuthorization = try await send(
            "/v1/accounts/start", body: Start(provider: provider, port: port, enableWrite: enableWrite), timeout: 20
        )
        guard attempt.url.scheme == "https", attempt.url.host == provider.signInHost,
              attempt.url.user == nil, attempt.url.password == nil else {
            throw AccountSignInError.invalidProvider
        }
        return attempt
    }

    func finishAccountSignIn(_ provider: AccountProvider, callback: URL) async throws -> [String: String] {
        struct Finish: Encodable {
            let provider: AccountProvider
            let callback: URL
        }
        let result: AccountConnections = try await send(
            "/v1/accounts/finish", body: Finish(provider: provider, callback: callback), timeout: 20
        )
        return result.connections
    }

    func disconnectAccount(_ provider: AccountProvider) async throws {
        let _: AccountConnections = try await send("/v1/accounts/disconnect", body: ["provider": provider])
    }

    func updateAccountWriteAccess(_ account: ConnectedAccount, mode: AccountWriteMode) async throws {
        struct Change: Encodable {
            let account: ConnectedAccount
            let mode: AccountWriteMode
        }
        struct Access: Decodable {
            let accountWriteAccess: [String: Bool]
        }
        let _: Access = try await send("/v1/accounts/access", method: "PUT", body: Change(account: account, mode: mode))
    }
}
