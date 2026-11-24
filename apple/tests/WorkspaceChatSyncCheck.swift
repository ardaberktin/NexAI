import Foundation

// Stand-in for HTTP calls used by the shared SwiftUI chat model.
@MainActor
final class Controller {
    var snapshot = WorkspaceHistory(items: [], cursor: "empty", watch: true)
    var pages: [String: WorkspaceHistoryPage] = [:]
    var answer: (String) async throws -> Reply = { _ in throw URLError(.notConnectedToInternet) }
    var requestedPrivateAnswer = false

    func workspaceHistory(cursor: String?, wait: Bool) async throws -> WorkspaceHistory { snapshot }

    func olderWorkspaceHistory(before: String) async throws -> WorkspaceHistoryPage {
        guard let page = pages[before] else { throw URLError(.notConnectedToInternet) }
        return page
    }

    func ask(_ question: String, id: String, publishReply: Bool, privately: Bool) async throws -> Reply {
        requestedPrivateAnswer = privately
        return try await answer(id)
    }
}

@main
enum WorkspaceChatSyncCheck {
    static let reply = Reply(text: "Ready.", language: "en", sources: [], engine: "local_model")

    static func pair(_ id: String) -> [WorkspaceMessage] {
        let date = "2026-11-21T12:00:00.000+00:00"
        return [WorkspaceMessage(id: "q-" + id, role: .user, text: "Ready?", date: date, sources: []),
                WorkspaceMessage(id: "a-" + id, role: .assistant, text: reply.text, date: date, sources: [])]
    }

    @MainActor
    static func main() async {
        for loseResponse in [false, true] {
            let model = Controller()
            let chat = WorkspaceChat()
            model.answer = { id in
                let rows = pair(id)
                model.snapshot = WorkspaceHistory(items: rows, cursor: "complete", watch: true)
                await chat.load(model)
                if loseResponse { throw URLError(.networkConnectionLost) }
                return reply
            }
            chat.draft = "Ready?"
            await chat.send(model)
            precondition(!model.requestedPrivateAnswer)
            precondition(chat.messages.map(\.text) == ["Ready?", "Ready."])
            precondition(chat.messages[0].date == chat.messages[1].date)
            precondition(chat.sendError == nil && chat.draft.isEmpty)
            chat.receiveVoiceMessage(chat.messages[1])
            precondition(chat.messages.count == 2)
        }

        let model = Controller()
        let chat = WorkspaceChat()
        var requestID = ""
        model.answer = { id in
            requestID = id
            throw URLError(.networkConnectionLost)
        }
        chat.draft = "Ready?"
        await chat.send(model)
        precondition(chat.sendError != nil && chat.draft == "Ready?")
        chat.draft = "A new draft"
        model.snapshot = WorkspaceHistory(items: pair(requestID), cursor: "recovered", watch: true)
        await chat.load(model)
        precondition(chat.messages.count == 2 && chat.sendError == nil && chat.draft == "A new draft")
        model.answer = { _ in reply }
        await chat.send(model, privately: true)
        precondition(model.requestedPrivateAnswer && chat.sendError == nil)
        checkPrivateTimeline()
        await checkHistoryPages()
        print("Chat reconciled paginated history, private approvals and dismissals, and a lost send response.")
    }

    @MainActor
    static func checkHistoryPages() async {
        let model = Controller()
        let chat = WorkspaceChat()
        func rows(_ range: ClosedRange<Int>) -> [WorkspaceMessage] {
            range.map { number in
                WorkspaceMessage(id: "q-\(number)", role: .user, text: "Message \(number)",
                                 date: String(format: "2026-11-21T12:00:%02d.000+00:00", number), sources: [])
            }
        }

        model.snapshot = WorkspaceHistory(items: rows(4...6), cursor: "v1", watch: true, nextBefore: "4")
        await chat.load(model)
        precondition(chat.messages.map(\.id) == rows(4...6).map(\.id))
        await chat.loadOlder(model)
        precondition(chat.olderError != nil && chat.nextBefore == "4")
        model.pages["4"] = WorkspaceHistoryPage(items: rows(2...3), nextBefore: "2")
        await chat.loadOlder(model)
        precondition(chat.olderError == nil && chat.nextBefore == "2")
        precondition(chat.messages.map(\.id) == rows(2...6).map(\.id))

        model.snapshot = WorkspaceHistory(items: rows(10...12), cursor: "v2", watch: true, nextBefore: "10")
        model.pages["10"] = WorkspaceHistoryPage(items: rows(7...9), nextBefore: "7")
        model.pages["7"] = WorkspaceHistoryPage(items: rows(4...6), nextBefore: "4")
        await chat.load(model)
        precondition(chat.nextBefore == "2" && chat.messages.map(\.id) == rows(2...12).map(\.id))
        model.pages["2"] = WorkspaceHistoryPage(items: rows(1...1), nextBefore: nil)
        await chat.loadOlder(model)
        precondition(chat.nextBefore == nil && chat.messages.map(\.id) == rows(1...12).map(\.id))
        model.snapshot = WorkspaceHistory(items: rows(11...13), cursor: "v3", watch: true, nextBefore: "11")
        await chat.load(model)
        precondition(chat.nextBefore == nil && chat.messages.map(\.id) == rows(1...13).map(\.id))
    }

    @MainActor
    static func checkPrivateTimeline() {
        let chat = WorkspaceChat()
        let original = pair("private")
        original.forEach(chat.receiveVoiceMessage)
        let publicRows = pair("public")
        publicRows.forEach(chat.receiveVoiceMessage)

        func item(_ text: String? = nil) -> PrivateAnswer {
            PrivateAnswer(id: "private", device: "iphone", question: "Private question?", subject: "fixture",
                          speaker: "unverified", state: text == nil ? "pending" : "ready", answer: text,
                          needsLocation: false, sources: text == nil ? [] : ["fixture-source"],
                          askedAt: "2026-11-21T12:01:00.000+00:00", updatedAt: "2026-11-21T12:02:00.000+00:00")
        }
        chat.reconcilePrivate([item()])
        let pending = WorkspaceMessage.timeline(chat.messages, privateItems: [item()])
        precondition(pending.map(\.id) == ["q-public", "a-public", "q-private", "a-private"])
        precondition(pending.last?.text == "" && pending.last?.privateRequest?.state == "pending")
        precondition(pending.last?.offersReadAloud == false && pending[1].offersReadAloud)

        var ready = item("Private answer.")
        ready.goalId = "goal-from-chat"
        let history = WorkspaceMessage.timeline(chat.messages, privateItems: [ready])
        precondition(history.map(\.id) == pending.map(\.id))
        precondition(history.last?.text == ready.answer && history.last?.sources == ready.sources)
        precondition(history.last?.goalId == "goal-from-chat")
        precondition(history.last?.offersReadAloud == false)
        // A late device echo must not bring an expired or dismissed exchange back into Chat.
        chat.reconcilePrivate([])
        original.forEach(chat.receiveVoiceMessage)
        precondition(WorkspaceMessage.timeline(chat.messages, privateItems: []).map(\.id) == publicRows.map(\.id))
        let reopened = WorkspaceMessage.timeline([], privateItems: [ready])
        precondition(reopened.map(\.text) == [ready.question, "Private answer."])
    }
}
