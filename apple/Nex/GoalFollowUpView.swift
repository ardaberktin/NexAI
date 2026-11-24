import SwiftUI

struct GoalFollowUpView: View {
    let followUp: GoalFollowUp
    let submit: (Int) async throws -> GoalFollowUp

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let question = followUp.question {
                Text("Follow-up").font(.headline)
                MarkdownMessageBody(question)
                GoalFollowUpChoices(followUp: followUp, submit: submit)
            } else {
                Label("Next follow-up · \(NexDate.display(followUp.due))", systemImage: "clock")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct GoalFollowUpChoices: View {
    let followUp: GoalFollowUp
    let submit: (Int) async throws -> GoalFollowUp
    @State private var receipt: GoalFollowUp?
    @State private var sending = false
    @State private var issue: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let label = followUp.selectedLabel ?? receipt?.selectedLabel {
                Label {
                    Text(verbatim: label)
                } icon: {
                    Image(systemName: "checkmark.circle")
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
            } else if followUp.canAnswer {
                ForEach(followUp.choices.indices, id: \.self) { index in
                    Button {
                        Task { await answer(index) }
                    } label: {
                        Text(verbatim: followUp.choices[index])
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .buttonStyle(.bordered)
                    .disabled(sending)
                }
                if let issue {
                    Text(issue).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func answer(_ index: Int) async {
        guard !sending, receipt == nil else { return }
        sending = true
        issue = nil
        defer { sending = false }
        do {
            receipt = try await submit(index)
        } catch is CancellationError { }
        catch { issue = error.localizedDescription }
    }
}
