import Foundation

/// WatchConnectivity owns the incoming URL and deletes it when the delegate returns.
struct PhoneWatchInbox: Sendable {
    let directory: URL

    init(directory location: URL? = nil) throws {
        directory = try location ?? Spool().directory.deletingLastPathComponent().appending(path: "NexWatchInbox")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700,
                                                             .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var location = directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try location.setResourceValues(values)
    }

    func scoped(to scope: String?) throws -> PhoneWatchInbox {
        guard let scope else { return self }
        guard scope.utf8.count == 64, scope.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
            throw NexError.capture("The Watch recording queue account is invalid.")
        }
        return try PhoneWatchInbox(directory: directory.appending(path: "accounts").appending(path: scope))
    }

    @discardableResult
    func importClip(_ metadata: Data, from source: URL) throws -> Clip {
        let size = try source.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        return try store(metadata, size: size) { try FileManager.default.copyItem(at: source, to: $0) }
    }

    func importQuestion(_ metadata: Data, audio: Data) throws {
        let clip = try JSONDecoder().decode(Clip.self, from: metadata)
        guard clip.watchConversation != nil, audio.count <= WatchLink.immediateAudioLimit else {
            throw NexError.capture("Invalid Watch voice question.")
        }
        _ = try store(metadata, size: audio.count) { try audio.write(to: $0) }
    }

    private func store(_ metadata: Data, size: Int, write: (URL) throws -> Void) throws -> Clip {
        let clip = try JSONDecoder().decode(Clip.self, from: metadata)
        guard WatchLink.isClipID(clip.id), let duration = clip.duration, (0.001...65).contains(duration),
              (32...WatchLink.maximumAudioBytes).contains(size),
              NexLanguage(rawValue: clip.language) != nil, !clip.wake,
              clip.liveHandled != true || clip.mode == .voice else {
            throw NexError.capture("Nex received an invalid Apple Watch recording.")
        }
        if let conversation = clip.watchConversation,
           !WatchLink.isClipID(conversation) || clip.mode != .voice || clip.liveHandled == true {
            throw NexError.capture("Invalid Watch conversation.")
        }
        let fm = FileManager.default
        let destination = directory.appending(path: clip.id, directoryHint: .isDirectory)
        if fm.fileExists(atPath: destination.path) { return clip }
        let staging = directory.appending(path: "." + nexID(), directoryHint: .isDirectory)
        try fm.createDirectory(at: staging, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: staging) }
        let audio = staging.appending(path: "audio.m4a")
        try write(audio)
        try fm.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                             ofItemAtPath: audio.path)
        try metadata.write(to: staging.appending(path: "clip.json"), options: .atomic)
        do { try fm.moveItem(at: staging, to: destination) }
        catch where fm.fileExists(atPath: destination.path) { return clip }
        return clip
    }

    func submitted(_ clip: Clip) -> Bool {
        FileManager.default.fileExists(atPath: directory.appending(path: clip.id).appending(path: "submitted").path)
    }

    func markSubmitted(_ clip: Clip) throws {
        try Data().write(to: directory.appending(path: clip.id).appending(path: "submitted"), options: .atomic)
    }

    func clips() throws -> [Clip] {
        let directories = try FileManager.default.contentsOfDirectory(at: directory,
            includingPropertiesForKeys: nil, options: .skipsHiddenFiles)
        return try directories.filter { WatchLink.isClipID($0.lastPathComponent) }.map {
            try JSONDecoder().decode(Clip.self, from: Data(contentsOf: $0.appending(path: "clip.json")))
        }.sorted {
            if ($0.watchConversation != nil) != ($1.watchConversation != nil) { return $0.watchConversation != nil }
            if $0.mode != $1.mode { return $0.mode == .voice }
            return $0.started < $1.started
        }
    }

    func audio(_ clip: Clip) throws -> Data {
        try Data(contentsOf: directory.appending(path: clip.id).appending(path: "audio.m4a"))
    }

    func remove(_ clip: Clip) throws {
        try FileManager.default.removeItem(at: directory.appending(path: clip.id))
    }
}
