import SwiftUI

@MainActor
final class WorkspaceChat: ObservableObject {
    @Published var draft = "" {
        didSet { if draft.isEmpty { privateDraftBackup = nil } }
    }
    @Published var access = ChatAccess.automatic
    @Published private(set) var replyInsertion = 0
    @Published private(set) var messages: [WorkspaceMessage] = []
    @Published private(set) var sending = false
    @Published private(set) var loaded = false
    @Published private(set) var historyError: String?
    @Published private(set) var sendError: String?
    @Published private(set) var nextBefore: String?
    @Published private(set) var loadingOlder = false
    @Published private(set) var olderError: String?
    @Published private var sendingMessage: WorkspaceMessage?
    private var archived: [WorkspaceMessage] = []
    private var current: [WorkspaceMessage] = []
    private var pending: (text: String, draft: String, id: String, access: ChatAccess)?
    private var privateDraftBackup: String?
    private var privateDraftGeneration = UUID()
    private var cursor: String?
    private var oldestArchived: WorkspaceMessage?
    private var newestArchived: WorkspaceMessage?
    private var privateExchangeIDs: Set<String> = []

    var replyingPrivately: Bool { privateDraftBackup != nil }
    var sendingPrivately: Bool { replyingPrivately || access.requiresPrivateReply }

    func timeline(unlockedInbox: [PrivateAnswer]?) -> [WorkspaceMessage] {
        guard let unlockedInbox else { return messages }
        let timeline = WorkspaceMessage.timeline(messages, privateItems: unlockedInbox)
        guard let sendingMessage, !isPrivateEcho(sendingMessage),
              !timeline.contains(where: { $0.id == sendingMessage.id }) else { return timeline }
        return (timeline + [sendingMessage]).sorted(by: WorkspaceMessage.chronological)
    }

    func insertReply(to message: WorkspaceMessage) {
        guard message.offersReply, let text = message.copyableText else { return }
        if message.isPrivate && !replyingPrivately { privateDraftBackup = draft }
        draft = draft.isEmpty ? text + "\n\n" : draft + "\n\n" + text + "\n\n"
        replyInsertion += 1
    }

    func clearPrivateReply() {
        privateDraftGeneration = UUID()
        sendingMessage = nil
        if access.requiresPrivateReply { draft = "" }
        else if let backup = privateDraftBackup { draft = backup }
        privateDraftBackup = nil
        pending = nil
        access = .automatic
    }

    func observe(_ model: Controller) async {
        var wait = false
        var retrySeconds = 1
        while !Task.isCancelled {
            let watching = await refresh(model, wait: wait)
            if historyError != nil {
                do { try await Task.sleep(for: .seconds(retrySeconds)) } catch { return }
                retrySeconds = min(retrySeconds * 2, 8)
            } else {
                retrySeconds = 1
                // An older server answers immediately and ignores the wait parameter.
                if wait && !watching {
                    do { try await Task.sleep(for: .seconds(1)) } catch { return }
                }
            }
            wait = true
        }
    }

    func load(_ model: Controller) async {
        _ = await refresh(model, wait: false)
    }

    func loadOlder(_ model: Controller) async {
        guard let before = nextBefore, !loadingOlder else { return }
        loadingOlder = true
        olderError = nil
        defer { loadingOlder = false }
        do {
            let page = try await model.olderWorkspaceHistory(before: before)
            guard !Task.isCancelled, nextBefore == before else { return }
            oldestArchived = page.items.first ?? oldestArchived
            nextBefore = page.nextBefore
            archived.insert(contentsOf: page.items.filter { !isPrivateEcho($0) }, at: 0)
            reconcileRestored(page.items)
        } catch {
            guard !Task.isCancelled, nextBefore == before else { return }
            olderError = error.localizedDescription
        }
    }

    private func refresh(_ model: Controller, wait: Bool) async -> Bool {
        let previous = cursor
        do {
            let snapshot = try await model.workspaceHistory(cursor: previous, wait: wait)
            guard !Task.isCancelled, cursor == previous else { return snapshot.watch == true }
            if let items = snapshot.items {
                let page = try await catchUp(items, before: snapshot.nextBefore, using: model)
                guard !Task.isCancelled, cursor == previous else { return snapshot.watch == true }
                restore(page)
            }
            cursor = snapshot.cursor
            if historyError != nil { historyError = nil }
            if !loaded { loaded = true }
            return snapshot.watch == true
        } catch {
            guard !Task.isCancelled, cursor == previous else { return false }
            historyError = error.localizedDescription
            loaded = true
            return false
        }
    }

    private func catchUp(_ items: [WorkspaceMessage], before: String?, using model: Controller) async throws
        -> WorkspaceHistoryPage {
        var page = WorkspaceHistoryPage(items: items, nextBefore: before)
        guard let newestArchived else { return page }
        var pages = [items]
        // A long disconnect can create more messages than fit in the latest page.
        while let next = page.nextBefore, let first = page.items.first,
              WorkspaceMessage.chronological(newestArchived, first) {
            try Task.checkCancellation()
            page = try await model.olderWorkspaceHistory(before: next)
            pages.append(page.items)
        }
        return WorkspaceHistoryPage(items: pages.reversed().flatMap { $0 }, nextBefore: page.nextBefore)
    }

    private func restore(_ page: WorkspaceHistoryPage) {
        let first = page.items.first
        if let first, page.nextBefore != nil {
            archived.removeAll { !WorkspaceMessage.chronological($0, first) }
        } else {
            archived.removeAll()
        }
        let includesOlderBoundary = if let first, let oldestArchived {
            !WorkspaceMessage.chronological(oldestArchived, first)
        } else { true }
        if includesOlderBoundary || page.nextBefore == nil {
            oldestArchived = first
            if nextBefore != page.nextBefore { olderError = nil }
            nextBefore = page.nextBefore
        }
        newestArchived = page.items.last
        archived.append(contentsOf: page.items.filter { !isPrivateEcho($0) })
        reconcileRestored(page.items)
    }

    private func reconcileRestored(_ history: [WorkspaceMessage]) {
        let restored = Set(history.map(\.id))
        current.removeAll { restored.contains($0.id) }
        if !sending, let pending, restored.contains("a-" + pending.id) {
            clearRecoveredSend()
        }
        publish()
    }

    func reconcilePrivate(_ items: [PrivateAnswer]) {
        let ids = items.compactMap { $0.askedAt == nil ? nil : $0.id }
        guard !ids.isEmpty else { return }
        privateExchangeIDs.formUnion(ids)
        // Private text is rendered directly from the unlocked inbox. Drop device-local echoes
        // so dismissing or expiring the private request also removes it from Chat.
        current.removeAll { isPrivateEcho($0) }
        archived.removeAll { isPrivateEcho($0) }
        if !sending, let pending, privateExchangeIDs.contains(pending.id) {
            clearRecoveredSend()
        }
        publish()
    }

    private func isPrivateEcho(_ message: WorkspaceMessage) -> Bool {
        message.exchangeID.map(privateExchangeIDs.contains) ?? false
    }

    private func clearRecoveredSend() {
        if draft == pending?.draft {
            draft = ""
            access = .automatic
        }
        pending = nil
        sendError = nil
    }

    func send(_ model: Controller, web: Bool = false) async {
        let value = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sending, !value.isEmpty else { return }
        let backup = privateDraftBackup
        let generation = privateDraftGeneration
        let requestAccess: ChatAccess = replyingPrivately ? .privateReply : access
        let text = web && !replyingPrivately ? WebLookup.request(for: value) : value
        let id = pending.flatMap { $0.text == text && $0.access == requestAccess ? $0.id : nil } ?? nexID()
        pending = (text, value, id, requestAccess)
        sendError = nil
        sending = true
        var question = WorkspaceMessage(id: "q-" + id, role: .user, text: value, date: NexDate.wire(), sources: [])
        // Treat the preview as private until the server confirms where the exchange belongs.
        question.privateQuestion = true
        sendingMessage = question
        draft = ""
        defer {
            sendingMessage = nil
            sending = false
        }
        do {
            let reply = try await model.ask(text, id: id, publishReply: false, access: requestAccess)
            guard generation == privateDraftGeneration else { return }
            if !requestAccess.requiresPrivateReply && reply.engine != "private_request" {
                question.privateQuestion = false
                add(question)
                add(WorkspaceMessage(id: "a-" + id, role: .assistant, text: reply.text, date: NexDate.wire(),
                                     sources: reply.sources, language: reply.language))
            }
            pending = nil
            if draft.isEmpty { access = .automatic }
        } catch {
            guard generation == privateDraftGeneration else { return }
            if privateExchangeIDs.contains(id) || archived.contains(where: { $0.id == "a-" + id }) {
                pending = nil
                if draft.isEmpty { access = .automatic }
                return
            }
            if draft.isEmpty {
                privateDraftBackup = backup
                draft = value
            }
            sendError = error.localizedDescription
        }
    }

    func receiveVoiceMessage(_ message: WorkspaceMessage) {
        add(message)
    }

    private func add(_ message: WorkspaceMessage) {
        guard !isPrivateEcho(message), !archived.contains(where: { $0.id == message.id }) else { return }
        current.removeAll { $0.id == message.id }
        current.append(message)
        if current.count > 100 { current.removeFirst(current.count - 100) }
        publish()
    }

    private func publish() {
        var unique: [String: WorkspaceMessage] = [:]
        archived.forEach { unique[$0.id] = $0 }
        current.forEach { unique[$0.id] = $0 }
        messages = unique.values.sorted(by: WorkspaceMessage.chronological)
    }
}
