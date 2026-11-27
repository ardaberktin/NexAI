import NaturalLanguage
import SwiftUI

struct WorkspaceChatView: View {
    @ObservedObject var model: Controller
    @ObservedObject var chat: WorkspaceChat
    @ObservedObject var privacy: AppPrivacy
    let openGoal: (String) -> Void
    let openActivities: (Date) -> Void
    let pair: () -> Void
    @State private var source: MemoryItem?
    @State private var sourceError: String?
    @State private var pdf: NexArtifact?
    #if os(iOS)
    @State private var selection: ChatTextSelection?
    #endif

    var body: some View {
        let inbox = privacy.unlocked && privacy.connected ? privacy.items : nil
        let messages = chat.timeline(unlockedInbox: inbox)
        VStack(spacing: 0) {
            if model.workspaceFeatureEnabled(.activities) {
                RecordingChatActivity(model: model, capture: model.capture, open: openActivities)
            }
            #if os(macOS)
            if messages.isEmpty && !chat.sending && model.liveState == nil {
                VStack(spacing: 12) {
                    welcome
                    historyIssue
                }
                .padding(WorkspaceStyle.inset)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                thread(messages)
            }
            #else
            thread(messages)
            #endif
            if model.serverProfile != nil {
                if let error = chat.sendError ?? sourceError ?? model.chatSpeechError ?? model.liveError ?? voiceIssue {
                    WorkspaceIssue(message: error)
                        .padding(.horizontal, WorkspaceStyle.inset)
                }
                ChatComposer(model: model, chat: chat) { web in
                    sourceError = nil
                    Task { await chat.send(model, web: web) }
                }
            }
        }
        #if os(iOS)
        .frame(maxWidth: WorkspaceStyle.readable)
        .frame(maxWidth: .infinity)
        #endif
        .sheet(item: $source) { item in SourceSheet(item: item, model: model) }
        .sheet(item: $pdf) { artifact in
            WorkspaceSheet(title: "File") { PDFArtifactDocument(model: model, artifact: artifact) }
                .privacySensitive()
        }
        #if os(iOS)
        .sheet(item: $selection) { ChatSelectionSheet(selection: $0) }
        #endif
        .task(id: privacy.connected) {
            if privacy.connected { try? await model.refreshStatus() }
        }
        .onChange(of: privacy.generation) {
            pdf = nil
            #if os(iOS)
            if selection?.isPrivate == true { selection = nil }
            #endif
        }
    }

    private func thread(_ messages: [WorkspaceMessage]) -> some View {
        ChatThreadScrollView(firstMessageID: messages.first?.id, latestMessageID: messages.last?.id, isSending: chat.sending,
                             nextBefore: chat.olderError == nil ? chat.nextBefore : nil,
                             loadOlder: { await chat.loadOlder(model) },
                             refresh: { if model.serverProfile != nil { await chat.load(model) } }) {
            #if os(iOS)
            if messages.isEmpty && !chat.sending { welcome }
            #endif
            historyIssue
            ForEach(messages) { message in
                row(message).id(message.id)
            }
            if chat.sending {
                ProgressView("Nex is thinking…").controlSize(.small)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let voice = model.liveState {
                Label(voice.label, systemImage: "waveform")
                    .font(.subheadline).foregroundStyle(WorkspaceStyle.accent)
            }
        } pagingStatus: { olderHistoryStatus }
    }

    @ViewBuilder private var olderHistoryStatus: some View {
        if chat.loadingOlder {
            ProgressView("Loading older messages…")
                .controlSize(.small)
                .padding(10)
                .background(.regularMaterial, in: Capsule())
                .allowsHitTesting(false)
        } else if chat.olderError != nil {
            Button("Retry loading older messages", systemImage: "arrow.clockwise") {
                Task { await chat.loadOlder(model) }
            }
            .buttonStyle(.borderedProminent)
        }
    }

    @ViewBuilder private var welcome: some View {
        if model.serverProfile == nil {
            VStack(spacing: 6) {
                WorkspaceEmpty(
                    title: "Your conversations", symbol: "waveform",
                    detail: "Connect to Excalibur to talk with Nex and see updates from your goals.")
                Button("Connect Nex", action: pair).buttonStyle(.borderedProminent)
            }
            #if os(iOS)
            .padding(.top, 46)
            #endif
        } else if !chat.loaded {
            ProgressView("Opening Chat…").frame(maxWidth: .infinity).padding(.vertical, 44)
        } else if chat.historyError == nil {
            WorkspaceEmpty(
                title: "What’s on your mind?", symbol: "bubble.left.and.bubble.right",
                detail: "Ask Nex about your day, your connected accounts, or something on the web.")
        }
    }

    @ViewBuilder private var historyIssue: some View {
        if let error = chat.historyError {
            WorkspaceIssue(message: error, retry: { await chat.load(model) })
        }
    }

    private func openSource(_ id: String) async {
        let opening = privacy.generation
        do {
            let item = try await model.source(id)
            if opening == privacy.generation, privacy.unlocked { source = item }
        } catch {
            if opening == privacy.generation, privacy.unlocked { sourceError = error.localizedDescription }
        }
    }

    private func openPDF(_ artifact: NexArtifact) {
        if privacy.unlocked && privacy.connected { pdf = artifact }
    }

    private func row(_ message: WorkspaceMessage) -> some View {
        let read = { model.readChatMessage(message, language: speechLanguage(for: message)) }
        let reply = {
            if !message.isPrivate || (privacy.unlocked && privacy.connected) { chat.insertReply(to: message) }
        }
        #if os(iOS)
        return ChatMessageRow(
            message: message, model: model, openGoal: openGoal, openPDF: openPDF, openSource: openSource,
            readAloud: read, reply: reply
        ) { text in
            if !message.isPrivate || (privacy.unlocked && privacy.connected) {
                selection = ChatTextSelection(id: message.id, text: text, isPrivate: message.isPrivate)
            }
        }
        #else
        return ChatMessageRow(message: message, model: model, openGoal: openGoal, openPDF: openPDF,
                              openSource: openSource, readAloud: read, reply: reply)
        #endif
    }

    private func speechLanguage(for message: WorkspaceMessage) -> String {
        if let language = message.language, language != NexLanguage.auto.rawValue { return language }
        if model.language != .auto { return model.language.rawValue }
        return NLLanguageRecognizer.dominantLanguage(for: String(message.text.prefix(1000))) == .turkish ? "tr" : "en"
    }

    private var voiceIssue: String? {
        model.voiceUnrecognized && model.liveState == nil ? "Nex couldn’t hear that clearly. Try again." : nil
    }
}
