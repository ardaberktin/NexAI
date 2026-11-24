import SwiftUI

struct ChatMessageRow: View {
    let message: WorkspaceMessage
    let model: Controller
    let openGoal: (String) -> Void
    let openPDF: (NexArtifact) -> Void
    let openSource: (String) async -> Void
    let readAloud: () -> Void
    let canRead: Bool

    private var outgoing: Bool { message.role == .user }

    var body: some View {
        #if os(macOS)
        if outgoing {
            contents
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(WorkspaceStyle.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 14))
                .frame(maxWidth: 560, alignment: .trailing)
                .padding(.leading, 40)
                .frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            HStack(alignment: .top, spacing: 11) {
                NexMark(size: 28)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Nex").font(.subheadline.weight(.semibold))
                    contents
                }
            }
            .frame(maxWidth: 690, alignment: .leading)
            .padding(.trailing, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        #else
        contents
            .padding(.horizontal, 15)
            .padding(.vertical, 13)
            .background {
                RoundedRectangle(cornerRadius: 20).fill(
                    outgoing ? WorkspaceStyle.accent.opacity(0.15) : Color.primary.opacity(0.055))
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
                } else if message.offersReadAloud {
                    Button("Read aloud", systemImage: "speaker.wave.2", action: readAloud)
                        .font(.caption)
                        .disabled(!canRead)
                }
            }
        }
        #if os(macOS)
        .buttonStyle(.borderless)
        .controlSize(.small)
        #endif
    }

    @ViewBuilder private var messageBody: some View {
        if outgoing {
            MessageBody(message.text)
        } else {
            MarkdownMessageBody(message.text)
        }
    }
}
