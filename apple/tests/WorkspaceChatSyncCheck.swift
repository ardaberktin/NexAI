import Foundation

// Stand-in for HTTP calls used by the shared SwiftUI chat model.
@MainActor
final class Controller {
    var snapshot = WorkspaceHistory(items: [], cursor: "empty", watch: true)
    var pages: [String: WorkspaceHistoryPage] = [:]
    var answer: (String) async throws -> Reply = { _ in throw URLError(.notConnectedToInternet) }
    var requestedAccess = ChatAccess.automatic
    var requestedText = ""
    var requestedAttachments: [String] = []

    func workspaceHistory(cursor: String?, wait: Bool) async throws -> WorkspaceHistory { snapshot }

    func olderWorkspaceHistory(before: String) async throws -> WorkspaceHistoryPage {
        guard let page = pages[before] else { throw URLError(.notConnectedToInternet) }
        return page
    }

    func ask(_ question: String, id: String, publishReply: Bool, access: ChatAccess,
             attachments: [String] = []) async throws -> Reply {
        requestedAccess = access
        requestedText = question
        requestedAttachments = attachments
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
            precondition(model.requestedAccess == .automatic)
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
        await checkChatAccess()
        let privateAnswer = checkPrivateTimeline()
        await checkHistoryPages()
        await checkReplyActions(privateAnswer)
        checkSpeechChunks()
        print("Chat sync, message actions, and speech chunk checks passed.")
    }

    @MainActor
    static func checkChatAccess() async {
        let model = Controller()
        let chat = WorkspaceChat()
        let privateReply = Reply(text: "", language: "en", sources: [], engine: "private_request")
        model.answer = { _ in
            precondition(chat.messages.isEmpty)
            return privateReply
        }
        for access in [ChatAccess.automatic, .privateMessage] {
            chat.access = access
            chat.draft = "A private draft"
            await chat.send(model)
            precondition(model.requestedAccess == access && chat.access == .automatic)
            precondition(chat.messages.isEmpty && chat.draft.isEmpty && chat.sendError == nil)
        }
        chat.access = .standard
        chat.draft = "Keep this in Chat history"
        model.answer = { _ in reply }
        await chat.send(model)
        precondition(model.requestedAccess == .standard && chat.messages.count == 2)
        precondition(ChatAccess.privateReply.requestValue == ChatAccess.privateMessage.requestValue)

        chat.access = .privateMessage
        chat.draft = "Unsent private draft"
        chat.clearPrivateReply()
        precondition(chat.draft.isEmpty && chat.access == .automatic)
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
    static func checkPrivateTimeline() -> WorkspaceMessage {
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
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let update = try! decoder.decode(WorkspaceMessage.self, from: Data("""
            {"id":"update","role":"update","text":"Saved.","date":"2026-11-21T12:02:00.000+00:00","sources":[],
             "pdf_attachment":{"id":"fixture-pdf","title":"Morning Edition","subtitle":"PDF",
                               "date":"2026-11-21T12:02:00.000+00:00","kind":"pdf","text":"","page_count":1}}
            """.utf8))
        precondition(update.pdfAttachment?.id == "fixture-pdf" && update.pdfAttachment?.pageCount == 1)
        ready.pdfAttachment = update.pdfAttachment
        let history = WorkspaceMessage.timeline(chat.messages, privateItems: [ready])
        precondition(history.map(\.id) == pending.map(\.id))
        precondition(history.last?.text == ready.answer && history.last?.sources == ready.sources)
        precondition(history.last?.goalId == "goal-from-chat")
        precondition(history.last?.pdfAttachment == ready.pdfAttachment)
        precondition(history.dropLast().allSatisfy { $0.pdfAttachment == nil })
        precondition(history.last?.offersReadAloud == true && history.last?.offersReply == true)
        precondition(history[2].isPrivate && history.last?.isPrivate == true)
        precondition(history[2].copyableText == ready.question && history.last?.copyableText == ready.answer)
        // A late device echo must not bring an expired or dismissed exchange back into Chat.
        chat.reconcilePrivate([])
        original.forEach(chat.receiveVoiceMessage)
        precondition(WorkspaceMessage.timeline(chat.messages, privateItems: []).map(\.id) == publicRows.map(\.id))
        let reopened = WorkspaceMessage.timeline([], privateItems: [ready])
        precondition(reopened.map(\.text) == [ready.question, "Private answer."])
        precondition(reopened.last?.pdfAttachment?.pageCount == 1)
        return history[3]
    }

    @MainActor
    static func checkReplyActions(_ privateAnswer: WorkspaceMessage) async {
        let model = Controller()
        let chat = WorkspaceChat()
        let rows = pair("actions")
        precondition(rows[0].copyableText == "Ready?" && !rows[0].offersReply && !rows[0].offersReadAloud)
        precondition(rows[1].copyableText == reply.text && rows[1].offersReply && rows[1].offersReadAloud)
        precondition(rows.allSatisfy { NexDate.display($0.date) != $0.date })

        chat.draft = "Can you explain this?"
        chat.insertReply(to: rows[0])
        precondition(chat.draft == "Can you explain this?" && chat.replyInsertion == 0)
        chat.insertReply(to: rows[1])
        precondition(chat.draft == "Can you explain this?\n\nReady.\n\n" && chat.replyInsertion == 1)
        precondition(model.requestedText.isEmpty && !chat.replyingPrivately)

        chat.draft = "Keep this draft"
        chat.insertReply(to: privateAnswer)
        precondition(chat.replyingPrivately)
        chat.clearPrivateReply()
        precondition(chat.draft == "Keep this draft" && !chat.replyingPrivately)

        chat.draft = ""
        chat.insertReply(to: privateAnswer)
        await chat.send(model, web: true)
        precondition(model.requestedAccess == .privateReply && model.requestedText == privateAnswer.text)
        precondition(chat.replyingPrivately && chat.draft == privateAnswer.text)
        precondition(chat.messages.isEmpty && chat.sendError != nil)

        model.answer = { _ in
            chat.clearPrivateReply()
            throw CancellationError()
        }
        await chat.send(model)
        precondition(!chat.replyingPrivately && chat.draft.isEmpty)
        precondition(chat.messages.isEmpty && chat.sendError == nil)
    }

    static func checkSpeechChunks() {
        let sentence = "Nex reads the full reply, including its last sentence. "
        let text = String(repeating: sentence, count: 100) + "End of the reply."
        let chunks = ChatSpeech.chunks(text)
        precondition(chunks.count > 1 && chunks.joined(separator: " ") == text)
        precondition(chunks.allSatisfy { $0.unicodeScalars.count <= ChatSpeech.requestLimit })

        let unicode = String(repeating: "🎙️", count: 1200)
        let split = ChatSpeech.chunks(unicode)
        precondition(split.count > 1 && split.joined() == unicode)
        precondition(split.allSatisfy { $0.unicodeScalars.count <= ChatSpeech.requestLimit })
        precondition(ChatSpeech.chunks(" \n").isEmpty)
    }
}
