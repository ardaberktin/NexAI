import SwiftUI

struct WorkspaceLibrary: View {
    @ObservedObject var model: Controller
    @State private var media = false

    var body: some View {
        VStack(spacing: 0) {
            #if os(iOS)
            VStack(alignment: .leading, spacing: 15) {
                Text("Library")
                    .font(.system(.largeTitle, design: .rounded, weight: .bold))
                    .accessibilityAddTraits(.isHeader)
                libraryPicker
            }
            .frame(maxWidth: WorkspaceStyle.readable, alignment: .leading)
            .padding(.horizontal, WorkspaceStyle.inset)
            .padding(.top, 20)
            .padding(.bottom, 8)
            .frame(maxWidth: .infinity)
            #endif

            if media {
                ArchiveView(model: model, mediaOnly: true)
            } else {
                GoalArtifactList(model: model)
            }
        }
        #if os(macOS)
        .toolbar {
            ToolbarItem(placement: .primaryAction) { libraryPicker.frame(width: 150) }
        }
        #endif
    }

    private var libraryPicker: some View {
        Picker("Library", selection: $media) {
            Text("Files").tag(false)
            Text("Media").tag(true)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }
}

struct GoalArtifactList: View {
    @ObservedObject var model: Controller
    var goal: String?
    @State private var items: [NexArtifact]?
    @State private var opened: NexArtifact?
    @State private var error: String?

    var body: some View {
        Group {
            #if os(macOS)
            VStack(spacing: 0) {
                if let error {
                    WorkspaceIssue(message: error, retry: load)
                        .padding(.horizontal, WorkspaceStyle.inset)
                }
                if let items, !items.isEmpty {
                    MacArtifactBrowser(items: items) { opened = $0 }
                } else {
                    listStatus
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            #else
            ScrollView {
                LazyVStack(spacing: 0) {
                    if let error { WorkspaceIssue(message: error, retry: load) }
                    listStatus
                    ForEach(items ?? []) { item in
                        row(item)
                        Divider().opacity(0.55)
                    }
                }
                .frame(maxWidth: WorkspaceStyle.readable)
                .padding(.horizontal, WorkspaceStyle.inset)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity)
            }
            #endif
        }
        .refreshable { await load() }
        .task { await load() }
        .sheet(item: $opened) { artifact in
            WorkspaceSheet(title: "File") { ArtifactDocument(artifact: artifact) }
        }
    }

    @ViewBuilder private var listStatus: some View {
        if items?.isEmpty == true {
            WorkspaceEmpty(title: "No files yet", symbol: "doc.text",
                           detail: goal == nil ? "Daily news editions and reports from your goals appear here." :
                            "Reports will appear here after Nex finds a match or reaches a reminder time.")
        } else if items == nil && error == nil {
            ProgressView("Opening files…").padding(.vertical, 30)
        }
    }

    #if os(iOS)
    private func row(_ item: NexArtifact) -> some View {
        Button { opened = item } label: {
            HStack(spacing: 13) {
                WorkspaceIcon(symbol: item.kind == "feed" ? "newspaper" : "doc.text")
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.title).font(.headline)
                    Text(item.subtitle + "  ·  " + NexDate.display(item.date))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.caption.weight(.medium)).foregroundStyle(.secondary)
            }
            .padding(.vertical, 13)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
    #endif

    private func load() async {
        do {
            let result = try await model.artifacts(goal: goal)
            guard !Task.isCancelled else { return }
            items = result
            error = nil
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}
