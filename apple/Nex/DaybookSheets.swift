import SwiftUI

struct DaybookConversationSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: Controller
    let context: DaybookContext
    @State private var conversation: DaybookDetail?
    @State private var error: String?
    @State private var source: MemoryItem?
    @State private var question: DaybookContext?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Conversation").font(.title2.weight(.semibold))
                    Spacer()
                    Button("Done") { dismiss() }
                }
                Button("Ask about this conversation", systemImage: "waveform.bubble") { question = context }
                    .buttonStyle(.borderedProminent)
                    .disabled(conversation == nil)
                if let conversation {
                    ForEach(conversation.recordings) { recording in
                        segment(recording)
                    }
                } else if error == nil {
                    ProgressView("Loading transcript…")
                }
                if let error { Text(error).foregroundStyle(.secondary) }
            }
            .padding(24)
        }
        .task { await load() }
        .sheet(item: $question) { selection in
            DaybookAskSheet(model: model, context: selection)
        }
        .sheet(item: $source) { item in
            SourceSheet(item: item, model: model) { Task { await load() } }
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 450)
        #endif
    }

    private func segment(_ recording: Recording) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            Text(NexDate.display(recording.started)).font(.caption).foregroundStyle(.secondary)
            if let transcript = recording.transcript {
                Text(transcript).textSelection(.enabled)
            }
            if let reply = recording.reply {
                Label("Nex", systemImage: "waveform").font(.caption.weight(.semibold))
                MessageBody(reply.text)
            }
            Button("Open recording", systemImage: "doc.text.magnifyingglass") {
                Task {
                    do { source = try await model.source(recording.id) }
                    catch { self.error = error.localizedDescription }
                }
            }
            .font(.callout)
        }
    }

    private func load() async {
        do { conversation = try await model.daybookConversation(context) }
        catch { self.error = error.localizedDescription }
    }
}

struct DaybookAskSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: Controller
    @ObservedObject private var privacy: AppPrivacy
    let context: DaybookContext
    @State private var question = ""
    @State private var response: (id: String, reply: Reply)?
    @State private var source: MemoryItem?
    @State private var error: String?
    @State private var asking = false
    @State private var lastRequest: (text: String, id: String)?

    private var live: Bool { model.liveContext == context && model.liveState != nil }

    init(model: Controller, context: DaybookContext) {
        self.model = model
        privacy = model.privacy
        self.context = context
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 15) {
                HStack {
                    Text("Ask Nex").font(.title2.weight(.semibold))
                    Spacer()
                    Button("Done") { dismiss() }
                }
                Text("Daybook · \(context.day)")
                    .font(.subheadline).foregroundStyle(.secondary)
                if let focus = context.focus {
                    Text(focus).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Button(live ? "End conversation" : "Talk live", systemImage: live ? "stop.circle" : "waveform") {
                    error = nil
                    Task { await model.toggleVoice(context: context) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.playing)
                if live, let state = model.liveState {
                    Text(state.label).font(.subheadline)
                    if let transcript = model.liveTranscript {
                        Text(transcript).font(.callout).foregroundStyle(.secondary)
                    }
                }
                TextField("Ask about a detail, decision, or follow-up", text: $question, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(2...5)
                Button(asking ? "Asking…" : "Send question") { Task { await ask() } }
                    .buttonStyle(.bordered)
                    .disabled(asking || live || question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                answerContent
                if let message = error ?? model.liveError {
                    Text(message).foregroundStyle(.orange)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .sheet(item: $source) { item in
            SourceSheet(item: item, model: model)
        }
        .onDisappear { model.endDaybookVoice(context) }
        #if os(macOS)
        .frame(minWidth: 460, minHeight: 370)
        #endif
    }

    @ViewBuilder private var answerContent: some View {
        if !live, let response, let item = privacy.items.first(where: { $0.id == response.id }) {
            if let text = item.answer { message(text, sources: item.sources) }
            PrivateAnswerActions(item: item, model: model, privacy: privacy, allowsDismissal: true)
        } else if let answer = live ? model.reply : response?.reply {
            message(answer.text, sources: answer.sources)
        }
    }

    private func message(_ text: String, sources: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            MessageBody(text)
            CitationButtons(sources: sources) { id in
                do { source = try await model.source(id) }
                catch { self.error = error.localizedDescription }
            }
        }
    }

    private func ask() async {
        let text = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !asking else { return }
        let id = lastRequest?.text == text ? lastRequest?.id ?? nexID() : nexID()
        lastRequest = (text, id)
        error = nil
        asking = true
        defer { asking = false }
        do {
            response = (id, try await model.ask(text, id: id, context: context))
            question = ""
            lastRequest = nil
        } catch { self.error = error.localizedDescription }
    }
}
