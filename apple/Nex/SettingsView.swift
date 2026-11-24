import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    @ObservedObject var model: Controller
    @State private var profile = ""
    @State private var showProfilePicker = false
    @State private var pending = false
    @State private var error: String?

    var body: some View {
        Form {
            WorkspaceFeatureSettings(model: model)
            AuthenticationSettings(autoLock: model.privacy.autoLock, lock: model.lockWorkspace)
            #if os(iOS)
            PhoneWatchSettings(model: model)
            #endif
            #if os(macOS)
            MacGemmaSettings(gemma: model.gemma)
            #endif
            AccountConnectionsView(model: model)
            if model.pairing != nil { WeatherSharingSettings(model: model, sharing: model.weatherSharing) }
            Section("Excalibur") {
                if InstalledPairing.available {
                    Button("Use installed pairing profile") { Task { await pair(installed: true) } }
                        .disabled(pending)
                }
                if let pairing = model.pairing {
                    LabeledContent("Tailscale address", value: pairing.url)
                    if let status = model.status {
                        LabeledContent("Chat", value: chatStatus(status.models.chat))
                        LabeledContent("Transcription", value: status.models.transcription == "configured" ? "Local Whisper configured" : "Waiting for model")
                        LabeledContent("Audio allowance",
                                       value: ByteCountFormatter.string(fromByteCount: Int64(status.remainingBytes),
                                                                        countStyle: .file))
                    }
                    Button("Update pairing profile") { showProfilePicker = true }
                    Button("Forget pairing", role: .destructive) { model.unpair() }
                } else {
                    Text("Connect Tailscale, then import the pairing profile for this device.")
                        .foregroundStyle(.secondary)
                    SecureField("Paste this device’s pairing profile", text: $profile)
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        #endif
                    Button(pending ? "Pairing…" : "Pair") { Task { await pair() } }
                        .disabled(pending || profile.isEmpty)
                    Button("Open pairing file") { showProfilePicker = true }.disabled(pending)
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            if model.pairing != nil {
                VoiceEnrollment(model: model, privacy: model.privacy)
                ExcaliburModelSettings(model: model)
            }
            NexNotificationSettings(model: model)
            Section("Speech and recording") {
                Picker("Speech language", selection: $model.language) {
                    ForEach(NexLanguage.allCases) { Text($0.title).tag($0) }
                }
                QuestionChimeVolumeSetting()
                Toggle("Live Conversation", isOn: $model.liveVoice)
                if model.liveVoice {
                    Text("Talk and interrupt naturally while connected to Excalibur. Recording can continue during the conversation.")
                        .foregroundStyle(.secondary)
                }
                Toggle("On-device transcription fallback", isOn: $model.useDeviceTranscription)
                if model.useDeviceTranscription {
                    Text("Choose English or Turkish for Apple’s offline recognizer. Excalibur uses Whisper when configured.")
                        .foregroundStyle(.secondary)
                }
                Toggle("Respond without a wake word", isOn: $model.automaticVoice)
                if model.automaticVoice {
                    if model.liveVoice {
                        Toggle("Continuous listening while recording", isOn: $model.continuousListening)
                    }
                    Text(model.continuousListening && model.liveVoice
                         ? "While recording, microphone audio streams to Excalibur so Nex can hear longer requests and decide when to answer. This uses about 115 MB per hour before network overhead."
                         : "Nex checks short speech clips for questions while Live Conversation is enabled.")
                        .foregroundStyle(.secondary)
                } else {
                    Toggle("Listen for “Hey Nex” questions", isOn: $model.handsFree)
                    if model.handsFree {
                        Text("While recording, say “Hey Nex” followed by a question. Voice clips are processed locally when Excalibur is reachable.")
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .fileImporter(isPresented: $showProfilePicker, allowedContentTypes: [.json]) { result in
            do {
                let url = try result.get()
                let accessed = url.startAccessingSecurityScopedResource()
                defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard 0 < size && size < 4096 else {
                    throw NexError.pairing("Choose the small JSON pairing profile for this device.")
                }
                profile = try String(contentsOf: url, encoding: .utf8)
                Task { await pair() }
            } catch { self.error = error.localizedDescription }
        }
    }

    private func pair(installed: Bool = false) async {
        pending = true
        error = nil
        defer { pending = false }
        do {
            let pairing = try installed ? InstalledPairing.read() : JSONDecoder().decode(Pairing.self, from: Data(profile.utf8))
            try await model.pair(pairing)
            profile = ""
        } catch {
            self.error = error is DecodingError ? "Paste the complete pairing profile for this device." : error.localizedDescription
        }
    }

    private func chatStatus(_ state: String) -> String {
        switch state {
        case "excalibur_live": "Selected Excalibur model"
        case "configured": "Excalibur model configured"
        case "mac_connected": "Mac model connected"
        case "mac_waiting": "Waiting for Mac relay"
        default: "Archive search"
        }
    }
}

private struct WeatherSharingSettings: View {
    let model: Controller
    @ObservedObject var sharing: WeatherSharing

    var body: some View {
        Section {
            Toggle("Share approximate location", isOn: Binding(get: { sharing.enabled }, set: { value in
                Task { await sharing.setEnabled(value, using: model) }
            }))
            .disabled(sharing.busy)
            if let status = sharing.status { Text(status).foregroundStyle(.secondary) }
        } header: {
            Text("Location sharing")
        } footer: {
            Text("Excalibur keeps your device’s latest approximate location for local weather and scheduled reports.")
            #if os(iOS)
            Text("Choose Always in iPhone Location Services to update it when Nex is in the background.")
            #endif
        }
    }
}

private struct QuestionChimeVolumeSetting: View {
    @AppStorage(QuestionChime.volumeKey) private var volume = QuestionChime.defaultVolume

    var body: some View {
        let title: LocalizedStringKey = "Acknowledgement chime volume"
        VStack(alignment: .leading) {
            LabeledContent(title) {
                Text(volume, format: .percent.precision(.fractionLength(0)))
            }
            .accessibilityHidden(true)
            Slider(value: $volume, in: 0...1, step: 0.01) {
                Text(title)
            }
            .labelsHidden()
        }
    }
}

private struct NexNotificationSettings: View {
    @ObservedObject var model: Controller
    #if os(iOS)
    @ObservedObject private var push = IPhonePush.shared
    #endif

    var body: some View {
        Section("Notifications") {
            #if os(iOS)
            if #available(iOS 26, *), model.pairing != nil, model.workspaceFeatureEnabled(.goals) {
                IPhoneNativeAlarmSetting()
            }
            #endif
            RecordingNotificationPermission(onEnable: { await GoalNotifications.enable(using: model) }) {
                if model.pairing != nil && model.workspaceFeatureEnabled(.goals) {
                    #if os(iOS)
                    switch push.state {
                    case .registered: Text("Apple Push Notifications registered")
                    case .connecting: Text("Connecting to Apple Push Notifications…")
                    case .local: Text("On-device timed reminders enabled")
                    }
                    #else
                    Text("Timed reminders and Gmail alerts enabled while Nex is running")
                    #endif
                }
            }
        }
    }
}
