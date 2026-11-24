import Darwin
import Foundation

@main
enum MacGemmaCheck {
    @MainActor
    static func main() async throws {
        if CommandLine.arguments.contains("--host") {
            if CommandLine.arguments.contains("--ignore-term") { signal(SIGTERM, SIG_IGN) }
            while true { pause() }
        }

        let id = UUID().uuidString.lowercased()
        let home = FileManager.default.temporaryDirectory.appending(path: "gemma-check-" + id)
        let label = "com.ardaberktin.nex.check." + id
        let pattern = #" --host (/private)?/tmp/nex-(mac-model-\#(id)/model|screen-\#(id)/vision)\.sock([[:space:]]|$)"#
        let relay = MacGemmaRelay(home: home, label: label, processes: MacGemmaProcesses(socketPattern: pattern))
        let worker = home.appending(path: "llama-server")
        try FileManager.default.createDirectory(at: relay.agent.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        defer {
            _ = try? MacCommand.run("/bin/launchctl", ["bootout", "gui/\(getuid())/\(label)"])
            _ = try? MacCommand.run("/bin/launchctl", ["enable", "gui/\(getuid())/\(label)"])
            try? FileManager.default.removeItem(at: home)
        }
        try FileManager.default.copyItem(at: URL(fileURLWithPath: CommandLine.arguments[0]), to: worker)
        let job: [String: Any] = ["Label": label, "RunAtLoad": true, "KeepAlive": true,
                                  "ProgramArguments": [worker.path, "--host", "/private/tmp/nex-mac-model-\(id)/model.sock"]]
        let plist = try PropertyListSerialization.data(fromPropertyList: job, format: .xml, options: 0)
        try plist.write(to: relay.agent)
        let unrelated = try start(worker, ["--host", "/tmp/independent-\(id)/model.sock"])
        defer { stop(unrelated) }
        let screen = try start(worker, ["--host", "/tmp/nex-screen-\(id)/vision.sock", "--ignore-term"])
        defer { stop(screen) }
        try await checkShutdown(relay, screen: screen, unrelated: unrelated)
        try await checkRecovery(relay, plist: plist)
        print("Gemma unload, persistent disable, screen gating, re-enable, retry, and process isolation passed.")
    }

    @MainActor
    private static func checkShutdown(_ relay: MacGemmaRelay, screen: Process, unrelated: Process) async throws {
        let control = MacGemmaControl(relay: relay)
        await control.setEnabled(true)
        try await wait { try relay.processes.running().count == 2 }
        let managed = try relay.processes.running()
        let actual = try MacGemmaProcesses().running()
        try require(managed.isSubset(of: actual) && !actual.contains(unrelated.processIdentifier),
                    "The Nex socket matcher must distinguish both Nex launchers from other model servers.")
        await control.setEnabled(false)
        try require(control.phase == .disabled && !relay.isEnabled, "Unloading failed: \(control.phase)")
        try require(try relay.processes.running().isEmpty && !screen.isRunning && unrelated.isRunning,
                    "Unloading must stop all and only the fixture's Nex models.")
        let domain = "gui/\(getuid())"
        let job = try MacCommand.run("/bin/launchctl", ["print", "\(domain)/\(relay.label)"])
        let overrides = try MacCommand.run("/bin/launchctl", ["print-disabled", domain])
        try require(job.status == 113 && overrides.output.contains("\"\(relay.label)\" => disabled"),
                    "The login service must be removed and disabled across logins.")

        let relaunched = MacGemmaControl(relay: relay)
        try await wait { relaunched.phase == .disabled }
        do {
            try await ScreenVision(relay: relay).warm()
            throw Failure("Screen vision loaded while disabled.")
        } catch NexError.capture(let message) {
            try require(message.contains("Enable Gemma"), "Screen vision must check the user's preference first.")
        }
    }

    @MainActor
    private static func checkRecovery(_ relay: MacGemmaRelay, plist: Data) async throws {
        let control = MacGemmaControl(relay: relay)
        try await wait { control.phase == .disabled }
        try Data("invalid plist".utf8).write(to: relay.agent)
        await control.setEnabled(true)
        guard case .failed(enabling: true, _) = control.phase, !relay.isEnabled else {
            throw Failure("A failed bootstrap must restore the off state and allow retry.")
        }
        try plist.write(to: relay.agent)
        await control.setEnabled(true)
        try await wait { try relay.processes.running().count == 1 }
        let first = try relay.processes.running()
        await control.setEnabled(true)
        try require(try relay.processes.running() == first && relay.isEnabled, "Repeated enable restarted a healthy model.")
        await control.setEnabled(false)
        try FileManager.default.removeItem(at: relay.agent)
        await control.setEnabled(true)
        try require(control.phase == .enabled && relay.isEnabled, "Screen-only installations must support enabling Gemma.")
    }

    private static func start(_ url: URL, _ arguments: [String]) throws -> Process {
        let process = Process()
        process.executableURL = url
        process.arguments = arguments
        try process.run()
        return process
    }

    private static func stop(_ process: Process) {
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        process.waitUntilExit()
    }

    @MainActor
    private static func wait(_ condition: () throws -> Bool) async throws {
        for _ in 0..<60 {
            if try condition() { return }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw Failure("Timed out waiting for the test relay.")
    }

    private static func require(_ result: Bool, _ message: String) throws {
        if !result { throw Failure(message) }
    }

    private struct Failure: Error, CustomStringConvertible {
        let description: String
        init(_ description: String) { self.description = description }
    }
}
