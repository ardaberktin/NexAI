import Foundation

struct PrivateAnswer: Decodable, Identifiable, Equatable {
    let id: String
    let device: String
    let question: String
    let subject: String
    let speaker: String
    let state: String
    let answer: String?
    let needsLocation: Bool
    let sources: [String]
    let askedAt: String?
    let updatedAt: String?
    var goalId: String? = nil
    var clockAlert: ClockAlert? = nil
    var pdfAttachment: NexArtifact? = nil

    var chatMessages: [WorkspaceMessage] {
        // Servers predating Chat timestamps still present these items in the private inbox.
        guard let askedAt else { return [] }
        let questionRow = WorkspaceMessage(id: "q-" + id, role: .user, text: question,
                                           date: askedAt, sources: [], privateQuestion: true)
        let complete = state == "ready"
        let answerRow = WorkspaceMessage(id: "a-" + id, role: .assistant, text: complete ? answer ?? "" : "",
                                        date: complete ? max(askedAt, updatedAt ?? askedAt) : askedAt,
                                        sources: complete ? sources : [], goalId: goalId,
                                        pdfAttachment: pdfAttachment, privateRequest: self)
        return [questionRow, answerRow]
    }
}

extension WorkspaceMessage {
    var exchangeID: String? {
        guard id.hasPrefix("q-") || id.hasPrefix("a-") else { return nil }
        return String(id.dropFirst(2))
    }

    var isPrivate: Bool { privateQuestion || privateRequest != nil || privateNotice }

    var copyableText: String? {
        if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return text }
        return title ?? pdfAttachment?.title
    }

    var offersReply: Bool {
        role != .user && !privateNotice && copyableText != nil
    }

    var offersReadAloud: Bool { offersReply }

    static func chronological(_ first: Self, _ second: Self) -> Bool {
        (first.date, first.role == .user ? 0 : 1, first.id) < (second.date, second.role == .user ? 0 : 1, second.id)
    }

    static func timeline(_ ordinary: [Self], privateItems: [PrivateAnswer]) -> [Self] {
        guard !privateItems.isEmpty else { return ordinary }
        var rows = Dictionary(uniqueKeysWithValues: ordinary.map { ($0.id, $0) })
        for item in privateItems {
            for message in item.chatMessages { rows[message.id] = message }
        }
        return rows.values.sorted(by: chronological)
    }
}
