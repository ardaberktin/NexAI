import SwiftUI

struct WatchMemoryView: View {
    @State private var draft = ""
    @State private var query = ""

    var body: some View {
        WatchPage(title: "Memory", key: query, request: WatchRequest(.memory, text: query)) { (result: ListResponse<MemoryItem>) in
            NavigationLink("Personal context", destination: WatchContextView())
            TextField("Search memory", text: $draft)
            Button("Search", systemImage: "magnifyingglass") { query = draft }
            if result.items.isEmpty { Text("No matching memories.").foregroundStyle(.secondary) }
            ForEach(result.items) { item in
                NavigationLink {
                    WatchSourceView(id: item.id)
                } label: {
                    WatchTimelineRow(title: item.text, subtitle: NexDate.display(item.started))
                }
            }
        }
    }
}

private struct WatchContextView: View {
    var body: some View {
        WatchPage(title: "Personal context", request: WatchRequest(.context)) { (result: PersonalContext) in
            ForEach(ContextFileName.allCases) { name in
                NavigationLink(name.title, destination: WatchContextEditor(file: result[name]))
            }
        }
    }
}

private struct WatchContextEditor: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @State var file: ContextFile
    @State private var update = ""
    @State private var failure: String?
    @State private var saving = false

    var body: some View {
        List {
            Text(file.markdown.isEmpty ? file.name.empty : file.markdown)
            if file.name != .now {
                TextField("Append to this file", text: $update)
                Button("Append") { Task { await save() } }.disabled(saving || update.isEmpty)
            }
            if let failure { WatchFailure(message: failure) }
        }
        .navigationTitle(file.name.title)
        .modifier(WatchPrivateScreen())
    }

    private func save() async {
        saving = true
        failure = nil
        defer { saving = false }
        do {
            let markdown = [file.markdown, update].filter { !$0.isEmpty }.joined(separator: "\n\n")
            let edit = ContextEdit(name: file.name, revision: file.revision, markdown: markdown)
            file = try await workspace.get(WatchRequest(.editContext, body: JSONEncoder().encode(edit)))
            update = ""
        } catch is CancellationError { }
        catch { failure = error.localizedDescription }
    }
}

struct WatchCitationLinks: View {
    let ids: [String]

    var body: some View {
        ForEach(Array(ids.enumerated()), id: \.offset) { index, id in
            NavigationLink(destination: WatchSourceView(id: id)) {
                Label("Source \(index + 1)", systemImage: "doc.text.magnifyingglass")
            }
        }
    }
}

private struct WatchSourceView: View {
    let id: String

    var body: some View {
        WatchPage(title: "Source", request: WatchRequest(.source, id: id)) { (item: MemoryItem) in
            if !item.device.isEmpty { Text(RecordingDevice.name(item.device)).font(.caption).foregroundStyle(.secondary) }
            Text(NexDate.display(item.started)).font(.caption).foregroundStyle(.secondary)
            Text(item.text)
            if item.kind == "recording" {
                NavigationLink("Open recording", destination: WatchRemoteRecording(id: item.id))
            }
            WatchCitationLinks(ids: item.sources.filter { $0 != item.id })
        }
    }
}
