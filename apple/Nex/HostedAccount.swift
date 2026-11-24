import Foundation

struct HostedAccount: Codable {
    let accountId: String
    let username: String
    let deviceId: String
    let owner: Bool

    var valid: Bool { [accountId, deviceId].allSatisfy(Self.isIdentifier) }

    private static func isIdentifier(_ value: String) -> Bool {
        value.utf8.count == 32 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

struct NexServerLink {
    let address: URL
    let pin: String

    init(_ link: String) throws {
        let value = link.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parts = URLComponents(string: value), parts.scheme == "nex",
              let host = parts.host, Pairing.isTailscaleV4(host), parts.port == 8765,
              parts.query == nil, parts.fragment == nil, parts.user == nil, parts.password == nil else {
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
        self.address = address
        pin = standard
    }

    func signIn(username: String, password: String, publicKey: Data) async throws -> Pairing {
        struct Request: Encodable {
            let username: String
            let password: String
            let device: String
            let publicKey: Data

            enum CodingKeys: String, CodingKey {
                case username, password, device
                case publicKey = "public"
            }
        }
        struct Response: Decodable {
            let token: String
            let accountId: String
            let deviceId: String
            let username: String
            let owner: Bool
        }

        let session = try NexAPI.pinnedSession(base: address, pin: pin)
        defer { session.invalidateAndCancel() }
        var request = URLRequest(url: address.appending(path: "v2/auth/login"), timeoutInterval: 12)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(
            Request(username: username, password: password, device: Pairing.platform, publicKey: publicKey)
        )
        let (data, response) = try await session.data(for: request)
        try NexAPI.validate(response, data: data)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let result = try decoder.decode(Response.self, from: data)
        let account = HostedAccount(accountId: result.accountId, username: result.username, deviceId: result.deviceId,
                                    owner: result.owner)
        let profile = Pairing(url: address.absoluteString, pin: pin, token: result.token, device: Pairing.platform,
                              account: account)
        _ = try profile.address()
        return profile
    }
}
