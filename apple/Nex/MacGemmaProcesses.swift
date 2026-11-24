#if os(macOS)
import Darwin
import Foundation

struct MacCommand {
    let status: Int32
    let output: String

    static func run(_ path: String, _ arguments: [String]) throws -> Self {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return Self(status: process.terminationStatus, output: String(decoding: data, as: UTF8.self))
    }
}

struct MacGemmaProcesses: Sendable {
    // Both Nex launchers use private Unix sockets. Other llama-server instances may belong to LM Studio.
    var socketPattern = #" --host (/private)?/tmp/nex-(mac-model-[^/[:space:]]+/model|screen-[^/[:space:]]+/vision)\.sock([[:space:]]|$)"#

    func running() throws -> Set<pid_t> {
        let servers = try matching(["-x", "llama-server"])
        guard !servers.isEmpty else { return [] }
        return try servers.intersection(matching(["-f", socketPattern]))
    }

    func unload() throws {
        if try stop(with: SIGTERM, timeout: 6) { return }
        guard try stop(with: SIGKILL, timeout: 3) else {
            throw NexError.capture("A Nex model process is still running. Retry unloading Gemma.")
        }
    }

    private func matching(_ arguments: [String]) throws -> Set<pid_t> {
        let result = try MacCommand.run("/usr/bin/pgrep", ["-u", String(getuid())] + arguments)
        if result.status == 1 { return [] }
        guard result.status == 0 else { throw NexError.capture("macOS couldn’t check the running model processes.") }
        return Set(result.output.split(whereSeparator: \.isWhitespace).compactMap { pid_t($0) }.filter { $0 > 1 })
    }

    private func stop(with signal: Int32, timeout: TimeInterval) throws -> Bool {
        let processes = try running()
        guard !processes.isEmpty else { return true }
        for pid in processes {
            if kill(pid, signal) != 0 && errno != ESRCH {
                throw NexError.capture("macOS couldn’t stop the Nex model process.")
            }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        repeat {
            Thread.sleep(forTimeInterval: 0.1)
            if try running().isEmpty { return true }
        } while ProcessInfo.processInfo.systemUptime < deadline
        return false
    }
}
#endif
