import Foundation

enum InstalledPairing {
    // Deployment delivers this file through the local account or the paired iPhone's app container.
    static var url: URL {
        #if os(macOS)
        URL.cachesDirectory.appending(path: "com.ardaberktin.nex/Nex-Pairing.json")
        #else
        URL.cachesDirectory.appending(path: "Nex-Pairing.json")
        #endif
    }

    static var available: Bool { FileManager.default.fileExists(atPath: url.path) }

    static func read() throws -> Pairing {
        let info = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard info.isRegularFile == true, info.isSymbolicLink != true else {
            throw NexError.pairing("The installed pairing profile is unavailable.")
        }
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        guard let data = try file.read(upToCount: 4096), !data.isEmpty, data.count < 4096 else {
            throw NexError.pairing("The installed pairing profile is invalid.")
        }
        let profile = try JSONDecoder().decode(Pairing.self, from: data)
        _ = try profile.address()
        guard profile.account == nil, let setup = profile.setup, setup.utf8.count == 43,
              setup.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else {
            throw NexError.pairing("Export a fresh pairing profile from Excalibur.")
        }
        return profile
    }

    static func matching(_ trusted: Pairing) -> Pairing? {
        guard available, let installed = try? read(), installed.url == trusted.url,
              installed.device == trusted.device, installed.pin == trusted.pin, installed.token == trusted.token else { return nil }
        return installed
    }

    static func finish(_ profile: Pairing) {
        guard let setup = profile.setup else { return }
        if available, let installed = try? read(), installed.setup == setup {
            // Keep the pending signing key if the delivery file still needs to be retried.
            do { try FileManager.default.removeItem(at: url) } catch { return }
        }
        OwnerKey.discard("registration-" + setup)
    }
}
