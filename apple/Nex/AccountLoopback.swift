#if os(macOS)
import Foundation
import Network

@MainActor
final class AccountLoopback {
    enum Event {
        case ready(UInt16)
        case callback(URL)
    }

    let events: AsyncThrowingStream<Event, Error>
    private let continuation: AsyncThrowingStream<Event, Error>.Continuation
    private let listener: NWListener
    private var expectedState: String?
    private var port: UInt16?
    private var timeout: Task<Void, Never>?

    init() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters)
        (events, continuation) = AsyncThrowingStream.makeStream()
        listener.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in self?.handle(state) }
        }
        listener.newConnectionHandler = { [weak self] connection in
            Task { @MainActor in self?.begin(connection) }
        }
        listener.start(queue: .main)
        timeout = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(600)) }
            catch { return }
            self?.continuation.finish(throwing: AccountSignInError.expired)
        }
    }

    func expect(state: String) { expectedState = state }

    func close() {
        timeout?.cancel()
        listener.cancel()
        continuation.finish()
    }

    private func handle(_ state: NWListener.State) {
        switch state {
        case .ready:
            guard let port = listener.port?.rawValue else {
                continuation.finish(throwing: AccountSignInError.localCallback)
                return
            }
            self.port = port
            continuation.yield(.ready(port))
        case .failed(let error):
            continuation.finish(throwing: error)
        default:
            break
        }
    }

    private func begin(_ connection: NWConnection) {
        connection.start(queue: .main)
        receive(connection, buffer: Data())
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { connection.cancel() }
    }

    private func receive(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, ended, error in
            Task { @MainActor in
                guard let self, error == nil else { connection.cancel(); return }
                let combined = buffer + (data ?? Data())
                if combined.count <= 16_384,
                   let request = String(data: combined, encoding: .utf8), request.contains("\r\n\r\n") {
                    self.reply(to: request, connection: connection)
                } else if !ended && combined.count < 16_384 {
                    self.receive(connection, buffer: combined)
                } else {
                    connection.cancel()
                }
            }
        }
    }

    private func callback(in request: String, port: UInt16, state: String) -> URL? {
        let line = request.prefix { !$0.isNewline }.split(separator: " ")
        guard line.count == 3, line[0] == "GET", line[1].hasPrefix("/callback?"),
              let url = URL(string: "http://127.0.0.1:\(port)" + line[1]),
              let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              query.filter({ $0.name == "state" }).map(\.value) == [state] else { return nil }
        return url
    }

    private func reply(to request: String, connection: NWConnection) {
        let url = port.flatMap { port in expectedState.flatMap { callback(in: request, port: port, state: $0) } }
        let status = url == nil ? "400 Bad Request" : "200 OK"
        let body = url == nil ? "This sign-in request has expired or is invalid." : "Return to Nex to finish connecting Google."
        let response = "HTTP/1.1 \(status)\r\nContent-Type: text/plain; charset=utf-8\r\n" +
            "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n" +
            "Content-Length: \(body.utf8.count)\r\n\r\n\(body)"
        connection.send(content: Data(response.utf8), completion: .contentProcessed { _ in connection.cancel() })
        if let url {
            expectedState = nil
            continuation.yield(.callback(url))
            continuation.finish()
        }
    }
}
#endif
