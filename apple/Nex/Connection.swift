import CryptoKit
import Foundation
import Security

struct Pairing: Codable {
    let url: String
    let pin: String
    let token: String
    let device: String
    var setup: String? = nil
    var account: HostedAccount? = nil

    var workspaceScope: String {
        guard let account else { return pin }
        return SHA256.hash(data: Data((pin + ":" + account.accountId).utf8)).map { String(format: "%02x", $0) }.joined()
    }

    var biometricSlot: String { account.map { "account-device-" + $0.deviceId } ?? "owner" }
    var signingDevice: String { account.map { device + ":" + $0.deviceId } ?? device }

    static var platform: String {
        #if os(iOS)
        "iphone"
        #else
        "mac"
        #endif
    }

    func address() throws -> URL {
        guard let parts = URLComponents(string: url), let host = parts.host,
              parts.scheme == "https", parts.port == 8765, parts.path.isEmpty,
              parts.query == nil, parts.fragment == nil, parts.user == nil, parts.password == nil,
              Self.isTailscaleV4(host), let result = parts.url,
              Data(base64Encoded: pin)?.count == 32,
              token.count >= 40, token.count <= 120,
              token.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0)
                  || (97...122).contains($0) || $0 == 45 || $0 == 95 }),
              device == Self.platform, account == nil || (account?.valid == true && setup == nil) else {
            throw NexError.pairing("Use the updated Excalibur Tailscale pairing profile for this device.")
        }
        return result
    }

    static func isTailscaleV4(_ host: String) -> Bool {
        let segments = host.split(separator: ".", omittingEmptySubsequences: false)
        let numbers = segments.compactMap { UInt8($0) }
        guard segments.count == 4, numbers.count == 4, numbers.map(String.init).joined(separator: ".") == host else {
            return false
        }
        return numbers[0] == 100 && (64...127).contains(numbers[1])
    }
}

enum PairingKeychain {
    private static let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "com.ardaberktin.nex.pairing",
        kSecAttrAccount as String: "excalibur"
    ]

    static func load() -> Pairing? {
        var lookup = query
        lookup[kSecReturnData as String] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(lookup as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Pairing.self, from: data)
    }

    static func save(_ value: Pairing) throws {
        let data = try JSONEncoder().encode(value)
        #if os(iOS)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        #else
        let attributes: [String: Any] = [kSecValueData as String: data]
        #endif
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes) { _, value in value } as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw NexError.pairing("Could not save the profile in Keychain (\(status)).")
        }
    }

    static func clear() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NexError.pairing("Could not remove the Nex credential from Keychain (\(status)).")
        }
    }
}

final class CertificatePin: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let digest: Data
    let host: String
    init(digest: Data, host: String) {
        self.digest = digest
        self.host = host
    }

    func urlSession(
        _ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == host,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        SecTrustSetNetworkFetchAllowed(trust, false)
        let policy = SecPolicyCreateSSL(true, host as CFString)
        guard SecTrustSetPolicies(trust, policy) == errSecSuccess,
              let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first,
              Data(SHA256.hash(data: SecCertificateCopyData(leaf) as Data)) == digest else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        guard SecTrustSetAnchorCertificates(trust, [leaf] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
              SecTrustEvaluateWithError(trust, nil) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

final class NexAPI {
    private let accessLock = NSLock()
    private var ownerToken: String?
    private var compatible = false
    private var liveCompatible = false
    var workspaceToken: String? {
        get { accessLock.withLock { ownerToken } }
        set { accessLock.withLock { ownerToken = newValue } }
    }

    func checkPrivacyVersion(live: Bool = false) async throws {
        guard !accessLock.withLock({ live ? liveCompatible : compatible }) else { return }
        let request = try makeRequest("/v1/privacy/version", method: "GET", query: [], timeout: 5, port: live ? 8767 : nil)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response, data: data)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              (try? JSONDecoder().decode([String: Int].self, from: data))?["version"] == 1 else {
            throw NexError.pairing("Update the Excalibur privacy service before connecting Nex.")
        }
        accessLock.withLock {
            if live { liveCompatible = true } else { compatible = true }
        }
    }
    private let base: URL
    private let token: String
    let signingDevice: String
    let biometricSlot: String
    private let session: URLSession
    private let decoder: JSONDecoder

    init(_ pairing: Pairing) throws {
        base = try pairing.address()
        token = pairing.token
        signingDevice = pairing.signingDevice
        biometricSlot = pairing.biometricSlot
        session = try Self.pinnedSession(base: base, pin: pairing.pin)
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    static func pinnedSession(base: URL, pin: String) throws -> URLSession {
        guard let host = base.host, let fingerprint = Data(base64Encoded: pin) else {
            throw NexError.pairing("The Excalibur profile is invalid.")
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 220
        configuration.timeoutIntervalForResource = 235
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.connectionProxyDictionary = [:]
        return URLSession(configuration: configuration,
                          delegate: CertificatePin(digest: fingerprint, host: host), delegateQueue: nil)
    }

    deinit { session.invalidateAndCancel() }

    func cancelPending() { session.invalidateAndCancel() }

    func bytes(
        _ path: String, method: String = "GET", query: [URLQueryItem] = [],
        body: Data? = nil, headers: [String: String] = [:], timeout: Double = 12, port: Int? = nil
    ) async throws -> Data {
        try await checkPrivacyVersion()
        let request = try makeRequest(path, method: method, query: query, body: body, headers: headers,
                                      timeout: timeout, port: port)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response, data: data)
        return data
    }

    func signOut() async throws {
        let request = try makeRequest("/v1/auth/logout", method: "POST", timeout: 5)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response, data: data)
    }

    static func validate(_ response: URLResponse, data: Data) throws {
        guard let response = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200..<300).contains(response.statusCode) else {
            let failure = try? JSONDecoder().decode([String: String].self, from: data)
            throw NexError.api(response.statusCode, failure?["error"] ?? failure?["detail"] ?? "Excalibur rejected the request.")
        }
    }

    private func makeRequest(
        _ path: String, method: String, query: [URLQueryItem] = [], body: Data? = nil,
        headers: [String: String] = [:], timeout: Double, port: Int? = nil
    ) throws -> URLRequest {
        guard var url = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            throw NexError.pairing("Invalid Excalibur address.")
        }
        if let port { url.port = port }
        url.path = try protectedPath(path)
        url.queryItems = query.isEmpty ? nil : query
        guard let address = url.url else { throw NexError.pairing("Invalid Excalibur request.") }
        var request = URLRequest(url: address, timeoutInterval: timeout)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        request.setValue(workspaceToken, forHTTPHeaderField: "X-Nex-Workspace")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
        return request
    }

    // Older Excalibur releases do not serve these paths, including after a server rollback.
    private func protectedPath(_ path: String) throws -> String {
        guard path.hasPrefix("/v1/") else { throw NexError.pairing("Invalid Nex API path.") }
        return "/v2/" + path.dropFirst(4)
    }

    @MainActor
    func watchVoice(_ id: String, receiveChime: () -> Void,
                    receiveAudio: (Data) throws -> Void) async throws -> VoiceStreamEvent {
        try await checkPrivacyVersion()
        let request = try makeRequest("/v1/recordings/\(id)/voice-stream", method: "GET", timeout: 130)
        let (stream, response) = try await session.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        for try await line in stream.lines {
            let event = try decoder.decode(VoiceStreamEvent.self, from: Data(line.utf8))
            if event.kind == "complete" { return event }
            if event.kind == "chime" {
                receiveChime()
                continue
            }
            if let audio = event.audio { try receiveAudio(audio) }
        }
        throw URLError(.networkConnectionLost)
    }

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], timeout: Double = 12) async throws -> T {
        let data = try await bytes(path, query: query, timeout: timeout)
        return try decoder.decode(T.self, from: data)
    }

    func send<T: Decodable, Body: Encodable>(
        _ path: String, method: String = "POST", body: Body, timeout: Double = 12
    ) async throws -> T {
        let data = try await bytes(path, method: method, body: JSONEncoder().encode(body), timeout: timeout)
        return try decoder.decode(T.self, from: data)
    }

    func attention(_ audio: Data, language: String, context: Data = Data()) async throws -> String? {
        try await checkPrivacyVersion()
        try await checkPrivacyVersion(live: true)
        let request = try makeRequest("/v1/attention", method: "POST", body: context + audio, headers: [
            "Content-Type": "application/octet-stream", "X-Nex-Language": language,
            "X-Nex-Context-Bytes": String(context.count)
        ], timeout: 15, port: 8767)
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        struct Result: Decodable { let ticket: String? }
        return try decoder.decode(Result.self, from: data).ticket
    }

    func declineAttention(_ ticket: String) async {
        guard UUID(uuidString: ticket) != nil else { return }
        _ = try? await bytes("/v1/attention/" + ticket, method: "DELETE", timeout: 2, port: 8767)
    }

    func liveSocket(path: String = "/v1/live", language: String? = nil,
                    wakeChime: Bool = false) throws -> URLSessionWebSocketTask {
        guard accessLock.withLock({ compatible }) else {
            throw NexError.pairing("Connect to the Excalibur privacy service first.")
        }
        guard var url = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            throw NexError.pairing("Invalid Excalibur address.")
        }
        url.scheme = "wss"
        url.port = 8767
        url.path = try protectedPath(path)
        if let language { url.queryItems = [URLQueryItem(name: "language", value: language)] }
        if wakeChime { url.queryItems = (url.queryItems ?? []) + [URLQueryItem(name: "wake_chime", value: "1")] }
        guard let address = url.url else { throw NexError.pairing("Invalid live voice address.") }
        var request = URLRequest(url: address)
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        return session.webSocketTask(with: request)
    }

    func upload(_ clip: Clip, data: Data, fromWatch: Bool = false) async throws -> Recording {
        let path = fromWatch ? "/v1/watch/recordings/" : "/v1/recordings/"
        var headers = [
            "Content-Type": "audio/mp4",
            "X-Nex-Started": NexDate.wire(clip.started),
            "X-Nex-Duration": String(format: "%.3f", locale: Locale(identifier: "en_US_POSIX"),
                                    max(0.001, clip.duration ?? 0)),
            "X-Nex-Mode": clip.mode.rawValue,
            "X-Nex-Wake": clip.wake ? "1" : "0",
            "X-Nex-Language": clip.language,
            "X-Nex-Time-Zone": clip.timeZone ?? "UTC",
            "X-Nex-Live": clip.liveHandled == true ? "1" : "0"
        ]
        if fromWatch { headers["X-Nex-Watch-Conversation"] = clip.watchConversation }
        let bytes = try await bytes(path + clip.id, method: "PUT", body: data, headers: headers, timeout: 25)
        return try decoder.decode(Recording.self, from: bytes)
    }
}
