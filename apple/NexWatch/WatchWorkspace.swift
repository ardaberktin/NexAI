import AVFoundation
import Combine
import LocalAuthentication

@MainActor
final class WatchWorkspace: ObservableObject {
    @Published private(set) var features: WorkspaceFeatures?
    @Published private(set) var unlocking = false
    @Published private(set) var playing = false
    @Published var issue: String?
    @Published var language = NexLanguage(rawValue: UserDefaults.standard.string(forKey: "watch.language") ?? "") ?? .auto {
        didSet { UserDefaults.standard.set(language.rawValue, forKey: "watch.language") }
    }

    private let link: WatchSync
    private var generation = UUID()
    private var player: AVAudioPlayer?
    private var playerTask: Task<Void, Never>?
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    init(link: WatchSync) { self.link = link }

    func unlock() async {
        guard !unlocking else { return }
        unlocking = true
        issue = nil
        let attempt = generation
        defer { unlocking = false }
        do {
            let context = LAContext()
            defer { context.invalidate() }
            guard try await context.evaluatePolicy(.deviceOwnerAuthenticationWithWristDetection,
                    localizedReason: "Open your private Nex workspace on Apple Watch.") else { return }
            try Task.checkCancellation()
            guard generation == attempt else { return }
            let data = try await response(WatchRequest(.open), generation: attempt)
            features = try decoder.decode(WorkspaceFeatures.self, from: data)
        } catch is CancellationError { }
        catch { issue = error.localizedDescription }
    }

    func lock() {
        generation = UUID()
        features = nil
        stopPlayback()
    }

    func get<Value: Decodable>(_ request: WatchRequest) async throws -> Value {
        let data = try await bytes(request)
        return try decoder.decode(Value.self, from: data)
    }

    func ask(_ text: String, day: DaybookContext?, access: ChatAccess) async throws -> Reply {
        let request = WatchRequest(.ask, id: nexID(), day: day, text: text,
                                   language: language.rawValue, timeZone: NexDate.timeZone,
                                   value: access.requestValue, deviceClock: .current)
        let expires = Date.now.addingTimeInterval(230)
        repeat {
            let progress: WatchAnswerProgress = try await get(request)
            if let answer = progress.answer { return answer }
            try await Task.sleep(for: .seconds(2))
        } while Date.now < expires
        throw NexError.capture("Nex is still working. Refresh Chat to check for the answer.")
    }

    func answerGoalFollowUp(_ goal: String, followUp: String, choice: Int) async throws -> GoalFollowUp {
        let body = try JSONEncoder().encode(GoalFollowUpAnswer(followUpId: followUp, choice: choice))
        return try await get(WatchRequest(.goalFollowUp, id: goal, body: body))
    }

    func play(_ clip: String, capture: WatchCapture) async throws {
        guard !capture.isRecording else { throw NexError.capture("Pause recording to play audio.") }
        let data = try await bytes(WatchRequest(.audio, id: clip))
        guard !capture.isRecording else { throw NexError.capture("Pause recording to play audio.") }
        stopPlayback()
        let audio = try AVAudioPlayer(data: data)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio)
        try session.setActive(true)
        guard audio.play() else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw NexError.capture("Apple Watch could not play this recording.")
        }
        player = audio
        playing = true
        playerTask = Task {
            while audio.isPlaying {
                do { try await Task.sleep(for: .milliseconds(250)) }
                catch { return }
            }
            stopPlayback()
        }
    }

    func stopPlayback() {
        playerTask?.cancel()
        playerTask = nil
        if let player {
            player.stop()
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
        player = nil
        playing = false
    }

    private func bytes(_ request: WatchRequest) async throws -> Data {
        guard features != nil else { throw CancellationError() }
        return try await response(request, generation: generation)
    }

    private func response(_ request: WatchRequest, generation: UUID) async throws -> Data {
        var request = request
        var all = Data()
        while true {
            let data = try await link.request(JSONEncoder().encode(request))
            try Task.checkCancellation()
            guard self.generation == generation else { throw CancellationError() }
            let result = try decoder.decode(WatchResponse.self, from: data)
            if result.locked {
                issue = result.error
                lock()
            }
            if let error = result.error { throw NexError.connection(error) }
            guard let data = result.data, all.count + data.count <= WatchLink.maximumResponseBytes,
                  result.remainder == nil || !data.isEmpty else {
                throw NexError.capture("The iPhone returned an incomplete response.")
            }
            all.append(data)
            guard let remainder = result.remainder else { return all }
            request = WatchRequest(.chunk, id: remainder, offset: all.count)
        }
    }
}
