import Combine
import Foundation
import UIKit
import WatchConnectivity

@MainActor
final class PhoneWatchRelay: NSObject, ObservableObject {
    static let shared = PhoneWatchRelay()

    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: WatchLink.enabled)
    @Published private(set) var syncError: String?
    private weak var model: Controller?
    private let session = WCSession.isSupported() ? WCSession.default : nil
    nonisolated private let inbox = try? PhoneWatchInbox()
    nonisolated private let binding = WatchRelayBinding()
    private var scope: String? { model?.serverProfile?.workspaceScope }
    private let workspace = PhoneWatchWorkspace()
    private let live = PhoneWatchLiveRelay()
    private let voice = PhoneWatchReplyVoice()
    private let recording = WatchRecordingReporter()
    private var privacyObserver: AnyCancellable?
    private var nextSync = Date.distantPast
    private var uploadTask: Task<Void, Never>?
    private var relayGeneration = UUID()

    private var watchQuestionUntil = Date.distantPast
    private var lastWatchReport = Date.distantPast
    var watchLiveActive: Bool { live.active || (enabled && watchQuestionUntil > .now) }

    func attach(_ model: Controller) {
        live.stop()
        self.model = model
        refreshBinding()
        privacyObserver = model.privacy.$connected.sink { [weak self] connected in
            if !connected { self?.workspace.reset() }
        }
        session?.delegate = self
        session?.activate()
    }

    func setEnabled(_ enabled: Bool) {
        self.enabled = enabled
        relayGeneration = UUID()
        voice.reset()
        refreshBinding()
        UserDefaults.standard.set(enabled, forKey: WatchLink.enabled)
        nextSync = .distantPast
        if !enabled {
            watchQuestionUntil = .distantPast
            lastWatchReport = .distantPast
            uploadTask?.cancel()
            workspace.reset()
            live.stop()
            recording.clear(using: model)
            syncError = nil
        }
        publishState()
        sync()
    }

    func sync() {
        recording.sync(using: model)
        guard enabled, uploadTask == nil, Date.now >= nextSync,
              let session, session.activationState == .activated,
              session.isPaired, session.isWatchAppInstalled,
              let model, let account = scope else { return }
        nextSync = .now.addingTimeInterval(15)
        uploadTask = Task {
            let generation = relayGeneration
            let allowance = UIApplication.shared.beginBackgroundTask(withName: "Upload Nex Watch recordings") { [weak relay = self] in
                relay?.uploadTask?.cancel()
            }
            defer {
                uploadTask = nil
                if allowance != .invalid { UIApplication.shared.endBackgroundTask(allowance) }
            }
            do {
                guard let inbox = try inbox?.scoped(to: account) else {
                    throw NexError.capture("The iPhone could not open the Watch recording queue.")
                }
                let connection = try model.connection()
                var saved: [String] = []
                defer {
                    if !saved.isEmpty, session.activationState == .activated {
                        var message: [String: Any] = [WatchLink.acknowledgements: saved]
                        message[WatchLink.accountScope] = account
                        session.transferUserInfo(message)
                    }
                }
                for clip in try inbox.clips().prefix(8) {
                    try Task.checkCancellation()
                    guard generation == relayGeneration, (try? model.connection()) === connection else { return }
                    let delivered = try await PhoneWatchTransfers.deliver(clip, inbox: inbox, session: session,
                                                                          connection: connection, voice: voice, scope: account) {
                        generation == self.relayGeneration && (try? model.connection()) === connection
                    }
                    guard delivered else {
                        nextSync = min(nextSync, .now.addingTimeInterval(2))
                        continue
                    }
                    saved.append(clip.id)
                    try inbox.remove(clip)
                }
                if syncError != nil {
                    syncError = nil
                    publishState()
                }
            } catch {
                guard generation == relayGeneration, !Task.isCancelled else { return }
                let message = error.localizedDescription
                if syncError != message {
                    syncError = message
                    publishState()
                }
            }
        }
    }

    func accountChanged() {
        recording.reset()
        refreshBinding()
        publishState()
    }

    private func refreshBinding() {
        binding.update(enabled: enabled && model?.serverProfile != nil, scope: scope)
    }

    private func publishState() {
        guard let session, session.activationState == .activated else { return }
        var context: [String: Any] = [WatchLink.enabled: enabled && model?.serverProfile != nil]
        context[WatchLink.questionDelivery] = true
        context[WatchLink.accountScope] = scope
        context[WatchLink.syncError] = syncError
        try? session.updateApplicationContext(context)
    }

    private func receiveRecording(_ report: WatchRecordingReport) {
        guard enabled, let session, session.activationState == .activated,
              session.isPaired, session.isWatchAppInstalled else { return }
        if report.observedAt >= lastWatchReport, let expiration = report.expiration(receivedAt: .now) {
            lastWatchReport = report.observedAt
            watchQuestionUntil = report.conversing ? expiration : .distantPast
        }
        recording.receive(report, using: model)
    }
}

extension PhoneWatchRelay: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                             error: Error?) {
        let context = session.receivedApplicationContext
        let account = context[WatchLink.accountScope] as? String
        let report = binding.allows(account) ? WatchRecordingReport(context) : nil
        let available = session.isPaired && session.isWatchAppInstalled
        Task { @MainActor in
            publishState()
            if state == .activated, enabled, !available {
                recording.clear(using: model)
            } else if let report, scope == account {
                receiveRecording(report)
            }
            sync()
        }
    }

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {
        Task { @MainActor in live.stop() }
    }

    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        Task { @MainActor in
            setEnabled(false)
            self.session?.activate()
        }
    }

    nonisolated func sessionWatchStateDidChange(_ session: WCSession) {
        let available = session.isPaired && session.isWatchAppInstalled
        Task { @MainActor in
            if !available {
                live.stop()
                recording.clear(using: model)
            }
            publishState()
            sync()
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        let account = applicationContext[WatchLink.accountScope] as? String
        guard binding.allows(account), let report = WatchRecordingReport(applicationContext) else { return }
        Task { @MainActor in if scope == account { receiveRecording(report) } }
    }

    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any]) {
        let account = userInfo[WatchLink.accountScope] as? String
        guard binding.allows(account), let report = WatchRecordingReport(userInfo) else { return }
        Task { @MainActor in if scope == account { receiveRecording(report) } }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        let account = message[WatchLink.accountScope] as? String
        guard binding.allows(account), let report = WatchRecordingReport(message) else { return }
        Task { @MainActor in if scope == account { receiveRecording(report) } }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                             replyHandler: @escaping ([String: Any]) -> Void) {
        let account = message[WatchLink.accountScope] as? String
        guard let account, binding.allows(account) else { replyHandler([:]); return }
        if let id = message[WatchReplyAudio.key] as? String, let offset = message["offset"] as? Int {
            Task { @MainActor in
                let generation = relayGeneration
                guard enabled, scope == account, let connection = try? model?.connection() else { replyHandler([:]); return }
                let packet = await voice.packet(id, offset: offset, connection: connection)
                let valid = generation == relayGeneration && (try? model?.connection()) === connection
                replyHandler(valid ? packet : [:])
            }
            return
        }
        if let id = message[WatchLink.questionStatus] as? String, WatchLink.isClipID(id) {
            Task { @MainActor in
                nextSync = .distantPast
                sync()
                let generation = relayGeneration
                guard enabled, scope == account, let connection = try? model?.connection(),
                      let row = try? await PhoneWatchTransfers.receipt(id, connection: connection),
                      generation == relayGeneration, (try? model?.connection()) === connection,
                      let result = WatchQuestionResult(row), let data = try? JSONEncoder().encode(result) else {
                    replyHandler([:])
                    return
                }
                if result.text != nil { voice.prepare(id, connection: connection) }
                var response: [String: Any] = [WatchLink.questionResult: data]
                response[WatchLink.accountScope] = account
                replyHandler(response)
            }
            return
        }
        guard let inbox = try? inbox?.scoped(to: account),
              let metadata = message[WatchLink.clip] as? Data, let audio = message[WatchLink.questionAudio] as? Data,
              (try? inbox.importQuestion(metadata, audio: audio)) != nil else {
            replyHandler([WatchLink.questionDelivery: false])
            return
        }
        replyHandler([WatchLink.questionDelivery: true])
        Task { @MainActor in
            nextSync = .distantPast
            sync()
        }
    }

    nonisolated func session(_ session: WCSession, didReceive file: WCSessionFile) {
        let account = file.metadata?[WatchLink.accountScope] as? String
        guard let account, binding.allows(account), let metadata = file.metadata?[WatchLink.clip] as? Data else { return }
        do {
            guard let inbox = try inbox?.scoped(to: account) else {
                throw NexError.capture("Nex could not open the iPhone's Watch recording queue.")
            }
            try inbox.importClip(metadata, from: file.fileURL)
        } catch {
            let message = error.localizedDescription
            Task { @MainActor in
                guard enabled else { return }
                syncError = message
                publishState()
            }
            return
        }
        Task { @MainActor in
            nextSync = .distantPast
            sync()
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessageData data: Data,
                             replyHandler: @escaping (Data) -> Void) {
        Task { @MainActor in
            guard let data = WatchLink.payload(data, scope: scope) else {
                replyHandler((try? JSONEncoder().encode(WatchResponse(
                    error: "Reconnect Apple Watch for this Nex account.", locked: true))) ?? Data())
                return
            }
            if data.starts(with: WatchLiveLink.prefix) {
                let update = enabled ? model.map { live.respond(to: data, using: $0) } : nil
                let response = update ?? WatchLiveUpdate(ended: true, error: "Enable Apple Watch access in Nex settings on iPhone.")
                let binary = WatchLiveLink.isBinary(data.dropFirst(WatchLiveLink.prefix.count))
                replyHandler((try? WatchLiveLink.encode(response, binary: binary)) ?? Data())
                return
            }
            let response: WatchResponse
            if enabled, let model {
                response = await workspace.respond(to: data, using: model)
            } else {
                response = WatchResponse(error: "Enable Apple Watch access in Nex settings on iPhone.", locked: true)
            }
            replyHandler((try? JSONEncoder().encode(response)) ?? Data())
        }
    }
}


private final class WatchRelayBinding: @unchecked Sendable {
    private let lock = NSLock()
    private var enabled = false
    private var scope: String?

    func update(enabled: Bool, scope: String?) {
        lock.withLock { (self.enabled, self.scope) = (enabled, scope) }
    }

    func allows(_ scope: String?) -> Bool {
        lock.withLock { enabled && scope != nil && self.scope == scope }
    }
}
