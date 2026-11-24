import AVFoundation
import Foundation
import OSLog

@MainActor
final class LiveConversation {
    enum State {
        case connecting, listening, thinking, speaking, unrecognized
        var label: String {
            switch self {
            case .connecting: "Connecting to Live Conversation"
            case .listening: "Live Conversation is listening"
            case .thinking: "Nex is thinking"
            case .speaking: "Nex is speaking"
            case .unrecognized: "I couldn’t hear that clearly. Try again."
            }
        }
    }

    struct Event: Decodable {
        let kind: String
        let id: String?
        let start: Int?
        let end: Int?
        let reply: Reply?
        let transcript: String?
        let archiveProgress: Bool?
        let privacyVersion: Int?
        let speaker: String?
    }

    struct Start: Encodable {
        let language: String
        let time_zone: String
        let wake: Bool
        let local_chime: Bool
        let opening_chimed: Bool
        let archive_progress = true
        var weather_location_supported = true
        var clock_supported = true
        let attention_ticket: String?
        let selective_followups: Bool
        let daybook_day: String?
        let daybook_source: String?
        let daybook_focus: String?
    }

    private struct ClockResult: Encodable {
        let kind = "clock_result"
        let id: String
        let device_clock = DeviceClockReading.current
    }

    private let spool: Spool
    private let weatherLocation = LiveWeatherLocation()
    private let chime: QuestionChime
    private var openingChimed: Bool
    private var awaitingTurnEnd = false
    private let log = Logger(subsystem: "com.ardaberktin.nex", category: "LiveConversation")
    private var audio: LiveAudio?
    private var socket: URLSessionWebSocketTask?
    private var input: AsyncThrowingStream<Data, Error>.Continuation?
    private var pump: Task<Void, Never>?
    private var archive: LiveArchive?
    private var ended = false
    private var failure: Error?
    private var routeObserver: NSObjectProtocol?
    private var routeRecovery: Task<Void, Never>?
    private var inputCheck: Task<Void, Never>?
    private var inputGeneration = 0
    private var consecutiveRouteChanges = 0
    private var receivedMicAudio = false
    private var automaticSession = false
    private var endAfterReply = false
    private var currentTurnID: String?
    private var playbackFinished: Task<Void, Never>?
    private(set) var hasTurn = false
    var onState: ((State) -> Void)?
    var onMicrophoneChanged: ((Bool) -> Void)?
    var onReply: ((String, Reply) -> Void)?
    var onTurnStarted: (() -> Void)?
    var onTranscript: ((String, String) -> Void)?
    var onSpeaker: ((Bool) -> Void)?
    var onArchive: (@Sendable (String?) -> Void)?

    init(spool: Spool, chime: QuestionChime, openingChimed: Bool) {
        self.spool = spool
        self.chime = chime
        self.openingChimed = openingChimed
    }

    func run(socket: URLSessionWebSocketTask, language: String, wake: Bool, context: DaybookContext?,
             opening: AutomaticOpening? = nil,
             openMicrophone: () async throws -> (ambient: Bool, seed: LiveSeed?)) async throws {
        try Task.checkCancellation()
        guard !ended else { return }
        self.socket = socket
        automaticSession = opening != nil
        endAfterReply = opening?.namedOverMedia == true
        socket.resume()
        let timeout = Task {
            try? await Task.sleep(for: .seconds(5))
            guard !Task.isCancelled else { return }
            socket.cancel(with: .goingAway, reason: nil)
        }
        defer {
            timeout.cancel()
            stop()
        }
        let hello = Start(language: language, time_zone: context?.timeZone ?? NexDate.timeZone,
                          wake: wake, local_chime: chime.available, opening_chimed: openingChimed,
                          attention_ticket: opening?.ticket, selective_followups: opening?.selective == true,
                          daybook_day: context?.day, daybook_source: context?.source,
                          daybook_focus: context?.focus)
        try await socket.send(.string(String(decoding: try JSONEncoder().encode(hello), as: UTF8.self)))
        guard case .string(let welcome) = try await socket.receive() else {
            throw NexError.capture("Live Conversation is unavailable.")
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let ready = try decoder.decode(Event.self, from: Data(welcome.utf8))
        guard ready.kind == "ready", ready.archiveProgress == true, ready.privacyVersion == 1 else {
            throw NexError.capture("Live Conversation is unavailable.")
        }
        timeout.cancel()

        try Task.checkCancellation()
        guard !ended else { return }
        let (ambient, seed) = try await openMicrophone()
        guard !ended else { return }
        let seedAudio = seed?.pcm() ?? Data()
        archive = LiveArchive(spool: spool, language: language, seed: seedAudio, ambient: ambient,
                              completed: onArchive ?? { _ in })
        let microphone = AsyncThrowingStream<Data, Error>.makeStream(bufferingPolicy: .bufferingOldest(80))
        let network = AsyncThrowingStream<Data, Error>.makeStream(bufferingPolicy: .bufferingOldest(80))
        input = microphone.continuation
        try await startAudio()
        pump = Task { await self.stageAudio(microphone.stream, to: network.continuation) }
        let sender = Task { await sendAudio(network.stream, seed: seedAudio, socket: socket) }
        let mediaMonitor = automaticSession && !endAfterReply ? Task { await closeForOtherAudio() } : nil
        defer {
            sender.cancel()
            mediaMonitor?.cancel()
        }
        try await receiveEvents(socket)
    }

    private func closeForOtherAudio() async {
        while !Task.isCancelled && !ended {
            do { try await Task.sleep(for: .seconds(1)) }
            catch { return }
            if OtherAudio.isPlaying {
                stop()
                return
            }
        }
    }

    private func stageAudio(_ stream: AsyncThrowingStream<Data, Error>,
                            to network: AsyncThrowingStream<Data, Error>.Continuation) async {
        defer { network.finish() }
        do {
            for try await frame in stream {
                try archive?.append(frame)
                if case .dropped = network.yield(frame) {
                    throw NexError.capture("The live connection fell behind. Recording is continuing locally.")
                }
            }
        } catch {
            if !ended && !Task.isCancelled { fail(error) }
        }
    }

    private func sendAudio(_ stream: AsyncThrowingStream<Data, Error>, seed: Data,
                           socket: URLSessionWebSocketTask) async {
        do {
            if !seed.isEmpty { try await forward(seed, socket: socket) }
            for try await frame in stream {
                receivedMicAudio = true
                try await forward(frame, socket: socket)
                consecutiveRouteChanges = 0
            }
        } catch {
            if !ended && !Task.isCancelled { fail(error) }
        }
    }

    private func forward(_ data: Data, socket: URLSessionWebSocketTask) async throws {
        for offset in stride(from: 0, to: data.count, by: 4_096) {
            let frame = data.subdata(in: offset..<min(data.count, offset + 4_096))
            try await socket.send(.data(frame))
        }
    }

    private func receiveEvents(_ socket: URLSessionWebSocketTask) async throws {
        do {
            while !ended {
                switch try await socket.receive() {
                case .data(let data):
                    // Earlier servers send their pickup as audio between turn and turn_end.
                    if awaitingTurnEnd && (openingChimed || chime.play()) { continue }
                    onState?(.speaking)
                    audio?.play(data)
                case .string(let json):
                    try await receive(Data(json.utf8), from: socket)
                @unknown default:
                    break
                }
            }
        } catch {
            if let failure { throw failure }
            if !ended { throw error }
        }
        if let failure { throw failure }
    }

    private func receive(_ data: Data, from socket: URLSessionWebSocketTask) async throws {
        let event = try JSONDecoder().decode(Event.self, from: data)
        switch event.kind {
        case "turn":
            weatherLocation.cancel()
            playbackFinished?.cancel()
            awaitingTurnEnd = true
            hasTurn = true
            currentTurnID = event.id
            onTurnStarted?()
            onState?(.listening)
            if let id = event.id, let start = event.start { archive?.start(id: id, sample: start) }
        case "chime":
            if event.id == currentTurnID, !openingChimed { chime.play() }
        case "turn_end":
            awaitingTurnEnd = false
            if let id = event.id, let transcript = event.transcript, !transcript.isEmpty {
                openingChimed = false
                onTranscript?(id, transcript)
            }
            onState?(event.transcript == "" ? .unrecognized : .thinking)
            if let id = event.id, let end = event.end { archive?.finish(id: id, sample: end) }
        case "speaker":
            if event.id == currentTurnID { onSpeaker?(event.speaker == "recognized") }
        case "weather_location_request":
            if let id = event.id, id == currentTurnID {
                weatherLocation.respond(to: id, over: socket)
            }
        case "clock_request":
            if let id = event.id, id == currentTurnID {
                let payload = try JSONEncoder().encode(ClockResult(id: id))
                try await socket.send(.string(String(decoding: payload, as: UTF8.self)))
            }
        case "not_addressed":
            onTurnStarted?()
            onState?(.listening)
        case "progress":
            if let end = event.end { archive?.progress(to: end) }
        case "idle":
            stop()
        case "audio_timeout":
            fail(NexError.capture("Excalibur stopped receiving microphone audio."))
        case "stop_audio":
            weatherLocation.cancel()
            playbackFinished?.cancel()
            audio?.interrupt()
            onState?(.listening)
        case "reply", "background":
            guard let id = event.id, id == currentTurnID else { return }
            if let reply = event.reply { onReply?(id, reply) }
            showListeningAfterPlayback()
        default:
            break
        }
    }

    private func showListeningAfterPlayback() {
        playbackFinished?.cancel()
        playbackFinished = Task { [weak self] in
            while self?.audio?.isAudible == true {
                do { try await Task.sleep(for: .milliseconds(80)) }
                catch { return }
            }
            guard !Task.isCancelled, let self, !ended else { return }
            if endAfterReply { stop(); return }
            onState?(.listening)
        }
    }

    private func startAudio() async throws {
        guard let input else { return }
        let next = LiveAudio()
        audio = next
        receivedMicAudio = false
        inputGeneration += 1
        let generation = inputGeneration
        do {
            #if os(macOS)
            try await next.start(output: input)
            #else
            try next.start(output: input, mixPlayback: automaticSession)
            #endif
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            let detail = error as NSError
            log.error("Couldn’t open the live microphone: \(detail.domain, privacy: .public) (\(detail.code, privacy: .public))")
            throw (error as? NexError) ?? NexError.capture("Nex couldn’t open the microphone for live voice.")
        }
        try Task.checkCancellation()
        guard !ended, audio === next else { throw CancellationError() }
        onMicrophoneChanged?(true)
        routeObserver = NotificationCenter.default.addObserver(
            forName: next.routeNotification, object: next.routeObject, queue: nil
        ) { [weak self] note in
            #if os(iOS)
            let reason = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            #else
            let reason: UInt? = nil
            #endif
            Task { @MainActor [weak self] in
                guard let self, self.inputGeneration == generation,
                      self.audio?.needsRecovery(reason: reason) == true else { return }
                self.recoverAudio()
            }
        }
        chime.prepare()
        onState?(.listening)
        inputCheck = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self, !self.ended, !self.receivedMicAudio else { return }
            self.fail(NexError.capture("Nex couldn’t receive audio from the microphone."))
        }
    }

    private func recoverAudio() {
        guard !ended, routeRecovery == nil else { return }
        guard consecutiveRouteChanges < 2 else {
            fail(NexError.capture("The audio route kept changing."))
            return
        }
        consecutiveRouteChanges += 1
        stopAudio()
        log.info("Reopening the audio engine after an audio route change")
        routeRecovery = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(220))
            guard !Task.isCancelled, let self, !self.ended else { return }
            self.routeRecovery = nil
            do { try await self.startAudio() }
            catch is CancellationError { return }
            catch { self.fail(NexError.capture("Nex couldn’t reopen the microphone after the audio route changed.")) }
        }
    }

    private func stopAudio() {
        if let routeObserver { NotificationCenter.default.removeObserver(routeObserver) }
        routeObserver = nil
        inputCheck?.cancel()
        inputCheck = nil
        audio?.stop()
        audio = nil
        onMicrophoneChanged?(false)
    }

    private func fail(_ error: Error) {
        let detail = error as NSError
        log.error("Live voice closed: \(detail.domain, privacy: .public) (\(detail.code, privacy: .public))")
        failure = error
        stop()
    }

    func stop() {
        guard !ended else { return }
        ended = true
        weatherLocation.cancel()
        playbackFinished?.cancel()
        routeRecovery?.cancel()
        routeRecovery = nil
        stopAudio()
        input?.finish()
        pump?.cancel()
        pump = nil
        input = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        archive?.close()
        socket = nil
        archive = nil
    }
}
