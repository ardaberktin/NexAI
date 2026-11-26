import Foundation
import UIKit

@MainActor
final class PhoneWatchWorkspace {
    private struct Chunk {
        let data: Data
        let expires: Date
    }

    private struct QuestionJob {
        let id: String
        let task: Task<Void, Never>
        var result: Result<Reply, Error>?
    }

    private var chunks: [String: Chunk] = [:]
    private var question: QuestionJob?
    private var access = UUID()

    func reset() {
        access = UUID()
        chunks.removeAll()
        question?.task.cancel()
        question = nil
    }

    func respond(to data: Data, using model: Controller) async -> WatchResponse {
        guard model.privacy.connected, let api = try? model.connection(), api.workspaceToken != nil else {
            return lockedResponse
        }
        let generation = access
        let privacy = model.privacy.generation
        let valid = { self.access == generation && model.privacy.generation == privacy && model.privacy.connected }
        do {
            guard data.count <= 32_000 else { throw NexError.capture("This Watch request is too large.") }
            let request = try JSONDecoder().decode(WatchRequest.self, from: data)
            chunks = chunks.filter { $0.value.expires > .now }
            if request.action == .chunk { return try nextChunk(request) }
            let bytes: Data
            if request.action == .ask {
                bytes = try ask(request, model: model)
            } else {
                let route = try PhoneWatchRoute.route(request)
                bytes = try await api.bytes(route.path, method: route.method, query: route.query,
                                            body: route.body, timeout: 18)
            }
            guard valid() else { return lockedResponse }
            return try firstChunk(bytes)
        } catch NexError.api(403, "Unlock the Nex workspace with biometrics.") {
            model.lockWorkspace()
            return lockedResponse
        } catch {
            guard valid(), !Task.isCancelled else { return lockedResponse }
            return WatchResponse(error: error.localizedDescription)
        }
    }

    private var lockedResponse: WatchResponse {
        WatchResponse(error: "Open and unlock Nex on the paired iPhone.", locked: true)
    }

    private func ask(_ request: WatchRequest, model: Controller) throws -> Data {
        let text = request.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let id = request.id, WatchLink.isClipID(id), !text.isEmpty, text.utf8.count <= 6_000 else {
            throw NexError.capture("Enter a question of up to 6,000 bytes.")
        }
        if question?.id == id {
            let answer = try question?.result?.get()
            return try JSONEncoder().encode(WatchAnswerProgress(answer: answer))
        }
        guard question == nil || question?.result != nil else {
            throw NexError.capture("Nex is still answering your previous question.")
        }
        let generation = access
        let task = Task {
            let allowance = UIApplication.shared.beginBackgroundTask(withName: "Answer Nex Watch question") { [weak workspace = self] in
                guard workspace?.question?.id == id else { return }
                workspace?.question?.task.cancel()
            }
            defer { if allowance != .invalid { UIApplication.shared.endBackgroundTask(allowance) } }
            let result: Result<Reply, Error>
            do {
                let language = NexLanguage(rawValue: request.language ?? "") ?? model.language
                let response = try await model.ask(text, id: id, context: request.day, publishReply: false,
                                                   access: request.value.flatMap(ChatAccess.init(rawValue:)) ?? .automatic,
                                                   languageOverride: language,
                                                   timeZoneOverride: request.timeZone, clock: request.deviceClock)
                result = .success(response)
            } catch { result = .failure(error) }
            guard access == generation, question?.id == id else { return }
            question?.result = result
        }
        question = QuestionJob(id: id, task: task)
        return try JSONEncoder().encode(WatchAnswerProgress(answer: nil))
    }

    private func firstChunk(_ data: Data) throws -> WatchResponse {
        guard data.count <= WatchLink.maximumResponseBytes else {
            throw NexError.capture("Open this item on your iPhone. It is too large for Apple Watch.")
        }
        let size = WatchLink.responseChunkSize
        guard data.count > size else { return WatchResponse(data: data) }
        if chunks.count >= 3, let oldest = chunks.min(by: { $0.value.expires < $1.value.expires })?.key {
            chunks.removeValue(forKey: oldest)
        }
        let id = nexID()
        chunks[id] = Chunk(data: data, expires: .now.addingTimeInterval(120))
        return WatchResponse(data: data.subdata(in: 0..<size), remainder: id)
    }

    private func nextChunk(_ request: WatchRequest) throws -> WatchResponse {
        guard let id = request.id, let chunk = chunks[id], let offset = request.offset,
              offset > 0, offset < chunk.data.count else {
            throw NexError.capture("The iPhone response expired. Try again.")
        }
        let end = min(offset + WatchLink.responseChunkSize, chunk.data.count)
        let more = end < chunk.data.count
        if !more { chunks.removeValue(forKey: id) }
        return WatchResponse(data: chunk.data.subdata(in: offset..<end), remainder: more ? id : nil)
    }
}
