import SwiftUI

struct WatchWorkspaceMenu: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @ObservedObject var sync: WatchSync

    var body: some View {
        Group {
            if let features = workspace.features {
                List {
                    NavigationLink(destination: WatchChatView()) { Label("Chat", systemImage: "bubble.left") }
                    NavigationLink(destination: WatchPrivateInbox()) { Label("Private inbox", systemImage: "lock.envelope") }
                    if features[.daybook] {
                        NavigationLink(destination: WatchDaybookView()) { Label("Daybook", systemImage: "book") }
                    }
                    if features[.activities] {
                        NavigationLink(destination: WatchActivitiesView()) { Label("Activities", systemImage: "waveform.path") }
                    }
                    if features[.feed] {
                        NavigationLink(destination: WatchFeedView()) { Label("Feed", systemImage: "newspaper") }
                    }
                    if features[.ideas] {
                        NavigationLink(destination: WatchIdeasView()) { Label("Ideas", systemImage: "lightbulb") }
                    }
                    if features[.goals] {
                        NavigationLink(destination: WatchGoalsView()) { Label("Goals", systemImage: "target") }
                    }
                    NavigationLink(destination: WatchLibraryView()) { Label("Files", systemImage: "folder") }
                    NavigationLink(destination: WatchMemoryView()) { Label("Memory", systemImage: "brain") }
                    Button("Lock", systemImage: "lock") { workspace.lock() }
                }
                .safeAreaInset(edge: .top, spacing: 0) {
                    WatchWorkspaceRecordingStatus(workspace: workspace, sync: sync)
                }
            } else {
                ScrollView {
                    VStack(spacing: 10) {
                        Image(systemName: "lock.shield").font(.title2).foregroundStyle(Color.accentColor)
                        if let issue = workspace.issue {
                            Text(issue).font(.footnote).foregroundStyle(.orange)
                        } else {
                            Text(unlockInstructions)
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Button(workspace.unlocking ? "Unlocking…" : "Unlock workspace") {
                            Task { await workspace.unlock() }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(workspace.unlocking)
                    }
                }
            }
        }
        .navigationTitle("Workspace")
        .privacySensitive()
    }

    private var unlockInstructions: String {
        guard sync.enabled else { return "Enable Apple Watch access in Nex settings on iPhone." }
        return sync.reachable ? "Unlock Nex on your iPhone, then open your workspace here." :
            "Bring your paired iPhone nearby and open Nex."
    }
}

private struct WatchWorkspaceRecordingStatus: View {
    @ObservedObject var workspace: WatchWorkspace
    @ObservedObject var sync: WatchSync
    @State private var presence = RecordingPresence()

    var body: some View {
        RecordingStatusPeers(local: .watch)
            .environment(\.recordingPresence, presence)
            .task(id: sync.reachable && sync.enabled) {
                guard sync.reachable, sync.enabled else { return }
                while !Task.isCancelled && workspace.features != nil {
                    if let report: RecordingStatusSnapshot = try? await workspace.get(WatchRequest(.recordingStatus)),
                       !Task.isCancelled {
                        presence = RecordingPresence(snapshot: report, receivedAt: .now)
                    }
                    do { try await Task.sleep(for: .seconds(4)) } catch { return }
                }
            }
    }
}

struct WatchPage<Value: Decodable, Content: View>: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    let title: LocalizedStringKey
    var key = ""
    let request: WatchRequest
    var refreshInterval: Duration?
    @ViewBuilder let content: (Value) -> Content
    @State private var value: Value?
    @State private var failure: String?
    @State private var currentRequest = UUID()

    var body: some View {
        List {
            if workspace.features == nil {
                Text("The workspace is locked. Return to Workspace to unlock.").foregroundStyle(.secondary)
            } else {
                if let failure { WatchFailure(message: failure, retry: reload) }
                if let value { content(value) }
                else if failure == nil { ProgressView() }
            }
        }
        .navigationTitle(title)
        .privacySensitive()
        .task(id: key) {
            value = nil
            await reload()
            if let refreshInterval {
                while !Task.isCancelled && workspace.features != nil {
                    do { try await Task.sleep(for: refreshInterval) }
                    catch { return }
                    await reload()
                }
            }
        }
        .refreshable { await reload() }
        .onChange(of: workspace.features == nil) {
            if workspace.features == nil {
                currentRequest = UUID()
                value = nil
                failure = nil
            }
        }
    }

    private func reload() async {
        let identity = UUID()
        currentRequest = identity
        failure = nil
        do {
            let result: Value = try await workspace.get(request)
            if currentRequest == identity { value = result }
        }
        catch is CancellationError { }
        catch { if currentRequest == identity { failure = error.localizedDescription } }
    }
}

struct WatchFailure: View {
    let message: String
    var retry: (() async -> Void)?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(message).font(.footnote).foregroundStyle(.orange)
            if let retry { Button("Try again") { Task { await retry() } } }
        }
    }
}

struct WatchReadingView: View {
    let title: String
    let detail: String
    var subtitle: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 9) {
                if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                Text(detail).font(.body)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(title)
        .modifier(WatchPrivateScreen())
    }
}
