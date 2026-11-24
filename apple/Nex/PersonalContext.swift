import Foundation

enum ContextFileName: String, CaseIterable, Codable, Identifiable {
    case now, memory, soul

    var id: Self { self }
    var filename: String { rawValue.uppercased() + ".md" }

    var title: String {
        switch self {
        case .now: "Now"
        case .memory: "About you"
        case .soul: "Soul"
        }
    }

    var symbol: String {
        switch self {
        case .now: "clock"
        case .memory: "person.text.rectangle"
        case .soul: "sparkle"
        }
    }

    var detail: String {
        switch self {
        case .now: "Recent work from conversations and your shared Mac screen."
        case .memory: "Your preferences, ongoing projects, and things you’ve asked Nex to remember."
        case .soul: "How Nex speaks and works with you."
        }
    }

    var empty: String {
        switch self {
        case .now: "Recent context appears here as Nex learns about your work."
        case .memory: "Tell Nex “Remember that…” to save something about you."
        case .soul: "Add instructions to shape how Nex responds."
        }
    }
}

struct ContextFile: Codable, Identifiable {
    let name: ContextFileName
    let markdown: String
    let revision: String
    let modified: String

    var id: ContextFileName { name }
}

struct PersonalContext: Codable {
    let now: ContextFile
    let memory: ContextFile
    let soul: ContextFile
    let activityRequest: Bool?
    let activities: [ActivityEvidence]?

    struct ActivityEvidence: Codable {
        let text: String
    }

    subscript(name: ContextFileName) -> ContextFile {
        switch name {
        case .now: now
        case .memory: memory
        case .soul: soul
        }
    }

    #if os(macOS)
    static func isFileRequest(_ question: String) -> Bool {
        let clean = question.lowercased().replacingOccurrences(of: #"^(?:hey\s+)?nex\b[\s,.:!?-]*"#,
                                                                 with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
        let write = #"^(?:remember\s*:|remember that\b|update (?:your )?soul\b|soul\.md\s*:)"#
        let read = #"^(?:what do you (?:remember|know) about me|(?:show me|read) your (?:memory|soul)(?: file)?|what is in your (?:memory|soul)|what is my current focus|what am i focused on|what do you think i am working on)$"#
        return clean.range(of: write, options: .regularExpression) != nil
            || clean.range(of: read, options: .regularExpression) != nil
    }
    #endif
}

struct ContextEdit: Encodable {
    let name: ContextFileName
    let revision: String
    let markdown: String
}

#if !os(watchOS)
extension Controller {
    func personalContext() async throws -> PersonalContext {
        try await connection().get("/v1/context")
    }

    func saveContext(_ file: ContextFile, markdown: String) async throws -> ContextFile {
        try await connection().send("/v1/context", method: "PUT",
                                    body: ContextEdit(name: file.name, revision: file.revision, markdown: markdown))
    }

    #if os(macOS)
    func reportScreenContext(_ summary: String?, observed: Date) async {
        guard let connection = try? connection() else { return }
        var payload = ["observed": NexDate.wire(observed)]
        payload["summary"] = summary
        struct Result: Decodable { let ok: Bool }
        let _: Result? = try? await connection.send("/v1/context/screen", method: "PUT", body: payload, timeout: 5)
    }

    func screenPersonalContext(for question: String) async -> PersonalContext? {
        let query = [URLQueryItem(name: "question", value: question), URLQueryItem(name: "time_zone", value: NexDate.timeZone)]
        return try? await connection().get("/v1/context", query: query, timeout: 2)
    }
    #endif
}
#endif
