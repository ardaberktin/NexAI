#if os(macOS)
import Foundation

extension ScreenVision {
    func answer(_ question: String, frame: Data?, history: [ScreenExchange],
                observations: [ScreenObservation] = [], language: String, spoken: Bool,
                personal: PersonalContext? = nil) async throws -> String {
        let prompt = try ScreenVisionPrompt.question(question, frame: frame, history: history,
                                                     observations: observations, language: language,
                                                     spoken: spoken, personal: personal)
        return try await complete(prompt)
    }

    func describeActivity(frame: Data) async throws -> String {
        try await complete(.observation(frame: frame))
    }

    private func complete(_ prompt: ScreenVisionPrompt) async throws -> String {
        let socket = try await readySocket()
        try Task.checkCancellation()
        let body = try JSONSerialization.data(withJSONObject: [
            "messages": prompt.messages, "max_tokens": prompt.tokens, "temperature": 0.2,
            "cache_prompt": false, "chat_template_kwargs": ["enable_thinking": false]
        ])
        let data = try await ScreenVisionHTTP.request(socket: socket, route: "/v1/chat/completions",
                                                      body: body, timeout: prompt.timeout)
        try Task.checkCancellation()
        let response = try JSONDecoder().decode(Completion.self, from: data)
        guard let text = response.choices.first?.message.content?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else {
            throw NexError.capture("The on-device vision model returned an empty answer.")
        }
        return text
    }

    private struct Completion: Decodable {
        struct Choice: Decodable {
            struct Message: Decodable { let content: String? }
            let message: Message
        }
        let choices: [Choice]
    }
}
#endif
