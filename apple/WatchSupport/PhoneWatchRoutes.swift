import Foundation

struct PhoneWatchRoute {
    let path: String
    var method = "GET"
    var query: [URLQueryItem] = []
    var body: Data?

    init(_ path: String, method: String = "GET", query: [URLQueryItem] = [], body: Data? = nil) {
        self.path = "/v1/" + path
        self.method = method
        self.query = query
        self.body = body
    }

    static func route(_ request: WatchRequest) throws -> Self {
        let zone = [URLQueryItem(name: "time_zone", value: request.timeZone ?? NexDate.timeZone)]
        switch request.action {
        case .open: return Self("workspace/features")
        case .recordingStatus: return Self("workspace/recording-status")
        case .chat:
            if let cursor = request.cursor {
                return Self("workspace/chat", query: [.init(name: "before", value: cursor)])
            }
            return Self("workspace/chat", query: [.init(name: "paged", value: "1")])
        case .daybook, .conversation, .activities:
            guard let day = request.day else { throw invalid }
            let path = request.action == .conversation ? "daybook/conversation" : request.action.rawValue
            return Self(path, query: day.query)
        case .feed:
            return Self("workspace/feed", query: zone + [URLQueryItem(name: "day", value: request.cursor)].filter { $0.value != nil })
        case .savedStory:
            guard let id = request.id, let value = request.value, ["true", "false"].contains(value) else { throw invalid }
            struct Change: Encodable { let id: String; let saved: Bool }
            return Self("workspace/feed/saved", method: "PUT",
                        body: try JSONEncoder().encode(Change(id: id, saved: value == "true")))
        case .ideas: return Self("workspace/ideas")
        case .goals: return Self("workspace/goals", query: [.init(name: "clocks", value: "1")])
        case .goal: return Self("workspace/goals/" + (try identifier(request)))
        case .goalEdit, .goalAction:
            return Self("workspace/goals/" + (try identifier(request)),
                        method: request.action == .goalEdit ? "PUT" : "POST", body: try payload(request))
        case .goalFollowUp:
            return Self("workspace/goals/" + (try identifier(request)) + "/follow-ups",
                        method: "POST", body: try payload(request))
        case .files:
            if request.id != nil { return Self("workspace/goals/" + (try identifier(request)) + "/artifacts") }
            return Self("workspace/artifacts")
        case .recordings:
            let query = request.cursor.map { [URLQueryItem(name: "before", value: $0)] } ?? []
            return Self("recordings", query: query)
        case .recording: return Self("recordings/" + (try identifier(request)))
        case .audio: return Self("recordings/" + (try identifier(request)) + "/audio")
        case .transcript, .correctTranscript:
            let path = "recordings/" + (try identifier(request)) + "/correction"
            return request.action == .transcript ? Self(path) : Self(path, method: "PUT", body: try payload(request))
        case .memory:
            return Self("memory", query: [.init(name: "q", value: String((request.text ?? "").prefix(600)))])
        case .source: return Self("memory/" + (try identifier(request)))
        case .context: return Self("context")
        case .editContext: return Self("context", method: "PUT", body: try payload(request))
        case .inbox: return Self("privacy/inbox")
        case .dismiss:
            return Self("privacy/dismiss", method: "POST",
                        body: try JSONEncoder().encode(["id": identifier(request)]))
        case .ask, .chunk: throw invalid
        }
    }

    private static let invalid = NexError.pairing("The Apple Watch request is invalid.")

    private static func identifier(_ request: WatchRequest) throws -> String {
        guard let id = request.id, id.utf8.count <= 80, !id.isEmpty,
              id.utf8.allSatisfy({ (48...57).contains($0) || (97...122).contains($0) || $0 == 45 }) else {
            throw invalid
        }
        return id
    }

    private static func payload(_ request: WatchRequest) throws -> Data {
        guard let body = request.body, (2...16_000).contains(body.count) else { throw invalid }
        return body
    }
}
