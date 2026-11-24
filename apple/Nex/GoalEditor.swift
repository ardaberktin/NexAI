import SwiftUI

struct GoalEditor: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: Controller
    @State var draft: GoalDraft
    let didSave: (NexGoal) -> Void
    @State private var error: String?
    @State private var busy = false

    private var editing: Bool { draft.revision != nil }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Tell Nex what to do", text: $draft.prompt, axis: .vertical)
                        .lineLimit(3...8)
                        .accessibilityLabel("Prompt")
                } header: {
                    Text("Prompt")
                }
                Section {
                    Picker("Frequency", selection: $draft.frequency) {
                        ForEach(GoalFrequency.allCases) { Text(LocalizedStringKey($0.title)).tag($0) }
                    }
                    if draft.frequency != .onDemand {
                        DatePicker("Time", selection: $draft.date)
                            .environment(\.timeZone, TimeZone(identifier: draft.zone) ?? .gmt)
                    }
                }
                if let error { WorkspaceIssue(message: error) }
            }
            .formStyle(.grouped)
            .onChange(of: draft.frequency) { _, frequency in
                if frequency == .once, draft.date <= .now {
                    draft.date = GoalDraft.defaultTime(in: draft.zone)
                }
            }
            .navigationTitle(editing ? "Edit goal" : "Create a goal")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(busy)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(editing ? "Save changes" : "Create goal") { Task { await save() } }
                        .disabled(busy || draft.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 525, minHeight: 375)
        #endif
    }

    private func save() async {
        busy = true
        defer { busy = false }
        do {
            let goal = try await model.saveGoal(draft)
            didSave(goal)
            dismiss()
            Task {
                if goal.status == .active, goal.nextRun != nil {
                    await GoalNotifications.enable(using: model)
                } else if let goals = try? await model.goals() {
                    await GoalNotifications.sync(goals, using: model)
                }
            }
        } catch { self.error = error.localizedDescription }
    }
}
