import Foundation

@MainActor
extension Controller {
    private var workspaceQuery: [URLQueryItem] { [.init(name: "time_zone", value: NexDate.timeZone)] }

    func newsFeed(day: String? = nil) async throws -> NewsFeed {
        var query = workspaceQuery
        if let day { query.append(.init(name: "day", value: day)) }
        return try await connection().get("/v1/workspace/feed", query: query)
    }

    func updateFeed(_ settings: FeedSettings, refreshNow: Bool) async throws -> NewsFeed {
        struct Request: Encodable {
            let settings: FeedSettings
            let refreshNow: Bool

            enum CodingKeys: String, CodingKey {
                case settings
                case refreshNow = "refresh_now"
            }
        }
        return try await connection().send("/v1/workspace/feed", method: "PUT",
                                           body: Request(settings: settings, refreshNow: refreshNow))
    }

    func refreshFeed() async throws -> NewsFeed {
        try await connection().send("/v1/workspace/feed", body: [String: String]())
    }

    func saveStory(_ id: String, saved: Bool) async throws -> NewsFeed {
        struct Request: Encodable { let id: String; let saved: Bool }
        return try await connection().send("/v1/workspace/feed/saved", method: "PUT", body: Request(id: id, saved: saved))
    }

    func ideas() async throws -> IdeasSnapshot {
        try await connection().get("/v1/workspace/ideas")
    }

    func refreshIdeas() async throws -> IdeasSnapshot {
        try await connection().send("/v1/workspace/ideas", body: [String: String]())
    }

    func workspaceHistory(cursor: String?, wait: Bool) async throws -> WorkspaceHistory {
        var query = [URLQueryItem(name: "paged", value: "1")]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        if wait { query.append(URLQueryItem(name: "wait", value: "1")) }
        return try await connection().get("/v1/workspace/chat", query: query, timeout: wait ? 20 : 12)
    }

    func olderWorkspaceHistory(before: String) async throws -> WorkspaceHistoryPage {
        try await connection().get("/v1/workspace/chat", query: [.init(name: "before", value: before)])
    }

    func goals() async throws -> [NexGoal] {
        try await goalOverview().items
    }

    func goalOverview(includeClocks: Bool = false) async throws -> GoalOverview {
        let query: [URLQueryItem] = includeClocks ? [.init(name: "clocks", value: "1")] : []
        return try await connection().get("/v1/workspace/goals", query: query)
    }

    func cancelClock(_ id: String) async throws -> ClockAlert {
        let alert: ClockAlert = try await connection().send("/v1/workspace/clocks/\(id)/cancel", body: [String: String]())
        if alert.status == .cancelled { ClockNotifications.remove(alert) }
        return alert
    }

    func goal(_ id: String) async throws -> NexGoal {
        try await connection().get("/v1/workspace/goals/" + id)
    }

    func saveGoal(_ draft: GoalDraft) async throws -> NexGoal {
        try await connection().send("/v1/workspace/goals/" + draft.id, method: "PUT", body: draft)
    }

    func goalAction(_ goal: NexGoal, action: String) async throws -> NexGoal {
        return try await connection().send("/v1/workspace/goals/" + goal.id,
                                           body: GoalActionRequest(action: action, revision: goal.revision))
    }

    func answerGoalFollowUp(_ goal: String, followUp: String, choice: Int) async throws -> GoalFollowUp {
        try await connection().send("/v1/workspace/goals/" + goal + "/follow-ups",
                                    body: GoalFollowUpAnswer(followUpId: followUp, choice: choice))
    }

    func deleteGoal(_ goal: NexGoal) async throws {
        let _: GoalDeletion = try await connection().send("/v1/workspace/goals/" + goal.id,
                                                          body: GoalActionRequest(action: "delete", revision: goal.revision))
        GoalNotifications.removeScheduledAlert(for: goal.id)
    }

    func artifacts(goal id: String? = nil) async throws -> [NexArtifact] {
        let path = id.map { "/v1/workspace/goals/" + $0 + "/artifacts" } ?? "/v1/workspace/artifacts"
        let result: ListResponse<NexArtifact> = try await connection().get(path)
        return result.items
    }

    func artifactPDF(_ id: String) async throws -> Data {
        guard privacy.unlocked else { throw CancellationError() }
        let opening = privacy.generation
        let data = try await connection().bytes("/v1/workspace/artifacts/" + id + "/pdf")
        guard !Task.isCancelled, privacy.unlocked, privacy.generation == opening else { throw CancellationError() }
        return data
    }
}
