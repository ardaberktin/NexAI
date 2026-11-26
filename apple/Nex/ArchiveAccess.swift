import Foundation

private struct ChatQuestion: Encodable {
    let id: String
    let text: String
    let language: String
    let time_zone: String
    let daybook_day: String?
    let daybook_source: String?
    let daybook_focus: String?
    let access: String?
    let private_reply: Bool?
    let device_clock: DeviceClockReading?
    let weather_location_supported = true
    var weather_location: WeatherLocationResult?
    let attachments: [String]?
}

@MainActor
extension Controller {
    func publishVoiceReply(_ answer: Reply, chatID: String?) {
        reply = answer
        if let speaker = answer.speaker { recognizedSpeaker = speaker == "recognized" }
        if let status = answer.statusMessage {
            message = status
            return
        }
        if let chatID {
            voiceChatMessages.send(WorkspaceMessage(id: "a-" + chatID, role: .assistant, text: answer.text,
                date: NexDate.wire(), sources: answer.sources, language: answer.language,
                privateNotice: answer.engine == "private_request"))
            if answer.engine == "private_request", privacy.connected {
                Task { try? await privacy.refresh(self) }
            }
        }
    }

    func ask(_ question: String, id: String, context: DaybookContext? = nil, publishReply: Bool = true,
             access: ChatAccess = .automatic, languageOverride: NexLanguage? = nil,
             timeZoneOverride: String? = nil, clock: DeviceClockReading? = .current,
             attachments: [String] = []) async throws -> Reply {
        let api = try connection()
        let opening = privacy.generation
        var request = ChatQuestion(id: id, text: question, language: (languageOverride ?? language).rawValue,
                                   time_zone: context?.timeZone ?? timeZoneOverride ?? NexDate.timeZone,
                                   daybook_day: context?.day, daybook_source: context?.source,
                                   daybook_focus: context?.focus, access: access.requestValue,
                                   private_reply: access == .privateReply ? true : nil,
                                   device_clock: clock,
                                   attachments: attachments.isEmpty ? nil : attachments)
        var answer: Reply = try await api.send("/v1/chat", body: request, timeout: 220)
        if answer.engine == "weather_location_request", context == nil {
            request.weather_location = await WeatherLocationResult.current(for: id)
            try Task.checkCancellation()
            guard privacy.generation == opening, privacy.unlocked else { throw CancellationError() }
            answer = try await api.send("/v1/chat", body: request, timeout: 220)
        }
        guard privacy.generation == opening else { throw CancellationError() }
        if let status = answer.statusMessage { throw NexError.api(503, status) }
        if access.requiresPrivateReply && answer.engine != "private_request" {
            throw NexError.api(409, "The server did not confirm private delivery.")
        }
        if answer.engine == "private_request", privacy.connected {
            try? await privacy.refresh(self)
        }
        guard privacy.generation == opening else { throw CancellationError() }
        if let alert = privacy.items.first(where: { $0.id == id })?.clockAlert {
            Task {
                guard privacy.generation == opening else { return }
                await ClockNotifications.enable(for: alert, using: self)
            }
        } else if answer.goalId != nil {
            Task {
                guard privacy.generation == opening else { return }
                await GoalNotifications.enable(using: self)
            }
        }
        if context == nil && publishReply { reply = answer }
        return answer
    }

    func daybook(_ context: DaybookContext) async throws -> DaybookDay {
        try await connection().get("/v1/daybook", query: context.query)
    }

    func daybookConversation(_ context: DaybookContext) async throws -> DaybookDetail {
        try await connection().get("/v1/daybook/conversation", query: context.query)
    }

    func journal(_ query: String) async throws -> [MemoryItem] {
        let api = try connection()
        let result: ListResponse<MemoryItem> = try await api.get(
            "/v1/memory", query: [URLQueryItem(name: "q", value: query)]
        )
        return result.items
    }

    func clips(before: String?) async throws -> [Recording] {
        let api = try connection()
        let query = before.map { [URLQueryItem(name: "before", value: $0)] } ?? []
        let response: ListResponse<Recording> = try await api.get("/v1/recordings", query: query)
        return response.items
    }

    func source(_ id: String) async throws -> MemoryItem {
        let api = try connection()
        return try await api.get("/v1/memory/" + id)
    }

    func readWebSource(_ id: String, link: Int?) async throws -> MemoryItem {
        struct Selection: Encodable {
            let source: String
            let link: Int?
        }
        return try await connection().send("/v1/web/read", body: Selection(source: id, link: link))
    }

    func transcript(_ id: String) async throws -> TranscriptSnapshot {
        let api = try connection()
        return try await api.get("/v1/recordings/" + id + "/correction")
    }

    func correctTranscript(_ id: String, correction: TranscriptCorrection) async throws -> TranscriptSnapshot {
        let api = try connection()
        return try await api.send("/v1/recordings/" + id + "/correction", method: "PUT", body: correction)
    }

}
