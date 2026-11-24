import Foundation

typealias StandbyAudioHandler = @Sendable (Data) -> Void

/// A recording device can stay connected without taking the interactive microphone slot.
@MainActor
final class ContinuousVoice {
    private struct Decision: Decodable {
        let kind: String
        let ticket: String?
        let named: Bool?
        let wake_token: String?
        let privacy_version: Int?
    }

    private var socket: URLSessionWebSocketTask?
    private var frames: AsyncStream<Data>.Continuation?
    private var task: Task<Void, Never>?
    private var retry = Date.distantPast
    private var delay = 1.0
    private(set) var ready = false

    typealias Accepted = (String, Bool, Bool) async -> Void

    func send(_ frame: Data, connect: () throws -> URLSessionWebSocketTask,
              onWake: @escaping () -> Bool = { false }, accepted: @escaping Accepted) {
        if socket == nil {
            guard Date.now >= retry, let next = try? connect() else { return }
            let stream = AsyncStream<Data>.makeStream(bufferingPolicy: .bufferingOldest(40))
            socket = next
            frames = stream.continuation
            next.resume()
            task = Task { await listen(next, input: stream.stream, onWake: onWake, accepted: accepted) }
        }
        if case .dropped = frames?.yield(frame) { disconnect(retryLater: true) }
    }

    private func listen(_ connection: URLSessionWebSocketTask, input: AsyncStream<Data>,
                        onWake: () -> Bool, accepted: @escaping Accepted) async {
        do {
            let welcome = try await event(connection)
            guard welcome.kind == "ready", welcome.privacy_version == 1, socket === connection else {
                throw URLError(.badServerResponse)
            }
            ready = true
            delay = 1
            let send = Task {
                do {
                    for await frame in input {
                        for start in stride(from: 0, to: frame.count, by: 4096) {
                            try await connection.send(.data(frame.subdata(in: start..<min(start + 4096, frame.count))))
                        }
                    }
                } catch { connection.cancel(with: .goingAway, reason: nil) }
            }
            defer { send.cancel() }
            var chimedWake: String?
            while socket === connection {
                let decision = try await event(connection)
                guard socket === connection else { return }
                if decision.kind == "wake_chime", let token = decision.wake_token {
                    if token != chimedWake && onWake() { chimedWake = token }
                } else if let ticket = decision.ticket {
                    disconnect(retryLater: false, cancelTask: false)
                    await accepted(ticket, decision.named == true, ticket == chimedWake)
                    return
                }
            }
        } catch {
            if socket === connection { disconnect(retryLater: true, cancelTask: false) }
        }
    }

    private func event(_ connection: URLSessionWebSocketTask) async throws -> Decision {
        guard case .string(let text) = try await connection.receive() else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(Decision.self, from: Data(text.utf8))
    }

    func stop() { disconnect(retryLater: false) }

    private func disconnect(retryLater: Bool, cancelTask: Bool = true) {
        if cancelTask { task?.cancel() }
        task = nil
        frames?.finish()
        frames = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        ready = false
        retry = retryLater ? Date.now.addingTimeInterval(delay) : .distantPast
        delay = retryLater ? min(30, delay * 2) : 1
    }
}
