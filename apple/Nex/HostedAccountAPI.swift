import Foundation

struct HostedUser: Decodable, Identifiable {
    let id: String
    let username: String
    let owner: Bool
    let admin: Bool
    let enabled: Bool

    var role: String { owner ? "Owner" : admin ? "Administrator" : "Member" }
}

struct HostedDevice: Decodable, Identifiable {
    let id: String
    let role: String
    let current: Bool

    var name: String { role == "mac" ? "Mac" : "iPhone" }
}

struct HostedProfile: Decodable {
    let user: HostedUser
    let devices: [HostedDevice]

    init(from decoder: Decoder) throws {
        user = try HostedUser(from: decoder)
        let values = try decoder.container(keyedBy: CodingKeys.self)
        devices = try values.decode([HostedDevice].self, forKey: .devices)
    }

    private enum CodingKeys: String, CodingKey { case devices }
}

struct HostedStorage: Decodable {
    let audioBytes: Int64
    let audioLimitBytes: Int64?

    var limitGiB: Int64? { audioLimitBytes.map { $0 / (1 << 30) } }
}

struct HostedAccountAPI {
    private let api: NexAPI
    private let root = "/v1/auth"

    init(_ api: NexAPI) { self.api = api }

    func profile() async throws -> HostedProfile { try await api.get(root + "/profile") }

    func rename(_ username: String) async throws -> HostedProfile {
        try await api.send(root + "/profile", method: "PUT", body: ["username": username])
    }

    func changePassword(current: String, password: String) async throws -> HostedProfile {
        try await api.send(root + "/profile/password", method: "PUT",
                           body: ["current_password": current, "password": password])
    }

    func revoke(_ id: String) async throws -> HostedProfile {
        try await api.send(root + "/profile/revoke-device", body: ["device_id": id])
    }

    func users() async throws -> [HostedUser] {
        let result: ListResponse<HostedUser> = try await api.get(root + "/users")
        return result.items
    }

    func create(username: String, password: String) async throws -> HostedUser {
        try await api.send(root + "/users", body: ["username": username, "password": password])
    }

    func update(_ id: String, username: String, enabled: Bool, admin: Bool) async throws -> HostedUser {
        struct Request: Encodable {
            let username: String
            let enabled: Bool
            let admin: Bool
        }
        return try await api.send(root + "/users/" + id, method: "PUT",
                                  body: Request(username: username, enabled: enabled, admin: admin))
    }

    func resetPassword(_ id: String, password: String) async throws {
        let _: [String: Bool] = try await api.send(root + "/users/" + id + "/password", method: "PUT",
                                                  body: ["password": password])
    }

    func storage(_ id: String) async throws -> HostedStorage {
        try await api.get(root + "/users/" + id + "/storage")
    }

    func setStorage(_ id: String, limitGiB: Int64?) async throws -> HostedStorage {
        try await api.send(root + "/users/" + id + "/storage", method: "PUT", body: ["audio_limit_gib": limitGiB])
    }
}

extension Pairing {
    var serverLink: String? {
        guard let address = try? address() else { return nil }
        return NexServerLink.serverLink(address: address, pin: pin)
    }
}

@MainActor
extension Controller {
    func rememberUsername(_ username: String, deviceId: String) throws {
        guard var profile = pairing, profile.account?.deviceId == deviceId,
              profile.account?.username != username else { return }
        profile.account?.username = username
        try PairingKeychain.save(profile)
        pairing = profile
    }
}
