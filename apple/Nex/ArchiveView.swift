import SwiftUI

struct ArchiveView: View {
    @ObservedObject var model: Controller
    let mediaOnly: Bool
    @State private var query = ""
    @State private var memories: [MemoryItem] = []
    @State private var recordings: [Recording] = []
    @State private var selection: MemoryItem?
    @State private var editing: Recording?
    @State private var error: String?
    @State private var browsingAudio = false
    @State private var loading = false
    @State private var canLoadMore = false

    init(model: Controller, mediaOnly: Bool = false) {
        self.model = model
        self.mediaOnly = mediaOnly
        _browsingAudio = State(initialValue: mediaOnly)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                #if os(iOS)
                if !mediaOnly { Text("Archive").font(.largeTitle.weight(.semibold)) }
                #endif
                if model.pairing != nil {
                    #if os(iOS)
                    if !mediaOnly {
                        archivePicker
                    }
                    #endif
                    if browsingAudio { clipList } else { memoryList }
                } else {
                    Text("Pair with Excalibur to open the archive.").foregroundStyle(.secondary)
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            #if os(macOS)
            .frame(maxWidth: WorkspaceStyle.pageWidth, alignment: .leading)
            #else
            .frame(maxWidth: 720, alignment: .leading)
            #endif
            .padding(24)
            .frame(maxWidth: .infinity)
        }
        #if os(macOS)
        .toolbar {
            if !mediaOnly && model.pairing != nil {
                ToolbarItem(placement: .primaryAction) { archivePicker.frame(width: 190) }
            }
        }
        #endif
        .task(id: browsingAudio) { await load() }
        .onChange(of: model.pairing != nil) { _, paired in
            if paired { Task { await load() } }
        }
        .sheet(item: $selection) { item in
            SourceSheet(item: item, model: model) {
                Task { await load() }
            }
        }
        .sheet(item: $editing) { clip in
            TranscriptEditor(clipID: clip.id, model: model) {
                Task { await load() }
            }
        }
    }

    private var archivePicker: some View {
        Picker("Archive view", selection: $browsingAudio) {
            Text("Memories").tag(false)
            Text("Recordings").tag(true)
        }
        .pickerStyle(.segmented)
    }

    private var memoryList: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                TextField("Search transcripts, notes and chats", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await load() } }
                Button("Search") { Task { await load() } }.disabled(loading)
            }
            if memories.isEmpty && !loading {
                Text("No matching memories.").foregroundStyle(.secondary)
            }
            ForEach(memories) { item in
                Divider()
                VStack(alignment: .leading, spacing: 6) {
                    Text(NexDate.display(item.started) + " · " + item.kind.capitalized)
                        .font(.caption).foregroundStyle(.secondary)
                    Text(item.text).lineLimit(4)
                    Button("Open") { selection = item }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var clipList: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("Refresh recordings") { Task { await load() } }.disabled(loading)
            if recordings.isEmpty && !loading {
                Text("No recordings have been uploaded.").foregroundStyle(.secondary)
            }
            ForEach(recordings) { clip in
                Divider()
                Text(NexDate.display(clip.started)).font(.subheadline.weight(.semibold))
                Text(clip.state.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.caption).foregroundStyle(.secondary)
                if let transcript = clip.transcript, !transcript.isEmpty {
                    Text(transcript).textSelection(.enabled)
                }
                if let notes = clip.notes, !notes.isEmpty && notes != clip.transcript {
                    Text(notes).font(.callout).foregroundStyle(.secondary)
                }
                if let answer = clip.reply {
                    MessageBody(answer.text)
                    CitationButtons(sources: answer.sources) { identifier in
                        do { selection = try await model.source(identifier) }
                        catch { self.error = error.localizedDescription }
                    }
                }
                if let failure = clip.error { Text(failure).font(.callout).foregroundStyle(.orange) }
                if clip.audioAvailable == false {
                    Text("Audio expired").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button("Play recording", systemImage: "play") {
                        Task {
                            do { try await model.play(clip.id) }
                            catch { self.error = error.localizedDescription }
                        }
                    }.disabled(model.playing)
                }
                Button("Edit transcript") { editing = clip }
            }
            if canLoadMore {
                Button("Load older recordings") { Task { await load(older: true) } }.disabled(loading)
            }
        }
    }

    private func load(older: Bool = false) async {
        guard model.pairing != nil else { return }
        let audio = browsingAudio
        error = nil
        loading = true
        defer { if audio == browsingAudio { loading = false } }
        do {
            if audio {
                let cursor = older ? recordings.last.map { $0.started + "," + $0.id } : nil
                let page = try await model.clips(before: cursor)
                guard audio == browsingAudio else { return }
                recordings = older ? recordings + page : page
                canLoadMore = page.count >= 35
            } else {
                let found = try await model.journal(query)
                if audio == browsingAudio { memories = found }
            }
        } catch {
            if audio == browsingAudio { self.error = error.localizedDescription }
        }
    }
}

struct SourceSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State var item: MemoryItem
    @ObservedObject var model: Controller
    var onCorrection: () -> Void = {}
    @State private var error: String?
    @State private var editing = false
    @State private var readingWeb = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(item.kind == "web" ? "Web source" : item.kind.capitalized)
                        .font(.title2.weight(.semibold))
                    Spacer()
                    Button("Done") { dismiss() }
                }
                Text(NexDate.display(item.started) + " · " + RecordingDevice.name(item.device))
                    .font(.subheadline).foregroundStyle(.secondary)
                if item.kind == "web" {
                    WebPageContent(item: item, loading: readingWeb) { index in
                        Task { await readWeb(index) }
                    }
                } else if item.kind == "question" || item.kind == "answer" {
                    MessageBody(item.text)
                } else {
                    Text(item.text).textSelection(.enabled)
                }
                if item.kind == "recording" {
                    if item.audioAvailable == false {
                        Text("Audio expired").font(.caption).foregroundStyle(.secondary)
                    } else {
                        Button(model.playing ? "Stop playback" : "Play recording", systemImage: "play") {
                            Task { await togglePlayback() }
                        }
                    }
                    Button("Edit transcript") { editing = true }
                }
                CitationButtons(sources: item.sources) { identifier in
                    do { item = try await model.source(identifier) }
                    catch { self.error = error.localizedDescription }
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            .padding(24)
        }
        .sheet(isPresented: $editing) {
            TranscriptEditor(clipID: item.id, model: model) {
                onCorrection()
                Task {
                    do { item = try await model.source(item.id) }
                    catch { self.error = error.localizedDescription }
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 270)
        #endif
    }

    private func togglePlayback() async {
        if model.playing {
            model.stopPlayback()
        } else {
            do { try await model.play(item.id) }
            catch { self.error = error.localizedDescription }
        }
    }

    private func readWeb(_ index: Int?) async {
        guard !readingWeb else { return }
        readingWeb = true
        error = nil
        defer { readingWeb = false }
        do { item = try await model.readWebSource(item.id, link: index) }
        catch is CancellationError { return }
        catch { self.error = error.localizedDescription }
    }
}
