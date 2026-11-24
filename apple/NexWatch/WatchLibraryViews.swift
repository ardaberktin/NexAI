import SwiftUI

struct WatchLibraryView: View {
    var body: some View {
        List {
            NavigationLink(destination: WatchFilesView()) { Label("Files", systemImage: "doc.text") }
            NavigationLink(destination: WatchArchiveView()) { Label("Recordings", systemImage: "waveform") }
        }
        .navigationTitle("Library")
        .modifier(WatchPrivateScreen())
    }
}

struct WatchFilesView: View {
    var goal: String?

    var body: some View {
        WatchPage(title: "Files", request: WatchRequest(.files, id: goal)) { (result: ListResponse<NexArtifact>) in
            if result.items.isEmpty { Text("No files yet.").foregroundStyle(.secondary) }
            ForEach(result.items) { item in
                NavigationLink {
                    WatchReadingView(title: item.title, detail: item.text, subtitle: item.subtitle)
                } label: {
                    WatchTimelineRow(title: item.title, subtitle: NexDate.display(item.date))
                }
            }
        }
    }
}

private struct WatchArchiveView: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @State private var clips: [Recording] = []
    @State private var failure: String?
    @State private var loading = false
    @State private var hasMore = false

    var body: some View {
        List {
            if let failure { WatchFailure(message: failure, retry: { await load() }) }
            if clips.isEmpty && loading { ProgressView() }
            if clips.isEmpty && !loading && failure == nil { Text("No recordings uploaded.").foregroundStyle(.secondary) }
            ForEach(clips) { clip in WatchRecordingLink(clip: clip) }
            if hasMore {
                Button("Older recordings") { Task { await load(older: true) } }.disabled(loading)
            }
        }
        .navigationTitle("Recordings")
        .modifier(WatchPrivateScreen())
        .task { await load() }
        .refreshable { await load() }
        .onChange(of: workspace.features == nil) { if workspace.features == nil { clips = [] } }
    }

    private func load(older: Bool = false) async {
        guard !loading else { return }
        loading = true
        failure = nil
        defer { loading = false }
        do {
            let before = older ? clips.last.map { $0.started + "," + $0.id } : nil
            let result: ListResponse<Recording> = try await workspace.get(WatchRequest(.recordings, cursor: before))
            clips = older ? clips + result.items : result.items
            hasMore = result.items.count >= 35
        } catch is CancellationError { }
        catch { failure = error.localizedDescription }
    }
}

struct WatchRecordingLink: View {
    let clip: Recording

    var body: some View {
        NavigationLink {
            WatchRemoteRecording(id: clip.id)
        } label: {
            let date = NexDate.display(clip.started)
            let device = clip.device.map { " · " + RecordingDevice.name($0) } ?? ""
            WatchTimelineRow(title: clip.transcript ?? (clip.mode == .voice ? "Voice question" : "Recording"),
                             subtitle: date + device)
        }
    }
}

struct WatchRemoteRecording: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @EnvironmentObject private var capture: WatchCapture
    let id: String
    @State private var playbackFailure: String?
    @State private var playbackTask: Task<Void, Never>?

    var body: some View {
        WatchPage(title: "Recording", request: WatchRequest(.recording, id: id)) { (clip: Recording) in
            if let device = clip.device { Text(RecordingDevice.name(device)).font(.caption).foregroundStyle(.secondary) }
            Text(NexDate.display(clip.started)).font(.caption).foregroundStyle(.secondary)
            if let transcript = clip.transcript { Text(transcript) }
            else { Text(clip.state.replacingOccurrences(of: "_", with: " ").capitalized) }
            if let notes = clip.notes, clip.transcript != notes { Text(notes) }
            if let reply = clip.reply {
                Text(reply.text)
                WatchCitationLinks(ids: reply.sources)
            }
            if let error = clip.error { WatchFailure(message: error) }
            if let playbackFailure { WatchFailure(message: playbackFailure) }
            if clip.audioAvailable == false {
                Text("Audio expired").foregroundStyle(.secondary)
            } else {
                Button(workspace.playing ? "Stop audio" : playbackTask != nil ? "Loading audio…" : "Play audio",
                       systemImage: workspace.playing ? "stop.fill" : "play.fill") {
                    if workspace.playing { workspace.stopPlayback() }
                    else { playbackTask = Task { await play() } }
                }
                .disabled(playbackTask != nil || capture.isRecording)
                if capture.isRecording {
                    Text("Pause recording to play audio.").font(.caption2).foregroundStyle(.secondary)
                }
            }
            NavigationLink("Edit transcript", destination: WatchTranscriptView(id: id))
        }
        .onDisappear {
            playbackTask?.cancel()
            workspace.stopPlayback()
        }
    }

    private func play() async {
        playbackFailure = nil
        defer { playbackTask = nil }
        do { try await workspace.play(id, capture: capture) }
        catch is CancellationError { }
        catch { playbackFailure = error.localizedDescription }
    }
}

private struct WatchTranscriptView: View {
    let id: String

    var body: some View {
        WatchPage(title: "Transcript", request: WatchRequest(.transcript, id: id)) { (snapshot: TranscriptSnapshot) in
            if snapshot.editable {
                WatchTranscriptEditor(id: id, snapshot: snapshot)
                    .id(snapshot.revision)
            } else {
                Text("Nex is still transcribing this clip.").foregroundStyle(.secondary)
            }
        }
    }
}

private struct WatchTranscriptEditor: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @Environment(\.dismiss) private var dismiss
    let id: String
    let snapshot: TranscriptSnapshot
    @State private var text = ""
    @State private var failure: String?
    @State private var saving = false

    var body: some View {
        TextField("Transcript", text: $text)
            .onAppear { text = snapshot.text ?? "" }
        Button("Save transcript") { Task { await save() } }
            .disabled(saving || text == snapshot.text)
        if let failure { WatchFailure(message: failure) }
        if !snapshot.history.isEmpty {
            Section("Previous versions") {
                ForEach(snapshot.history) { revision in
                    NavigationLink(NexDate.display(revision.changed), destination: WatchReadingView(
                        title: "Previous transcript", detail: revision.previousText ?? ""))
                }
            }
        }
    }

    private func save() async {
        saving = true
        failure = nil
        defer { saving = false }
        do {
            let correction = TranscriptCorrection(id: nexID(), revision: snapshot.revision, text: text)
            let _: TranscriptSnapshot = try await workspace.get(WatchRequest(.correctTranscript, id: id,
                                                                    body: JSONEncoder().encode(correction)))
            dismiss()
        } catch is CancellationError { }
        catch { failure = error.localizedDescription }
    }
}
