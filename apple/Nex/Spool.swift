import AVFoundation
import Foundation

struct Spool: Sendable {
    let directory: URL
    var fm: FileManager { .default }

    init(directory: URL? = nil) throws {
        if let directory {
            self.directory = directory
        } else {
            self.directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                        appropriateFor: nil, create: true).appending(path: "NexSpool")
        }
        try fm.createDirectory(at: self.directory, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        var location = self.directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try location.setResourceValues(values)
        #if os(iOS)
        try fm.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                             ofItemAtPath: self.directory.path)
        #endif
    }

    func scoped(to scope: String) throws -> Spool {
        guard scope.utf8.count == 64, scope.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
            throw NexError.capture("The recording queue account is invalid.")
        }
        return try Spool(directory: directory.appending(path: "accounts").appending(path: scope))
    }

    func audio(_ id: String) -> URL { directory.appending(path: id + ".m4a") }
    func pcm(_ id: String) -> URL { file(id, "pcm") }
    func encoding(_ id: String) -> URL { file(id, "encoding.m4a") }

    func save(_ clip: Clip) throws {
        try JSONEncoder().encode(clip).write(to: file(clip.id, "clip"), options: .atomic)
    }

    func save(_ gap: Gap) throws {
        try JSONEncoder().encode(gap).write(to: file(gap.id, "gap"), options: .atomic)
    }

    func gaps() throws -> [Gap] { try list("gap") }
    func clips() throws -> [Clip] { try list("clip") }

    func remove(_ clip: Clip) throws {
        // Remove the audio first. A crash after this step leaves harmless metadata for reconciliation.
        for location in [audio(clip.id), pcm(clip.id), encoding(clip.id)] {
            if fm.fileExists(atPath: location.path) { try fm.removeItem(at: location) }
        }
        try fm.removeItem(at: file(clip.id, "clip"))
    }

    func remove(_ gap: Gap) throws { try fm.removeItem(at: file(gap.id, "gap")) }

    func playableDuration(_ id: String) -> Double? {
        guard let player = try? AVAudioPlayer(contentsOf: audio(id)), player.duration >= 0.18 else { return nil }
        return min(player.duration, 65)
    }

    func verifySpace() throws {
        let disk = try fm.attributesOfFileSystem(forPath: directory.path)
        let capacity = (disk[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        let used = try fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.fileSizeKey])
            .reduce(into: Int64(0)) { count, url in
                count += Int64(try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0)
            }
        guard capacity > 512 * 1_024 * 1_024, used < 2 * 1_024 * 1_024 * 1_024 else {
            throw NexError.capture("Capture paused because the local recording queue is out of space.")
        }
    }

    private func file(_ id: String, _ suffix: String) -> URL { directory.appending(path: id + "." + suffix) }

    private func list<T: Decodable>(_ suffix: String) throws -> [T] {
        try fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == suffix }
            .map { try JSONDecoder().decode(T.self, from: Data(contentsOf: $0)) }
    }
}
