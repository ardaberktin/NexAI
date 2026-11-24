import SwiftUI

struct WatchRecordingView: View {
    @ObservedObject var capture: WatchCapture
    @ObservedObject var sync: WatchSync
    @ObservedObject var workspace: WatchWorkspace
    @ObservedObject var live: WatchLiveConversation
    @Environment(\.isLuminanceReduced) private var dimmed

    var body: some View {
        ScrollView {
            if let state = live.state {
                WatchLiveControls(state: state, live: live)
                    .padding(.horizontal, 3)
            } else {
                recorder
            }
        }
        .navigationTitle("Nex")
    }

    private var recorder: some View {
        VStack(spacing: 12) {
            Button {
                live.start(language: workspace.language)
            } label: {
                Label("Ask Nex", systemImage: "waveform.bubble")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(capture.busy || capture.voice)
            if let issue = live.issue {
                WatchLiveIssue(message: issue, reply: live.reply)
            }
            recordingStatus
            Button {
                if capture.isRecording { capture.pause() }
                else { Task { await capture.startRecording(language: workspace.language) } }
            } label: {
                Label(capture.isRecording ? "Pause recording" :
                        capture.needsResume ? "Resume recording" : "Start recording",
                      systemImage: capture.isRecording ? "pause.fill" : "mic.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(capture.isRecording ? .red : .accentColor)
            .disabled(capture.busy)

            if let issue = capture.issue {
                Text(issue).font(.footnote).foregroundStyle(.orange)
            }

            syncStatus
            NavigationLink {
                WatchWorkspaceMenu(sync: sync)
            } label: {
                Label("Workspace", systemImage: "square.grid.2x2")
                    .frame(maxWidth: .infinity)
            }
            NavigationLink {
                WatchRecordingSettings(capture: capture, sync: sync, workspace: workspace)
            } label: {
                Label("Settings", systemImage: "gearshape")
                    .frame(maxWidth: .infinity)
            }
        }
        .padding(.horizontal, 3)
    }

    private var recordingStatus: some View {
        VStack(spacing: 5) {
            Image(systemName: "waveform")
                .font(.system(size: 31, weight: .semibold))
                .foregroundStyle(capture.isRecording ? .red : Color.accentColor)
                .accessibilityHidden(true)
            Text(capture.voice ? "Recording question" : capture.isRecording ? "Recording" :
                    capture.needsResume ? "Recording interrupted" : "Recording paused")
                .font(.headline)
                .accessibilityAddTraits(.isHeader)
            if let started = capture.started {
                Text(timerInterval: started...Date.distantFuture, countsDown: false)
                    .monospacedDigit()
                    .font(.subheadline)
                    .multilineTextAlignment(.center)
                    .opacity(dimmed ? 0.65 : 1)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 18))
    }

    private var syncStatus: some View {
        VStack(spacing: 3) {
            if sync.pending > 0 {
                Label("\(sync.pending) waiting to sync", systemImage: "arrow.trianglehead.clockwise")
                    .font(.footnote)
            }
            if !sync.enabled {
                Text("Enable Apple Watch access in Nex settings on iPhone.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if let error = sync.error {
                Text(error).font(.caption2).foregroundStyle(.orange)
            }
        }
        .multilineTextAlignment(.center)
    }
}

private struct WatchRecordingSettings: View {
    @ObservedObject var capture: WatchCapture
    @ObservedObject var sync: WatchSync
    @ObservedObject var workspace: WatchWorkspace

    var body: some View {
        List {
            Picker("Speech language", selection: $workspace.language) {
                ForEach(NexLanguage.allCases) { Text($0.title).tag($0) }
            }
            .disabled(capture.isRecording)
            LabeledContent("iPhone relay", value: sync.enabled ? "On" : "Off")
            Button("Sync recordings", systemImage: "arrow.clockwise") { sync.retryNow() }
                .disabled(!sync.enabled)
            RecordingNotificationPermission(onEnable: { capture.refreshRecordingAlert() }) { EmptyView() }
            if workspace.features != nil {
                Button("Lock workspace", systemImage: "lock") { workspace.lock() }
            }
        }
        .navigationTitle("Settings")
    }
}
