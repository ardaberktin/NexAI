import AVFoundation
import CryptoKit
import SwiftUI

struct VoiceEnrollment: View {
    private struct Status: Decodable {
        let enrolled: Bool
        let sharedEnrolled: Bool?
        let available: Bool

        // Older servers only report whether this device contributed samples.
        var hasVoiceProfile: Bool { sharedEnrolled ?? enrolled }

        enum CodingKeys: String, CodingKey {
            case enrolled, available
            case sharedEnrolled = "shared_enrolled"
        }
    }

    @ObservedObject var model: Controller
    @ObservedObject var privacy: AppPrivacy
    @Environment(\.scenePhase) private var scene
    @State private var status: Result<Status, Error>?
    @State private var enrolling = false
    @State private var samples = Data()
    @State private var working = false
    @State private var issue: String?
    @State private var operation: Task<Void, Never>?
    private let size = 4 * 16_000 * 2
    private let phrases = ["The morning light is coming through the window.",
                           "I have a moment to ask a different question.",
                           "Today I am setting up my voice for Nex."]

    var body: some View {
        Section("Voice recognition") {
            if !privacy.connected {
                Text("Connect your private workspace to manage voice recognition.")
            } else {
                switch status {
                case .success(let profile): profileControls(profile)
                case .failure(let error): Text(error.localizedDescription).foregroundStyle(.secondary)
                case nil: ProgressView("Checking voice profile…")
                }
                if let issue { Text(issue).foregroundStyle(.secondary) }
            }
        }
        .disabled(working)
        .task(id: canRefreshStatus) {
            if canRefreshStatus { await refreshStatus() }
        }
        .onChange(of: privacy.connected) { _, connected in
            if !connected { reset() }
        }
        .onDisappear(perform: reset)
    }

    @ViewBuilder private func profileControls(_ profile: Status) -> some View {
        if !profile.available {
            Text("Install the room voice models on Excalibur to use voice recognition.")
        } else {
            if profile.hasVoiceProfile {
                Label("Voice profile active on all paired devices", systemImage: "waveform.badge.checkmark")
            }
            if enrolling {
                enrollmentControls
            } else {
                Button(enrollmentTitle(profile)) {
                    issue = nil
                    enrolling = true
                }
                if profile.enrolled {
                    Button("Delete samples from this device", role: .destructive) {
                        run { try await update(removing: true) }
                    }
                }
            }
        }
    }

    @ViewBuilder private var enrollmentControls: some View {
        let sample = samples.count / size
        if sample < phrases.count {
            Text("Read aloud: “\(phrases[sample])”")
            Button(working ? "Recording…" : "Record sample \(sample + 1) of 3") {
                run { samples.append(try await record()) }
            }
        } else {
            Button("Save voice profile") { run { try await update(removing: false) } }
        }
        if !samples.isEmpty {
            Button("Start over") {
                samples = Data()
                issue = nil
            }
        }
        Button("Cancel") {
            discardSamples()
            issue = nil
        }
    }

    private func enrollmentTitle(_ profile: Status) -> LocalizedStringKey {
        if profile.enrolled { return "Replace samples from this microphone" }
        if profile.hasVoiceProfile { return "Add samples from this microphone" }
        return "Enroll your voice"
    }

    private var canRefreshStatus: Bool { privacy.connected && scene == .active && !working }

    private func refreshStatus() async {
        while !Task.isCancelled {
            let result: Result<Status, Error>
            do { result = .success(try await request()) }
            catch { result = .failure(error) }
            guard !Task.isCancelled else { return }
            status = result
            do { try await Task.sleep(for: .seconds(12)) } catch { return }
        }
    }

    private func discardSamples() {
        enrolling = false
        samples = Data()
    }

    private func reset() {
        operation?.cancel()
        discardSamples()
        status = nil
        issue = nil
    }

    private func run(_ action: @escaping @MainActor () async throws -> Void) {
        working = true
        issue = nil
        operation = Task { @MainActor in
            defer { working = false }
            do { try await action() } catch { if !Task.isCancelled { issue = error.localizedDescription } }
        }
    }

    @MainActor private func record() async throws -> Data {
        guard !model.capture.ambient, model.liveState == nil, !model.capture.voice, !model.playing else {
            throw NexError.capture("Pause recording and end the current conversation before enrolling.")
        }
        #if os(iOS)
        let allowed = await AVAudioApplication.requestRecordPermission()
        #else
        let allowed = await AVCaptureDevice.requestAccess(for: .audio)
        #endif
        guard allowed else { throw NexError.capture("Allow microphone access in system settings.") }
        try Task.checkCancellation()
        let mic = LiveAudio()
        let (stream, sink) = AsyncThrowingStream<Data, Error>.makeStream(bufferingPolicy: .bufferingNewest(100))
        defer {
            mic.stop()
            sink.finish()
            #if os(iOS)
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            #endif
        }
        #if os(macOS)
        try await mic.start(output: sink)
        #else
        try mic.start(output: sink)
        #endif
        let timeout = Task {
            try await Task.sleep(for: .seconds(10))
            sink.finish(throwing: NexError.capture("No microphone samples arrived. Check your input and try again."))
        }
        defer { timeout.cancel() }
        var data = Data()
        for try await frame in stream {
            try Task.checkCancellation()
            data.append(frame.prefix(size - data.count))
            if data.count == size { return data }
        }
        throw NexError.capture("The microphone stopped before the sample was complete.")
    }

    private func request(_ payload: [String: String]? = nil) async throws -> Status {
        let bytes = try await model.connection().bytes("/v1/voice/profile", method: payload == nil ? "GET" : "POST",
                                                       body: try payload.map { try JSONEncoder().encode($0) },
                                                       timeout: 30, port: 8767)
        return try JSONDecoder().decode(Status.self, from: bytes)
    }

    private func update(removing: Bool) async throws {
        let api = try model.connection()
        let subject = removing ? "delete" : "enroll-" + SHA256.hash(data: samples).map { String(format: "%02x", $0) }.joined()
        try await privacy.withBiometrics(removing ? "Delete this device’s voice samples" : "Save this device’s voice samples") { context in
            let key = try OwnerKey.load(using: context)
            var proof = try await OwnerKey.proof(api, key: key, purpose: "voice", subject: subject)
            try Task.checkCancellation()
            proof["action"] = removing ? "delete" : "enroll"
            if !removing { proof["audio"] = samples.base64EncodedString() }
            let updated = try await request(proof)
            try Task.checkCancellation()
            status = .success(updated)
            discardSamples()
        }
    }
}
