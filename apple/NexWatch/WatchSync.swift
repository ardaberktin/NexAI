import Combine
import Foundation
import WatchConnectivity

@MainActor
final class WatchSync: NSObject, ObservableObject {
    @Published private(set) var pending = 0
    @Published private(set) var scope: String?
    @Published private(set) var enabled = false
    @Published private(set) var error: String?
    @Published private(set) var reachable = false
    var isRecording: (() -> Bool)?
    var isConversing: (() -> Bool)?
    var questionReceived: ((WatchQuestionResult) -> Void)?
    private(set) var supportsQuestions: Bool?
    private var fastQuestion: String?
    private var lastFastAttempt = Date.distantPast
    private var lastRecording: WatchRecordingReport?
    var accountChanged: ((Spool) -> Void)?
    private let spoolRoot: Spool
    private var spool: Spool
    let session = WCSession.isSupported() ? WCSession.default : nil
    private var nextSend = Date.distantPast
    private var sent: [String: Date] = [:]
    private var relayError: String?
    private var localError: String?
    private let retryKey = "watch.relayAttempts"

    init(spoolRoot: Spool, spool: Spool) {
        self.spoolRoot = spoolRoot
        self.spool = spool
        scope = Self.savedScope
        super.init()
        if let dates = UserDefaults.standard.data(forKey: retryKey) {
            sent = (try? JSONDecoder().decode([String: Date].self, from: dates)) ?? [:]
        }
        session?.delegate = self
        session?.activate()
    }

    static var savedScope: String? { UserDefaults.standard.string(forKey: WatchLink.accountScope) }

    func sync() {
        do {
            let clips = try spool.clips().filter { $0.duration != nil }
            pending = clips.count
            guard enabled, let scope, let session, session.activationState == .activated,
                  Date.now >= nextSend else { return }
            nextSend = .now.addingTimeInterval(2)
            let outstanding = session.outstandingFileTransfers
            let inTransit = Set(outstanding.map { $0.file.fileURL.deletingPathExtension().lastPathComponent })
            let retryAfter = Date.now.addingTimeInterval(-300)
            let prioritized = clips.sorted {
                if ($0.watchConversation != nil) != ($1.watchConversation != nil) { return $0.watchConversation != nil }
                if $0.mode != $1.mode { return $0.mode == .voice }
                return $0.started < $1.started
            }
            // Keep a place for a voice question even when six journal clips are already queued.
            var slots = 6 - outstanding.count
            for clip in prioritized where slots > 0 || (slots == 0 && clip.watchConversation != nil) {
                guard !inTransit.contains(clip.id), (sent[clip.id] ?? .distantPast) < retryAfter else { continue }
                let url = spool.audio(clip.id)
                guard FileManager.default.fileExists(atPath: url.path) else {
                    try spool.remove(clip)
                    continue
                }
                let data = try JSONEncoder().encode(clip)
                var metadata: [String: Any] = [WatchLink.clip: data]
                metadata[WatchLink.accountScope] = scope
                session.transferFile(url, metadata: metadata)
                sent[clip.id] = .now
                slots -= 1
            }
            let keys = Set(clips.map(\.id))
            sent = sent.filter { keys.contains($0.key) }
            saveAttempts()
            localError = nil
        } catch { localError = error.localizedDescription }
        updateError()
    }

    /// The file survives suspension. An immediate copy lets short questions start processing sooner.
    func deliverQuestion(_ id: String) {
        sync()
        guard enabled, let account = scope, let session, session.activationState == .activated else { return }
        if fastQuestion != id, Date.now.timeIntervalSince(lastFastAttempt) >= 8,
           let clip = try? spool.clips().first(where: { $0.id == id && $0.duration != nil && $0.watchConversation != nil }),
           let metadata = try? JSONEncoder().encode(clip),
           let size = try? spool.audio(id).resourceValues(forKeys: [.fileSizeKey]).fileSize,
           size <= WatchLink.immediateAudioLimit, let audio = try? Data(contentsOf: spool.audio(id)) {
            lastFastAttempt = .now
            var message: [String: Any] = [WatchLink.clip: metadata, WatchLink.questionAudio: audio]
            message[WatchLink.accountScope] = account
            session.sendMessage(message, replyHandler: { response in
                if response[WatchLink.questionDelivery] as? Bool == true {
                    Task { @MainActor in if self.scope == account { self.fastQuestion = id } }
                }
            }, errorHandler: { _ in })
        } else {
            var message: [String: Any] = [WatchLink.questionStatus: id]
            message[WatchLink.accountScope] = account
            session.sendMessage(message, replyHandler: { [weak self] response in
                _ = self?.receiveQuestion(response)
            }, errorHandler: { _ in })
        }
    }

    func deliverPendingContent() async {
        session?.activate()
        for _ in 0..<30 {
            if session?.activationState == .activated && session?.hasContentPending == false { break }
            do { try await Task.sleep(for: .milliseconds(200)) }
            catch { return }
        }
        sync()
    }

    func retryNow() {
        sent.removeAll()
        saveAttempts()
        nextSend = .distantPast
        sync()
    }

    func reportRecording() {
        guard enabled, let scope, let isRecording, let session, session.activationState == .activated else { return }
        let now = Date.now
        let recording = isRecording()
        let changed = lastRecording?.recording != recording
        let report = WatchRecordingReport(
            recording: recording, observedAt: now, conversing: isConversing?() == true,
            changedAt: changed ? now : lastRecording?.changedAt)
        lastRecording = report
        var context = report.context
        context[WatchLink.accountScope] = scope
        if changed { session.transferUserInfo(context) }
        // Keep the newest state available when the phone can't receive a message immediately.
        try? session.updateApplicationContext(context)
        if session.isReachable {
            session.sendMessage(context, replyHandler: nil, errorHandler: nil)
        }
    }

    func request(_ data: Data) async throws -> Data {
        guard enabled, let account = scope, let session, session.activationState == .activated, session.isReachable else {
            throw NexError.connection("The paired iPhone is out of reach. Open Nex on iPhone and try again.")
        }
        let wire = WatchLink.envelope(data, scope: account)
        let response: Data = try await withCheckedThrowingContinuation { continuation in
            session.sendMessageData(wire, replyHandler: { continuation.resume(returning: $0) },
                                    errorHandler: { continuation.resume(throwing: $0) })
        }
        guard enabled, scope == account else { throw CancellationError() }
        return response
    }

    private func apply(_ state: WatchRelayState) {
        guard let supported = state.supportsQuestions else { return }
        supportsQuestions = supported
        if scope != state.scope {
            enabled = false
            do { try useScope(state.scope) }
            catch {
                localError = error.localizedDescription
                updateError()
                return
            }
        }
        enabled = state.enabled && scope != nil
        relayError = state.error
        if !enabled {
            lastRecording = nil
            for transfer in session?.outstandingFileTransfers ?? [] { transfer.cancel() }
        }
        updateError()
        reportRecording()
        sync()
    }

    private func useScope(_ account: String?) throws {
        let next = try account.map { try spoolRoot.scoped(to: $0) } ?? spoolRoot
        for transfer in session?.outstandingFileTransfers ?? [] { transfer.cancel() }
        scope = account
        UserDefaults.standard.set(account, forKey: WatchLink.accountScope)
        spool = next
        lastRecording = nil
        fastQuestion = nil
        lastFastAttempt = .distantPast
        nextSend = .distantPast
        sent.removeAll()
        saveAttempts()
        accountChanged?(next)
    }

    private func acknowledge(_ ids: [String]) {
        let accepted = Set(ids.filter(WatchLink.isClipID))
        guard !accepted.isEmpty else { return }
        do {
            for clip in try spool.clips() where accepted.contains(clip.id) && clip.duration != nil {
                try spool.remove(clip)
                sent.removeValue(forKey: clip.id)
            }
            saveAttempts()
        } catch {
            localError = error.localizedDescription
            updateError()
        }
        nextSend = .distantPast
        sync()
    }

    private func saveAttempts() {
        UserDefaults.standard.set(try? JSONEncoder().encode(sent), forKey: retryKey)
    }

    private func updateError() {
        let issue = localError ?? relayError
        if error != issue { error = issue }
    }
}

extension WatchSync: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                             error: Error?) {
        let context = WatchRelayState(session.receivedApplicationContext)
        let reachable = session.isReachable
        Task { @MainActor in
            self.reachable = reachable
            apply(context)
        }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let reachable = session.isReachable
        Task { @MainActor in
            self.reachable = reachable
            if reachable { reportRecording() }
            sync()
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        let state = WatchRelayState(applicationContext)
        Task { @MainActor in apply(state) }
    }

    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        if receiveQuestion(userInfo) { return }
        let ids = userInfo[WatchLink.acknowledgements] as? [String] ?? []
        guard let account = userInfo[WatchLink.accountScope] as? String else { return }
        Task { @MainActor in if account == scope { acknowledge(ids) } }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        _ = receiveQuestion(message)
    }

    private nonisolated func receiveQuestion(_ message: [String: Any]) -> Bool {
        guard let bytes = message[WatchLink.questionResult] as? Data, bytes.count <= 64_000,
              let result = try? JSONDecoder().decode(WatchQuestionResult.self, from: bytes),
              WatchLink.isClipID(result.id) else { return false }
        guard let account = message[WatchLink.accountScope] as? String else { return false }
        Task { @MainActor in if account == scope, enabled { questionReceived?(result) } }
        return true
    }

    nonisolated func session(_ session: WCSession, didFinish fileTransfer: WCSessionFileTransfer, error: Error?) {
        let id = fileTransfer.file.fileURL.deletingPathExtension().lastPathComponent
        let issue = error?.localizedDescription
        let account = fileTransfer.file.metadata?[WatchLink.accountScope] as? String
        Task { @MainActor in
            guard account == scope else { return }
            if let issue {
                localError = issue
                sent.removeValue(forKey: id)
                saveAttempts()
                updateError()
                // The original file stays in the queue for a later retry.
            } else if sent[id] != nil {
                nextSend = .distantPast
                sync()
            }
        }
    }
}
