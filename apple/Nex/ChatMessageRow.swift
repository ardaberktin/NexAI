import SwiftUI

struct ChatMessageRow: View {
    let message: WorkspaceMessage
    let model: Controller
    let openGoal: (String) -> Void
    let openPDF: (NexArtifact) -> Void
    let openSource: (String) async -> Void
    let readAloud: () -> Void
    let reply: () -> Void
    #if os(iOS)
    let selectText: (String) -> Void
    #endif

    private var outgoing: Bool { message.role == .user }

    var body: some View {
        #if os(macOS)
        if outgoing {
            VStack(alignment: .trailing, spacing: 5) {
                contents
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(WorkspaceStyle.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
                footer
            }
            .frame(maxWidth: 560, alignment: .trailing)
            .padding(.leading, 40)
            .frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            HStack(alignment: .top, spacing: 11) {
                NexMark(size: 28)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Nex").font(.subheadline.weight(.semibold))
                    contents
                    footer
                }
            }
            .frame(maxWidth: 690, alignment: .leading)
            .padding(.trailing, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        #else
        VStack(alignment: outgoing ? .trailing : .leading, spacing: 5) {
            contents
            footer
        }
        .padding(.horizontal, 15)
        .padding(.vertical, 13)
        .background {
            RoundedRectangle(cornerRadius: 20).fill(
                outgoing ? WorkspaceStyle.accent.opacity(0.15) : Color.primary.opacity(0.055))
        }
        .contextMenu {
            if let text = message.copyableText {
                Button("Copy", systemImage: "document.on.document", action: copy)
                Button("Select text", systemImage: "selection.pin.in.out") { selectText(text) }
            }
            if message.offersReply {
                Button("Reply", systemImage: "arrowshape.turn.up.left", action: reply)
            }
        }
        .padding(outgoing ? .leading : .trailing, 28)
        .frame(maxWidth: .infinity, alignment: outgoing ? .trailing : .leading)
        #endif
    }

    private var contents: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title = message.title {
                Label(title, systemImage: "checkmark.square")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(WorkspaceStyle.accent)
            }
            if let item = message.privateRequest {
                Label(item.speaker == "recognized" ? "Private · Recognized voice" : "Private", systemImage: "lock.fill")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if message.privateNotice && message.text.isEmpty && message.privateRequest == nil {
                Label("Private inbox", systemImage: "lock.fill")
                    .font(.caption).foregroundStyle(.secondary)
            } else if !message.text.isEmpty {
                messageBody
                    .font(.body)
                    #if os(macOS)
                    .lineSpacing(2)
                    #endif
            }
            if let pdf = message.pdfAttachment {
                ChatPDFCard(artifact: pdf, loadPDF: { try await model.artifactPDF(pdf.id) }, open: { openPDF(pdf) })
                    .id(pdf.id)
            }
            if let clock = message.privateRequest?.clockAlert, model.workspaceFeatureEnabled(.goals) {
                ClockAlertCard(alert: clock) { try await model.cancelClock(clock.id) }
                    .id(clock.id)
            }
            if let goal = message.goalId, model.workspaceFeatureEnabled(.goals) {
                if let followUp = message.followUp {
                    GoalFollowUpChoices(followUp: followUp) { choice in
                        try await model.answerGoalFollowUp(goal, followUp: followUp.id, choice: choice)
                    }
                }
                Button("Open goal", systemImage: "arrow.up.right") { openGoal(goal) }
                    .font(.caption.weight(.semibold))
            }
            if message.role == .assistant {
                CitationButtons(sources: message.sources, open: openSource)
                    .font(.caption)
                if let item = message.privateRequest {
                    PrivateAnswerActions(item: item, model: model, privacy: model.privacy, allowsDismissal: false)
                }
            }
        }
        #if os(macOS)
        .buttonStyle(.borderless)
        .controlSize(.small)
        #endif
    }

    private var footer: some View {
        ChatMessageFooter(message: message, playing: model.chatSpeechID == message.id,
                          canRead: model.canStartChatSpeech, readAloud: readAloud, copy: copy, reply: reply)
    }

    private func copy() {
        guard let text = message.copyableText,
              !message.isPrivate || (model.privacy.unlocked && model.privacy.connected) else { return }
        copyChatText(text, privately: message.isPrivate)
    }

    @ViewBuilder private var messageBody: some View {
        #if os(macOS)
        let selectable = true
        #else
        let selectable = false
        #endif
        if outgoing {
            MessageBody(message.text, selectable: selectable)
        } else {
            MarkdownMessageBody(message.text, selectable: selectable)
        }
    }
}
