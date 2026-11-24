import SwiftUI

struct WatchChatView: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @EnvironmentObject private var capture: WatchCapture
    @EnvironmentObject private var live: WatchLiveConversation
    var prompt = ""
    var day: DaybookContext?
    @State private var draft = ""
    @State private var messages: [WorkspaceMessage] = []
    @State private var clockExchanges: [PrivateAnswer] = []
    @State private var nextBefore: String?
    @State private var reply: Reply?
    @State private var failure: String?
    @State private var question: Task<Void, Never>?
    @State private var loaded = false
    @State private var loading = false

    var body: some View {
        List {
            if workspace.features == nil {
                Text("Return to Workspace to unlock.")
            } else {
                composer
                if let failure { WatchFailure(message: failure, retry: { await load() }) }
                if let reply { answer(reply) }
                if day == nil { history }
            }
        }
        .navigationTitle(day == nil ? "Chat" : "Ask Nex")
        .privacySensitive()
        .task {
            draft = prompt
            guard day == nil else { return }
            while !Task.isCancelled {
                await load()
                do { try await Task.sleep(for: .seconds(12)) }
                catch { return }
            }
        }
        .refreshable { if day == nil { await load() } }
        .onDisappear {
            question?.cancel()
            if capture.isForeground { live.leaveChat() }
        }
        .onChange(of: workspace.features == nil) {
            if workspace.features == nil {
                question?.cancel()
                if capture.isForeground { live.leaveChat() }
                draft = ""
                reply = nil
                messages = []
                clockExchanges = []
            }
        }
    }

    @ViewBuilder private var composer: some View {
        if let state = live.state {
            WatchLiveControls(state: state, live: live)
        } else {
            entry
        }
    }

    private var entry: some View {
        Section {
            TextField("Ask Nex", text: $draft)
            if question != nil {
                ProgressView("Waiting for Nex…")
            } else {
                Button("Send", systemImage: "arrow.up.circle.fill") { send(privately: false) }
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Button("Ask privately", systemImage: "lock") { send(privately: true) }
                }
            }
            if day == nil {
                Button("Ask by voice", systemImage: "waveform.bubble") {
                    live.start(language: workspace.language)
                }
                .disabled(capture.busy || capture.voice || question != nil)
                if let issue = live.issue {
                    WatchLiveIssue(message: issue, reply: live.reply)
                }
                Button(capture.voice ? "Send recording" : "Record for later",
                       systemImage: capture.voice ? "stop.circle" : "mic") {
                    Task { await capture.toggleVoice(language: workspace.language) }
                }
                .disabled(capture.busy)
                if capture.voiceQueued {
                    Text("Voice question queued. Its reply will appear in Chat or your private inbox.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var history: some View {
        let timeline = WorkspaceMessage.timeline(messages, privateItems: clockExchanges)
        return Section("Recent messages") {
            if timeline.isEmpty, loaded {
                Text("No messages yet.").foregroundStyle(.secondary)
            } else if !loaded && failure == nil {
                ProgressView()
            }
            ForEach(timeline.reversed()) { message in
                NavigationLink {
                    List {
                        if message.role == .user {
                            Text(verbatim: message.text)
                        } else {
                            MarkdownMessageBody(message.text)
                        }
                        if let clock = message.privateRequest?.clockAlert { ClockAlertCard(alert: clock) }
                        WatchCitationLinks(ids: message.sources)
                        if let id = message.goalId {
                            if let followUp = message.followUp {
                                GoalFollowUpChoices(followUp: followUp) { choice in
                                    try await workspace.answerGoalFollowUp(id, followUp: followUp.id, choice: choice)
                                }
                            }
                            NavigationLink("Open goal", destination: WatchGoalView(id: id))
                        }
                    }
                    .privacySensitive()
                    .navigationTitle(message.role == .user ? "You" : "Nex")
                    .modifier(WatchPrivateScreen())
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(message.role == .user ? "You" : message.title ?? "Nex").font(.caption).foregroundStyle(.secondary)
                        if let clock = message.privateRequest?.clockAlert {
                            ClockAlertCard(alert: clock)
                        } else {
                            Text(verbatim: message.role == .user ? message.text : MessageMarkdown(message.text).preview)
                                .lineLimit(3)
                        }
                    }
                }
            }
            if nextBefore != nil {
                Button("Older messages") { Task { await load(older: true) } }.disabled(loading)
            }
        }
    }

    private func answer(_ reply: Reply) -> some View {
        Section("Nex") {
            if reply.engine == "private_request" && reply.text.isEmpty {
                NavigationLink("Private inbox", destination: WatchPrivateInbox())
            } else if let status = reply.statusMessage {
                Text(status).foregroundStyle(.secondary)
            } else {
                MarkdownMessageBody(reply.text)
            }
            WatchCitationLinks(ids: reply.sources)
            if let id = reply.goalId { NavigationLink("Open goal", destination: WatchGoalView(id: id)) }
        }
    }

    private func send(privately: Bool) {
        guard question == nil else { return }
        let text = draft
        failure = nil
        reply = nil
        question = Task {
            defer { question = nil }
            do {
                let answer = try await workspace.ask(text, day: day, privately: privately)
                draft = ""
                reply = answer
                if day == nil { await load() }
            } catch is CancellationError { }
            catch { failure = error.localizedDescription }
        }
    }

    private func load(older: Bool = false) async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let page: WorkspaceHistoryPage = try await workspace.get(WatchRequest(.chat, cursor: older ? nextBefore : nil))
            if older {
                let existing = Set(messages.map(\.id))
                messages = page.items.filter { !existing.contains($0.id) } + messages
            } else {
                let newest = Set(page.items.map(\.id))
                let previous = messages.filter { message in
                    page.items.first.map { message.date < $0.date } == true && !newest.contains(message.id)
                }
                if previous.isEmpty { nextBefore = page.nextBefore }
                messages = previous + page.items
            }
            if older { nextBefore = page.nextBefore }
            if !older, let inbox: ListResponse<PrivateAnswer> = try? await workspace.get(WatchRequest(.inbox)),
               !Task.isCancelled, workspace.features != nil {
                clockExchanges = inbox.items.filter { $0.clockAlert != nil }
            }
            loaded = true
            if question == nil { failure = nil }
        } catch is CancellationError { }
        catch { if failure == nil { failure = error.localizedDescription } }
    }
}

struct WatchPrivateInbox: View {
    var body: some View {
        WatchPage(title: "Private inbox", request: WatchRequest(.inbox)) { (result: ListResponse<PrivateAnswer>) in
            if result.items.isEmpty {
                Text("No private answers.").foregroundStyle(.secondary)
            }
            ForEach(result.items) { item in
                NavigationLink {
                    WatchPrivateAnswerView(item: item)
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.question)
                        Text(item.state.capitalized).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

private struct WatchPrivateAnswerView: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @Environment(\.dismiss) private var dismiss
    let item: PrivateAnswer
    @State private var failure: String?

    var body: some View {
        List {
            Text(item.question).font(.headline)
            if item.state == "ready", let answer = item.answer {
                MarkdownMessageBody(answer)
                WatchCitationLinks(ids: item.sources)
            } else if item.clockAlert == nil {
                Text("Open the private inbox on iPhone or Mac to handle this request.").foregroundStyle(.secondary)
            }
            if let clock = item.clockAlert { ClockAlertCard(alert: clock) }
            Button("Dismiss", role: .destructive) {
                Task {
                    do {
                        let _: [String: Bool] = try await workspace.get(WatchRequest(.dismiss, id: item.id))
                        dismiss()
                    } catch is CancellationError { }
                    catch { failure = error.localizedDescription }
                }
            }
            if let failure { WatchFailure(message: failure) }
        }
        .navigationTitle("Private answer")
        .modifier(WatchPrivateScreen())
    }
}

struct WatchPrivateScreen: ViewModifier {
    @EnvironmentObject private var workspace: WatchWorkspace

    func body(content: Content) -> some View {
        Group {
            if workspace.features == nil {
                Text("Return to Workspace to unlock.")
            } else {
                content
            }
        }
        .privacySensitive()
    }
}
