import Foundation
import UIKit

@MainActor
final class PhoneWatchLiveSession {
    let id: String
    let connection: NexAPI
    private(set) var active = true
    private let hello: LiveConversation.Start
    private let input = AsyncStream<WatchLiveRequest>.makeStream(bufferingPolicy: .bufferingOldest(8))
    private var socket: URLSessionWebSocketTask?
    private var worker: Task<Void, Never>?
    private var lease: Task<Void, Never>?
    private var background = UIBackgroundTaskIdentifier.invalid
    private var lastExchange = ContinuousClock.now
    private var error: String?
    private let codec: WatchRelayAudioCodec?
    private var output = WatchLiveOutputQueue()
    private let receipts = WatchExchangeReceipts()
    var accepted: WatchLiveUpdate { WatchLiveUpdate(compactAudio: codec != nil, sequence: 0) }
    private let encoder = JSONEncoder()
    private let weatherLocation = LiveWeatherLocation()

    init(id: String, connection: NexAPI, language: NexLanguage, zone: TimeZone, compactAudio: Bool) {
        self.id = id
        self.connection = connection
        codec = compactAudio ? WatchRelayAudioCodec(encodingRate: 24_000, decodingRate: 16_000) : nil
        hello = LiveConversation.Start(language: language.rawValue, time_zone: zone.identifier, wake: false,
                                       local_chime: true, opening_chimed: false,
                                       clock_supported: false,
                                       attention_ticket: nil, selective_followups: false, daybook_day: nil,
                                       daybook_source: nil, daybook_focus: nil)
    }

    func start() {
        background = UIApplication.shared.beginBackgroundTask(withName: "Nex Watch Live Conversation") { [weak self] in
            self?.stop("iPhone paused the live relay. Open Nex on iPhone and try again.")
        }
        lease = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(1)) }
                catch { return }
                guard let self, active else { return }
                if ContinuousClock.now - lastExchange > .seconds(15) { stop(); return }
            }
        }
        worker = Task { [weak self] in await self?.run() }
    }

    func exchange(_ request: WatchLiveRequest) -> WatchLiveUpdate {
        lastExchange = .now
        return receipts.respond(to: request.sequence) { process(request) }
    }

    private func process(_ request: WatchLiveRequest) -> WatchLiveUpdate {
        if active, request.audio?.isEmpty == false || request.interrupt == true,
           case .dropped = input.continuation.yield(request) {
            stop("The connection to Excalibur fell behind.")
        }
        let events = output.take()
        let ended = !active && output.isEmpty
        return WatchLiveUpdate(events: events, ended: ended, error: ended ? error : nil)
    }

    func stop(_ reason: String? = nil) {
        guard active else { return }
        active = false
        error = reason
        worker?.cancel()
        worker = nil
        lease?.cancel()
        lease = nil
        weatherLocation.cancel()
        input.continuation.finish()
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        if background != .invalid {
            UIApplication.shared.endBackgroundTask(background)
            background = .invalid
        }
    }

    private func run() async {
        let timeout = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(10)) }
            catch { return }
            self?.stop("Excalibur didn’t open Live Conversation. Check the connection and try again.")
        }
        defer {
            timeout.cancel()
            stop()
        }
        do {
            try await connection.checkPrivacyVersion()
            try await connection.checkPrivacyVersion(live: true)
            try Task.checkCancellation()
            let socket = try connection.liveSocket(path: "/v1/watch/live")
            self.socket = socket
            socket.resume()
            try await socket.send(.string(String(decoding: try encoder.encode(hello), as: UTF8.self)))
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            guard case .string(let json) = try await socket.receive(),
                  let ready = try? decoder.decode(LiveConversation.Event.self, from: Data(json.utf8)),
                  ready.kind == "ready", ready.privacyVersion == 1, ready.archiveProgress == true else {
                throw NexError.capture("Update the Excalibur live service to use Ask Nex on Apple Watch.")
            }
            timeout.cancel()
            append(WatchLiveEvent(kind: .ready))
            let sender = Task { [weak self] in await self?.send(to: socket) }
            defer { sender.cancel() }
            try await receive(from: socket, decoder: decoder)
        } catch {
            if active {
                let message = (error as? NexError)?.localizedDescription
                    ?? "Live Conversation couldn’t connect. Check Excalibur and end any other live session."
                stop(message)
            }
        }
    }

    private func send(to socket: URLSessionWebSocketTask) async {
        do {
            for await request in input.stream {
                try Task.checkCancellation()
                if request.interrupt == true { try await socket.send(.string("interrupt")) }
                for frame in try request.serverFrames(using: codec) { try await socket.send(.data(frame)) }
            }
        } catch {
            if active { stop((error as? NexError)?.localizedDescription ?? "The live connection to Excalibur ended.") }
        }
    }

    private func receive(from socket: URLSessionWebSocketTask, decoder: JSONDecoder) async throws {
        while active {
            switch try await socket.receive() {
            case .data(let data):
                guard data.count.isMultiple(of: 2), data.count <= WatchLiveLink.outputBacklog else {
                    throw NexError.capture("Excalibur sent invalid reply audio.")
                }
                for offset in stride(from: 0, to: data.count, by: WatchLiveLink.outputBatch) where active {
                    let end = min(offset + WatchLiveLink.outputBatch, data.count)
                    append(WatchLiveEvent(kind: .audio, audio: data.subdata(in: offset..<end)))
                }
            case .string(let json):
                let event = try decoder.decode(LiveConversation.Event.self, from: Data(json.utf8))
                if event.kind == "idle" { return }
                if event.kind == "audio_timeout" {
                    throw NexError.capture("Excalibur stopped receiving Watch microphone audio.")
                }
                relay(event)
            @unknown default:
                continue
            }
        }
    }

    private func relay(_ event: LiveConversation.Event) {
        switch event.kind {
        case "turn":
            weatherLocation.cancel()
            append(WatchLiveEvent(kind: .turn, id: event.id, sample: event.start))
        case "turn_end":
            append(WatchLiveEvent(kind: .turnEnd, id: event.id, sample: event.end, text: preview(event.transcript)))
        case "progress": append(WatchLiveEvent(kind: .progress, sample: event.end))
        case "chime": append(WatchLiveEvent(kind: .chime, id: event.id))
        case "stop_audio":
            weatherLocation.cancel()
            output.discardAudio()
            append(WatchLiveEvent(kind: .stopAudio))
        case "reply", "background":
            append(WatchLiveEvent(kind: .reply, id: event.id, text: preview(event.reply?.voiceText),
                                  language: event.reply?.language))
        case "weather_location_request":
            if let id = event.id, let socket { weatherLocation.respond(to: id, over: socket) }
        default: break
        }
    }

    private func preview(_ text: String?) -> String? {
        text.map { String(decoding: $0.utf8.prefix(2_000), as: UTF8.self) }
    }

    private func append(_ event: WatchLiveEvent) {
        do { try output.append(event, using: codec) }
        catch { stop(error.localizedDescription) }
    }
}
