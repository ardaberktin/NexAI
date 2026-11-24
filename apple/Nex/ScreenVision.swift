#if os(macOS)
import Foundation

@MainActor
final class ScreenVision {
    private let relay: MacGemmaRelay
    private var model: Process?
    private var socket: URL?
    private var loading: Task<Void, Error>?

    init(relay: MacGemmaRelay = .local) {
        self.relay = relay
    }

    func warm() async throws {
        _ = try await readySocket()
    }

    func stop() {
        loading?.cancel()
        loading = nil
        if let model, model.isRunning { model.terminate() }
        model = nil
        socket = nil
    }

    func readySocket() async throws -> URL {
        try checkEnabled()
        if let relay = relaySocket(),
           (try? await ScreenVisionHTTP.request(socket: relay, route: "/health", timeout: 1)) != nil {
            try checkEnabled()
            stop()
            return relay
        }
        try checkEnabled()
        if model?.isRunning != true {
            stop()
            try launch()
        }
        guard let model, let socket, let loading else { throw NexError.capture("The vision model could not start.") }
        do {
            try await loading.value
        } catch {
            if self.model === model { stop() }
            throw error
        }
        try checkEnabled()
        guard self.model === model, model.isRunning else { throw CancellationError() }
        return socket
    }

    private func checkEnabled() throws {
        try Task.checkCancellation()
        guard relay.isEnabled else { throw NexError.capture("Enable Gemma in Settings to use screen sharing.") }
    }

    private func relaySocket() -> URL? {
        guard let value = try? String(contentsOf: relay.visionSocketFile, encoding: .utf8) else { return nil }
        let path = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard path.hasPrefix("/private/tmp/nex-mac-model-"), path.hasSuffix("/model.sock"),
              FileManager.default.fileExists(atPath: path) else { return nil }
        return URL(fileURLWithPath: path)
    }

    private func launch() throws {
        let fm = FileManager.default
        let studio = relay.home.appending(path: ".lmstudio")
        let backends = (try? fm.contentsOfDirectory(at: studio.appending(path: "extensions/backends"),
                                                    includingPropertiesForKeys: nil)) ?? []
        let runtime = backends.filter { $0.lastPathComponent.hasPrefix("llama.cpp-mac-arm64-apple-metal-") }
            .sorted { $0.lastPathComponent.compare($1.lastPathComponent, options: .numeric) == .orderedDescending }
            .map { $0.appending(path: "llama-server") }.first(where: { fm.isExecutableFile(atPath: $0.path) })
        let folder = studio.appending(path: "models/lmstudio-community/gemma-4-12B-it-GGUF")
        let weights = folder.appending(path: "gemma-4-12B-it-Q4_K_M.gguf")
        let projector = folder.appending(path: "mmproj-gemma-4-12B-it-BF16.gguf")
        guard let runtime, fm.isReadableFile(atPath: weights.path), fm.isReadableFile(atPath: projector.path) else {
            throw NexError.capture("Screen sharing needs the local Gemma 4 model and its vision projector in LM Studio.")
        }
        let directory = URL(fileURLWithPath: "/tmp/nex-screen-" + nexID(), isDirectory: true)
        try fm.createDirectory(at: directory, withIntermediateDirectories: false,
                               attributes: [.posixPermissions: 0o700])
        let socket = directory.appending(path: "vision.sock")
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/sandbox-exec")
        process.arguments = ["-p", """
            (version 1) (allow default)
            (deny network-outbound (remote ip)) (deny network-inbound (local ip)) (deny network-bind (local ip))
            """, runtime.path, "--offline", "--no-webui", "--no-slots", "--host", socket.path, "-m", weights.path,
            "--mmproj", projector.path, "-c", "4096", "--parallel", "1", "-ngl", "99", "-t", "3",
            "--reasoning", "off"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { _ in try? FileManager.default.removeItem(at: directory) }
        do {
            try process.run()
        } catch {
            try? fm.removeItem(at: directory)
            throw NexError.capture("Nex couldn’t launch the on-device vision model.")
        }
        model = process
        self.socket = socket
        loading = Task {
            for _ in 0..<75 {
                try Task.checkCancellation()
                guard process.isRunning else { break }
                if fm.fileExists(atPath: socket.path),
                   (try? await ScreenVisionHTTP.request(socket: socket, route: "/health", timeout: 1)) != nil {
                    return
                }
                try await Task.sleep(for: .milliseconds(360))
            }
            throw NexError.capture("The on-device vision model did not finish starting.")
        }
    }

}

enum ScreenVisionHTTP {
    static func request(socket: URL, route: String, body: Data? = nil, timeout: Int) async throws -> Data {
        let request = Command()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    continuation.resume(with: Result {
                        try perform(socket: socket, route: route, body: body, timeout: timeout, request: request)
                    })
                }
            }
        } onCancel: {
            request.cancel()
        }
    }

    private static func perform(socket: URL, route: String, body: Data?, timeout: Int,
                                request: Command) throws -> Data {
        let process = Process()
        let output = Pipe()
        let input = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        process.arguments = ["-q", "--fail", "--silent", "--noproxy", "*", "--unix-socket", socket.path,
                             "--max-time", String(timeout), "http://localhost" + route]
        if body != nil {
            process.arguments? += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        }
        process.standardInput = body == nil ? FileHandle.nullDevice : input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try request.start(process)
        if let body {
            defer { try? input.fileHandleForWriting.close() }
            try input.fileHandleForWriting.write(contentsOf: body)
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        try request.checkCancellation()
        guard process.terminationStatus == 0 else {
            throw NexError.capture("The on-device vision model couldn’t answer. Try again in a moment.")
        }
        return data
    }

    private final class Command: @unchecked Sendable {
        private let lock = NSLock()
        private var process: Process?
        private var cancelled = false

        func start(_ process: Process) throws {
            try lock.withLock {
                guard !cancelled else { throw CancellationError() }
                try process.run()
                self.process = process
            }
        }

        func cancel() {
            lock.withLock {
                cancelled = true
                if let process, process.isRunning { process.terminate() }
            }
        }

        func checkCancellation() throws {
            try lock.withLock {
                if cancelled { throw CancellationError() }
            }
        }
    }
}
#endif
