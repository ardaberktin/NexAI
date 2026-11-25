import Foundation

struct HostedAccount: Codable {
    let accountId: String
    var username: String
    let deviceId: String
    let owner: Bool

    var valid: Bool { [accountId, deviceId].allSatisfy(Self.isIdentifier) }

    private static func isIdentifier(_ value: String) -> Bool {
        value.utf8.count == 32 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

struct HostedEnrollment: Encodable {
    let username: String
    let password: String
    let publicKey: Data
    var setup: String?
    var device = Pairing.platform

    enum CodingKeys: String, CodingKey {
        case username, password, device, setup
        case publicKey = "public"
    }
}

struct HostedLogin: Decodable {
    let token: String
    let accountId: String
    let deviceId: String
    let username: String
    let owner: Bool

    func pairing(url: String, pin: String) throws -> Pairing {
        let account = HostedAccount(accountId: accountId, username: username, deviceId: deviceId,
                                    owner: owner)
        let profile = Pairing(url: url, pin: pin, token: token, device: Pairing.platform, account: account)
        _ = try profile.address()
        return profile
    }
}

struct NexServerLink {
    let address: URL
    let pin: String
    let setup: String?

    init(_ link: String) throws {
        let value = link.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parts = URLComponents(string: value), parts.scheme == "nex",
              let host = parts.host, Pairing.isTailscaleV4(host), parts.port == 8765,
              parts.fragment == nil, parts.user == nil, parts.password == nil else {
            throw NexError.pairing("Enter the Nex server link from your server administrator.")
        }
        let encoded = String(parts.percentEncodedPath.dropFirst())
        let standard = encoded.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + "="
        guard encoded.utf8.count == 43, let pinData = Data(base64Encoded: standard), pinData.count == 32,
              pinData.base64EncodedString().replacingOccurrences(of: "+", with: "-")
                .replacingOccurrences(of: "/", with: "_").dropLast() == encoded,
              let address = URL(string: "https://\(host):8765") else {
            throw NexError.pairing("The Nex server link has an invalid certificate fingerprint.")
        }
        if let query = parts.queryItems {
            guard query.count == 1, query[0].name == "setup", let secret = query[0].value,
                  secret.utf8.count == 43, secret.utf8.allSatisfy(Self.isLinkCharacter) else {
                throw NexError.pairing("Enter a valid Nex server or setup link.")
            }
            setup = secret
        } else {
            setup = nil
        }
        self.address = address
        pin = standard
    }

    static func serverLink(address: URL, pin: String) -> String? {
        guard let host = address.host else { return nil }
        let fingerprint = pin.replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return "nex://\(host):8765/\(fingerprint)"
    }

    private static func isLinkCharacter(_ value: UInt8) -> Bool {
        (48...57).contains(value) || (65...90).contains(value) || (97...122).contains(value) || value == 45 || value == 95
    }

    func signIn(username: String, password: String, publicKey: Data) async throws -> Pairing {
        let session = try NexAPI.pinnedSession(base: address, pin: pin)
        defer { session.invalidateAndCancel() }
        let path = setup == nil ? "v2/auth/login" : "v2/auth/setup"
        var request = URLRequest(url: address.appending(path: path), timeoutInterval: 12)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            HostedEnrollment(username: username, password: password, publicKey: publicKey, setup: setup)
        )
        let (data, response) = try await session.data(for: request)
        try NexAPI.validate(response, data: data)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(HostedLogin.self, from: data).pairing(url: address.absoluteString, pin: pin)
    }
}
