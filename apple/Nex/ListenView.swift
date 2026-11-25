import SwiftUI

struct CaptureWorkspaceControls: View {
    @ObservedObject var model: Controller
    @ObservedObject var capture: Capture

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                captureCard
                #if os(macOS)
                ScreenSharingControls(sharing: model.screenSharing)
                #endif
                if let message = model.liveError ?? model.message {
                    WorkspaceIssue(message: message)
                }
            }
            .frame(maxWidth: WorkspaceStyle.readable)
            .padding(WorkspaceStyle.inset)
            .frame(maxWidth: .infinity)
        }
    }

    private var captureCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(model.liveState?.label ?? capture.status,
                  systemImage: capture.recording || model.liveState != nil ? "mic.fill" : "mic.slash")
                .font(.headline)
                .foregroundStyle(capture.recording ? Color.accentColor : Color.primary)
            #if os(macOS)
            TimelineView(.periodic(from: .now, by: 3)) { _ in
                if MacMicrophoneRoute.blockedByClosedLid {
                    Label("The closed lid disconnects the built-in microphone. Open the lid or select an external microphone.",
                          systemImage: "mic.slash")
                        .font(.subheadline)
                }
            }
            #endif
            if capture.ambient && capture.recording && model.liveState != nil {
                Label("Recording to archive", systemImage: "record.circle")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if let transcript = model.liveTranscript {
                if model.recognizedSpeaker { Label("Recognized voice", systemImage: "waveform").font(.caption) }
                Text("You said: \(transcript)").font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
            }
            if model.pending > 0 {
                Text("Waiting to upload: \(model.pending)").font(.subheadline).foregroundStyle(.secondary)
            }
            HStack {
                Button(capture.ambient && !capture.interrupted ? "Pause" :
                        (capture.interrupted ? "Resume" : "Start recording")) {
                    Task { await model.toggleRecording() }
                }.buttonStyle(.borderedProminent)
                if capture.ambient && capture.interrupted {
                    Button("Pause", action: model.pauseRecording).buttonStyle(.bordered)
                }
                if model.serverProfile != nil {
                    Button(model.liveState != nil ? "End conversation" :
                           (capture.voice ? "Send question" : "Ask by voice")) {
                        Task { await model.toggleVoice() }
                    }.buttonStyle(.bordered).disabled(model.playing)
                }
            }
            if model.playing {
                Button("Stop playback", action: model.stopPlayback).buttonStyle(.bordered)
            }
            if model.voiceUnrecognized && model.liveState == nil && !capture.listeningForQuestion && !capture.voice {
                Text("I couldn’t hear that clearly. Try again.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if model.status?.models.transcription == "awaiting_models" && !model.useDeviceTranscription {
                Text("Recordings are saved. Transcription needs an offline speech model.")
                    .font(.subheadline).foregroundStyle(.secondary)
            } else if model.localSpeechUnavailable {
                Text("An on-device recognizer was unavailable. Excalibur retains the audio for later transcription.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
    }

}

struct CitationButtons: View {
    let sources: [String]
    let open: (String) async -> Void

    var body: some View {
        ForEach(Array(sources.enumerated()), id: \.offset) { index, identifier in
            Button("Open source \(index + 1)", systemImage: "doc.text.magnifyingglass") {
                Task { await open(identifier) }
            }
        }
    }
}
