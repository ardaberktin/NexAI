import Combine
import Foundation
import WatchKit

@MainActor
final class WatchLiveConversation: ObservableObject {
    enum State { case connecting, listening, recording, waiting, preparingVoice, speaking, answered, unrecognized }

    @Published private(set) var state: State?
    @Published private(set) var reply: String?
    @Published private(set) var issue: String?

    private let input: WatchConversationInput
    private let sync: WatchSync
    private let ticketURL: URL
    private let playback = WatchSpokenReply()
    private var ticket: WatchQuestionTicket?
    private var conversation: String?
    private var microphone: Task<Void, Never>?
    var microphoneOpen: Bool { microphone != nil }
    private var monitor: Task<Void, Never>?
    private var foregrounded = true
    private var autoSpeak = false
    private var nextDelivery = Date.distantPast
    private var lastActivity = ContinuousClock.now
    private var listenAfter = ContinuousClock.now
    private var backgroundedAt: ContinuousClock.Instant?

    init(capture: WatchCapture, sync: WatchSync, spool: Spool) {
        input = WatchConversationInput(capture: capture, spool: spool)
        self.sync = sync
        ticketURL = spool.directory.appending(path: "watch-question.json")
        ticket = WatchQuestionTicket.restore(from: ticketURL)
        if let ticket {
            conversation = ticket.conversation
            reply = ticket.result?.text
            issue = ticket.result?.error
            state = ticket.result == nil ? .waiting : .answered
        }
        sync.questionReceived = { [weak self] answer in self?.receive(answer) }
        capture.liveInterrupted = { [weak self] message in self?.interrupted(message) }
    }

    func start(language: NexLanguage) {
        guard microphone == nil else { return }
        if sync.supportsQuestions != nil && !sync.enabled {
            issue = "Enable Apple Watch access in Nex settings on iPhone."
            return
        }
        if sync.supportsQuestions == false {
            issue = "Open the updated Nex app on your iPhone to enable watch voice replies."
            return
        }
        let id = conversation ?? nexID()
        conversation = id
        state = .connecting
        reply = nil
        issue = nil
        autoSpeak = true
        lastActivity = .now
        microphone = Task { [weak self] in await self?.listen(id: id, language: language) }
        sync.reportRecording()
        monitorProgress()
    }

    private func listen(id: String, language: NexLanguage) async {
        do {
            let frames = try await input.open(id: id, language: language) { [weak self] error in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if let error, conversation == id { interrupted(error) }
                    else if state == .waiting { deliver() }
                    else { sync.sync() }
                }
            }
            for try await frame in frames {
                try Task.checkCancellation()
                guard conversation == id else { return }
                if state == .connecting {
                    state = .listening
                    WKInterfaceDevice.current().play(.start)
                }
                let listening = (state == .listening || state == .recording || state == .unrecognized)
                    && ContinuousClock.now >= listenAfter
                if let event = try input.stage(frame, listening: listening) { try handle(event) }
            }
        } catch is CancellationError { }
        catch { if !Task.isCancelled, conversation == id { interrupted(error.localizedDescription) } }
    }

    func submit() {
        do {
            if let event = try input.finishQuestion() { try handle(event) }
        } catch { interrupted(error.localizedDescription) }
    }

    private func handle(_ event: WatchConversationInput.Event) throws {
        switch event {
        case .began:
            state = .recording
            reply = nil
            issue = nil
            lastActivity = .now
        case .submitted(let id):
            guard let conversation else { return }
            let next = WatchQuestionTicket(conversation: conversation, id: id, submitted: .now)
            try next.save(to: ticketURL)
            ticket = next
            state = .waiting
            autoSpeak = true
            nextDelivery = .distantPast
            WKInterfaceDevice.current().play(.click)
        }
    }

    private func deliver() {
        guard let ticket, ticket.result == nil else { return }
        nextDelivery = .now.addingTimeInterval(5)
        sync.deliverQuestion(ticket.id)
    }

    private func receive(_ answer: WatchQuestionResult) {
        guard var current = ticket, current.accept(answer) else { return }
        ticket = current
        do { try current.save(to: ticketURL) }
        catch { issue = error.localizedDescription }
        reply = answer.text
        if let error = answer.error {
            issue = error
            state = microphone == nil ? .answered : .unrecognized
            lastActivity = .now
        } else if autoSpeak, foregrounded || microphone != nil {
            hearAgain()
        } else {
            releaseMicrophone()
            state = .answered
        }
    }

    func hearAgain() {
        guard let ticket, let text = reply, !text.isEmpty else { return }
        issue = nil
        playback.speak(text, language: ticket.result?.language, sharingMicrophone: microphone != nil || input.recording) { [sync] in
            try await sync.replyAudio(ticket.id)
        }
        state = .preparingVoice
        monitorProgress()
    }

    func interrupt() {
        guard state == .speaking || state == .preparingVoice else { return }
        playback.stop()
        finishedSpeaking()
    }

    private func finishedSpeaking() {
        if playback.interruptedAudio { issue = "Playback stopped. Tap Hear again to replay the answer." }
        lastActivity = .now
        listenAfter = lastActivity.advanced(by: .milliseconds(350))
        state = microphone == nil || !foregrounded ? .answered : .listening
        if !foregrounded { releaseMicrophone() }
    }

    func background() {
        foregrounded = false
        backgroundedAt = .now
        if state == .recording { submit() }
        if state != .waiting && state != .preparingVoice && state != .speaking { leaveChat() }
    }

    func foreground() {
        foregrounded = true
        backgroundedAt = nil
        if state == .waiting {
            autoSpeak = true
            deliver()
        }
        if state != nil && state != .answered { monitorProgress() }
    }

    func leaveChat() {
        guard state != .waiting && state != .preparingVoice && state != .speaking else { return }
        if state == .recording { submit(); return }
        if let ticket, ticket.result != nil {
            releaseMicrophone()
            state = .answered
        } else { stop() }
    }

    func stop(_ reason: String? = nil) {
        conversation = nil
        releaseMicrophone()
        monitor?.cancel()
        monitor = nil
        playback.stop()
        ticket = nil
        if FileManager.default.fileExists(atPath: ticketURL.path) { try? FileManager.default.removeItem(at: ticketURL) }
        reply = nil
        issue = reason
        state = nil
        sync.sync()
    }

    private func interrupted(_ message: String) {
        if state == .waiting {
            releaseMicrophone()
            issue = message
        } else { stop(message) }
    }

    private func releaseMicrophone() {
        guard microphone != nil else { return }
        microphone?.cancel()
        microphone = nil
        input.close()
        sync.reportRecording()
    }

    private func monitorProgress() {
        guard monitor == nil else { return }
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .milliseconds(200)) } catch { return }
                guard let self else { return }
                tick()
            }
        }
    }

    private func tick() {
        if state == .waiting, Date.now >= nextDelivery { deliver() }
        if state == .preparingVoice, !playback.preparing { state = .speaking }
        if state == .speaking, !playback.audible { finishedSpeaking() }
        if let backgroundedAt, ContinuousClock.now - backgroundedAt > .seconds(60), state == .waiting {
            releaseMicrophone()
            autoSpeak = false
        }
        if microphone != nil, state != .connecting, ContinuousClock.now - input.lastFrame > .seconds(5) {
            interrupted("The watch microphone stopped.")
        } else if (state == .listening || state == .unrecognized), ContinuousClock.now - lastActivity > .seconds(45) {
            leaveChat()
        }
        if state == .answered {
            monitor?.cancel()
            monitor = nil
        }
    }
}
