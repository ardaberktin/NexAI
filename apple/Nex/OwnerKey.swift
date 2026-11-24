import CryptoKit
import Foundation
import LocalAuthentication
import Security

enum OwnerKey {
    typealias SigningKey = SecureEnclave.P256.Signing.PrivateKey

    private static let item: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "com.ardaberktin.nex.owner-key",
        kSecAttrAccount as String: "owner"
    ]

    private static func attributes(_ slot: String) -> [String: Any] {
        item.merging([kSecAttrAccount as String: slot]) { _, updated in updated }
    }

    static func load(using context: LAContext, slot: String = "owner") throws -> SigningKey {
        var query = attributes(slot)
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecSuccess, let data = result as? Data {
            return try SigningKey(dataRepresentation: data, authenticationContext: context)
        }
        guard status == errSecItemNotFound else { throw NexError.pairing("The biometric key is unavailable.") }
        var error: Unmanaged<CFError>?
        guard let control = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                                                            [.privateKeyUsage, .biometryCurrentSet], &error) else {
            throw NexError.pairing("Could not protect the signing key with biometrics.")
        }
        return try SigningKey(accessControl: control, authenticationContext: context)
    }

    static func save(_ key: SigningKey, slot: String = "owner") throws {
        let item = attributes(slot)
        var values: [String: Any] = [kSecValueData as String: key.dataRepresentation]
        #if os(iOS)
        values[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        #endif
        var status = SecItemUpdate(item as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(item.merging(values) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NexError.pairing("Could not save the biometric signing key.") }
    }

    static func discard(_ slot: String) {
        SecItemDelete(attributes(slot) as CFDictionary)
    }

    static func proof(_ api: NexAPI, key: SigningKey, purpose: String, subject: String = "") async throws -> [String: String] {
        struct Challenge: Decodable { let nonce: String; let message: String }
        let challenge: Challenge = try await api.send("/v1/privacy/challenge", body: ["purpose": purpose, "subject": subject])
        let message = ["Nex biometric approval v1", Pairing.platform, purpose, subject, challenge.nonce].joined(separator: "\n")
        guard message == challenge.message else { throw NexError.pairing("The approval request did not match.") }
        let signature = try key.signature(for: Data(message.utf8))
        return ["nonce": challenge.nonce, "signature": signature.derRepresentation.base64EncodedString()]
    }
}
