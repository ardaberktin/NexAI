import Foundation

@main
struct HostedAccountCheck {
    static func main() throws {
        let fingerprint = Data(repeating: 0, count: 32).base64EncodedString()
        let pin = String(fingerprint.dropLast())
        let address = "100.64.10.20"
        let link = try NexServerLink(" nex://\(address):8765/\(pin)\n")
        precondition(link.address.absoluteString == "https://\(address):8765")
        precondition(link.pin == fingerprint)
        for invalid in [
            "https://\(address):8765/\(pin)", "nex://example.test:8765/\(pin)",
            "nex://100.63.10.20:8765/\(pin)", "nex://someone@\(address):8765/\(pin)",
            "nex://\(address):8765/\(pin)?token=secret", "nex://\(address):8765/extra/\(pin)",
            "nex://\(address):8765/\(pin)#fragment", "nex://\(address):8767/\(pin)",
            "nex://\(address):8765/\(pin.dropLast())B",
        ] {
            precondition((try? NexServerLink(invalid)) == nil, invalid)
        }

        let oneTimeCode = String(repeating: "z", count: 43)
        let setup = try NexServerLink("nex://\(address):8765/\(pin)?setup=\(oneTimeCode)")
        precondition(setup.setup == oneTimeCode && link.setup == nil)
        precondition(NexServerLink.serverLink(address: setup.address, pin: setup.pin) == "nex://\(address):8765/\(pin)")
        for invalid in ["setup=x", "setup=\(oneTimeCode)&setup=\(oneTimeCode)", "setup=\(oneTimeCode)&secret=x"] {
            precondition((try? NexServerLink("nex://\(address):8765/\(pin)?\(invalid)")) == nil)
        }
        let migration = HostedEnrollment(username: "alice", password: "disposable test password", publicKey: Data())
        let request = try JSONSerialization.jsonObject(with: JSONEncoder().encode(migration)) as? [String: Any]
        precondition(request?["setup"] == nil && request?["device"] as? String == ServerProfile.platform)
        precondition(request?["public"] as? String == "")

        let obsolete = ["url": link.address.absoluteString, "pin": link.pin,
                        "token": String(repeating: "z", count: 48), "device": ServerProfile.platform]
        let oldData = try JSONEncoder().encode(obsolete)
        precondition((try? JSONDecoder().decode(ServerProfile.self, from: oldData)) == nil)

        let aliceID = String(repeating: "a", count: 32)
        let bobID = String(repeating: "b", count: 32)
        let alice = ServerProfile(url: link.address.absoluteString, pin: link.pin,
                                  token: String(repeating: "z", count: 43), device: ServerProfile.platform,
                                  account: HostedAccount(accountId: aliceID, username: "alice", deviceId: bobID, owner: true))
        let accountData = try JSONEncoder().encode(alice)
        let decoded = try JSONDecoder().decode(ServerProfile.self, from: accountData)
        precondition(decoded.account.username == alice.account.username)
        let scope = alice.workspaceScope
        precondition(decoded.workspaceScope == scope)
        precondition(alice.signingDevice == ServerProfile.platform + ":" + bobID)
        var rotated = alice
        rotated.account = HostedAccount(accountId: aliceID, username: "alice", deviceId: aliceID, owner: true)
        precondition(scope == rotated.workspaceScope && alice.biometricSlot != rotated.biometricSlot)
        var bob = alice
        bob.account = HostedAccount(accountId: bobID, username: "bob", deviceId: aliceID, owner: false)
        precondition(scope != bob.workspaceScope)

        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let base = try Spool(directory: directory)
        let first = try base.scoped(to: scope)
        let same = try base.scoped(to: rotated.workspaceScope)
        let second = try base.scoped(to: bob.workspaceScope)
        try first.save(Gap(id: UUID().uuidString, started: "2026-01-01T00:00:00Z", text: "Alice only"))
        let restored = try same.gaps()
        let unrelated = try second.gaps()
        let unscoped = try base.gaps()
        precondition(restored.count == 1 && unrelated.isEmpty && unscoped.isEmpty)
        precondition((try? base.scoped(to: "../../other")) == nil)
        try second.save(Gap(id: UUID().uuidString, started: "2026-01-01T00:00:00Z", text: "Bob only"))
        try base.eraseAccount(scope)
        try base.eraseAccount(scope)
        precondition(!FileManager.default.fileExists(atPath: first.directory.path))
        let bobGaps = try second.gaps()
        precondition(bobGaps.count == 1)
        precondition((try? base.eraseAccount("../../other")) == nil)

        let payload = Data("private workspace request".utf8)
        let envelope = WatchLink.envelope(payload, scope: scope)
        precondition(WatchLink.payload(envelope, scope: scope) == payload)
        precondition(WatchLink.payload(envelope, scope: bob.workspaceScope) == nil)
        precondition(WatchLink.payload(payload, scope: scope) == nil)
        precondition(WatchLink.payload(payload, scope: nil) == nil)
        print("Hosted account client checks passed")
    }
}
