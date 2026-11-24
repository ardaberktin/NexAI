import SwiftUI

struct IdeasView: View {
    @Environment(\.scenePhase) private var scene
    @ObservedObject var model: Controller
    let compose: (String) -> Void
    let newGoal: (String) -> Void
    @State private var snapshot: IdeasSnapshot?
    @State private var error: String?
    @State private var refreshing = false
    @State private var currentRead: UUID?

    private var pending: Bool { refreshing || snapshot?.pending == true }

    var body: some View {
        WorkspacePage(title: "Ideas", subtitle: "Things Nex can help you do") {
            Button("Refresh ideas", systemImage: "arrow.clockwise") { Task { await rebuild() } }
                .labelStyle(.iconOnly)
                .disabled(pending)
                #if os(macOS)
                .help("Refresh ideas")
                #else
                .buttonStyle(.bordered)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                #endif
        } content: {
            if let error {
                WorkspaceIssue(message: error, retry: load)
            } else if snapshot?.failed == true && !refreshing {
                WorkspaceIssue(message: String(localized: "Excalibur could not generate ideas."), retry: rebuild)
            } else if pending || snapshot == nil {
                ProgressView("Finding ideas…").frame(maxWidth: .infinity).padding(.vertical, 30)
            }
            if let snapshot {
                if !snapshot.items.isEmpty {
                    WorkspaceCollection(items: snapshot.items, row: ideaRow)
                } else if !pending && snapshot.failed != true && error == nil {
                    Text("No ideas available.").foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, 30)
                }
            }
        }
        .refreshable { await load() }
        .task(id: scene == .active) {
            guard scene == .active else { return }
            while !Task.isCancelled {
                await load()
                do { try await Task.sleep(for: .seconds(pending ? 2 : 5)) }
                catch { return }
            }
        }
    }

    private func ideaRow(_ idea: NexIdea) -> some View {
        Button {
            if idea.opensGoal == true { newGoal(idea.prompt) } else { compose(idea.prompt) }
        } label: {
            HStack(alignment: .top, spacing: 13) {
                WorkspaceIcon(symbol: idea.icon)
                VStack(alignment: .leading, spacing: 6) {
                    Text(idea.title).font(.headline)
                    Text(idea.detail).font(.subheadline).foregroundStyle(.secondary)
                    Text(idea.opensGoal == true ? "Set up goal" : "Open in Chat")
                        .font(.caption.weight(.semibold)).foregroundStyle(WorkspaceStyle.accent)
                        .padding(.top, 3)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "arrow.up.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    .padding(.top, 3)
            }
            .padding(.vertical, 17)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func load() async {
        guard !refreshing, !Task.isCancelled else { return }
        let read = UUID()
        currentRead = read
        do {
            let result = try await model.ideas()
            guard !Task.isCancelled, currentRead == read else { return }
            snapshot = result
            error = nil
        } catch {
            if !Task.isCancelled && currentRead == read { self.error = error.localizedDescription }
        }
    }

    private func rebuild() async {
        guard !pending, !Task.isCancelled else { return }
        currentRead = nil
        refreshing = true
        error = nil
        defer { refreshing = false }
        do {
            let result = try await model.refreshIdeas()
            if !Task.isCancelled { snapshot = result }
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}
