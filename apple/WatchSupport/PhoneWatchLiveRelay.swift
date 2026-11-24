import Foundation

/// Keeps live traffic separate from the durable recording inbox and workspace requests.
@MainActor
final class PhoneWatchLiveRelay {
    private var session: PhoneWatchLiveSession?
    var active: Bool { session?.active == true }

    func respond(to data: Data, using model: Controller) -> WatchLiveUpdate {
        do {
            guard data.count <= WatchLiveLink.maximumMessage else {
                throw NexError.capture("The Watch sent too much live audio.")
            }
            let request = try WatchLiveLink.decode(WatchLiveRequest.self, from: data.dropFirst(WatchLiveLink.prefix.count))
            guard WatchLink.isClipID(request.id) else {
                throw NexError.capture("The Watch live session was invalid.")
            }
            switch request.action {
            case .start:
                return try start(request, using: model)
            case .exchange:
                guard let session, session.id == request.id else {
                    return WatchLiveUpdate(ended: true, error: "The iPhone live relay ended. Tap Ask Nex to reconnect.")
                }
                if (try? model.connection()) !== session.connection {
                    stop()
                    return WatchLiveUpdate(ended: true, error: "The iPhone connection changed. Tap Ask Nex to reconnect.")
                }
                return session.exchange(request)
            case .end:
                if session?.id == request.id { stop() }
                return WatchLiveUpdate(ended: true)
            }
        } catch {
            return WatchLiveUpdate(ended: true, error: error.localizedDescription)
        }
    }

    func stop() {
        session?.stop()
        session = nil
    }

    private func start(_ request: WatchLiveRequest, using model: Controller) throws -> WatchLiveUpdate {
        if let session, session.id == request.id { return session.accepted }
        guard model.liveState == nil else {
            throw NexError.capture("End the live conversation on iPhone first.")
        }
        guard let language = request.language.flatMap(NexLanguage.init(rawValue:)),
              let zone = request.timeZone.flatMap(TimeZone.init(identifier:)) else {
            throw NexError.capture("The Watch live session settings were invalid.")
        }
        let connection = try model.connection()
        stop()
        let next = PhoneWatchLiveSession(id: request.id, connection: connection, language: language, zone: zone,
                                         compactAudio: request.compactAudio == true)
        session = next
        next.start()
        return next.accepted
    }
}
