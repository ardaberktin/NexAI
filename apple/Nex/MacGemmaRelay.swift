#if os(macOS)
import Foundation

struct MacGemmaRelay: Sendable {
    static let local = Self()
    var home = FileManager.default.homeDirectoryForCurrentUser
    var label = "com.ardaberktin.nex.model-relay"
    var processes = MacGemmaProcesses()

    private var domain: String { "gui/\(getuid())" }
    private var service: String { "\(domain)/\(label)" }
    var agent: URL { home.appending(path: "Library/LaunchAgents/\(label).plist") }
    private var directory: URL { home.appending(path: "Library/Application Support/Nex/Relay") }
    var visionSocketFile: URL { directory.appending(path: "vision-socket") }
    private var disabledFile: URL { directory.appending(path: "gemma-disabled") }
    var isEnabled: Bool { !FileManager.default.fileExists(atPath: disabledFile.path) }

    func disable() throws {
        if isEnabled {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try Data().write(to: disabledFile, options: .atomic)
        }
        try launchctl(["disable", service])
        if try isRegistered() {
            do { try launchctl(["bootout", service]) }
            catch { if try isRegistered() { throw error } }
        }
        try processes.unload()
    }

    func enable() throws {
        do {
            try launchctl(["enable", service])
            let installed = FileManager.default.fileExists(atPath: agent.path)
            if installed, try !isRegistered() {
                do { try launchctl(["bootstrap", domain, agent.path]) }
                catch { if try !isRegistered() { throw error } }
            }
            if !isEnabled { try FileManager.default.removeItem(at: disabledFile) }
            if installed { try launchctl(["kickstart", service]) }
        } catch {
            // Restore the off state if the login service could not be re-enabled.
            let failure = error
            do { try disable() }
            catch { throw NexError.capture("\(failure.localizedDescription) \(error.localizedDescription)") }
            throw failure
        }
    }

    private func isRegistered() throws -> Bool {
        let result = try MacCommand.run("/bin/launchctl", ["print", service])
        if result.status == 0 { return true }
        if result.status == 113 { return false } // launchd's "Could not find service"
        throw NexError.capture("macOS couldn’t check the Nex login service. \(result.output.trimmingCharacters(in: .whitespacesAndNewlines))")
    }

    private func launchctl(_ arguments: [String]) throws {
        let result = try MacCommand.run("/bin/launchctl", arguments)
        guard result.status == 0 else {
            throw NexError.capture("macOS couldn’t update the Nex login service. \(result.output.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
    }
}
#endif
