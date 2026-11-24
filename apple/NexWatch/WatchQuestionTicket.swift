import Foundation

/// Written before transport starts so a relaunched watch can still match the answer.
struct WatchQuestionTicket: Codable {
    let conversation: String
    let id: String
    let submitted: Date
    var result: WatchQuestionResult?

    static func restore(from url: URL) -> Self? {
        guard let bytes = try? Data(contentsOf: url),
              let ticket = try? JSONDecoder().decode(Self.self, from: bytes),
              Date.now.timeIntervalSince(ticket.submitted) < 86_400 else { return nil }
        return ticket
    }

    func save(to url: URL) throws {
        try JSONEncoder().encode(self).write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    mutating func accept(_ answer: WatchQuestionResult) -> Bool {
        guard answer.id == id, result == nil else { return false }
        result = answer
        return true
    }
}
