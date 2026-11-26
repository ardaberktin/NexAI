import SwiftUI
import UserNotifications

struct GoalDetailView: View {
    @ObservedObject var model: Controller
    let id: String
    let back: () -> Void
    @State private var goal: NexGoal?
    @State private var edit: GoalDraft?
    @State private var failure: String?
    @State private var actionError: String?
    @State private var artifacts = false
    @State private var busy = false
    @State private var confirmingDeletion = false

    var body: some View {
        WorkspacePage(title: LocalizedStringKey(goal?.title ?? "Goal"),
                      subtitle: goal?.frequency.title ?? "", fullWidthTitle: true) {
            Button("Back to Goals", systemImage: "chevron.left", action: back)
                #if os(macOS)
                .help("Back to Goals")
                #else
                .labelStyle(.iconOnly)
                .buttonStyle(.bordered)
                .buttonBorderShape(.circle)
                .controlSize(.large)
                #endif
            #if os(iOS)
            Spacer()
            #endif
            if let goal { options(goal) }
        } content: {
            if let error = actionError ?? failure {
                WorkspaceIssue(message: error) {
                    actionError = nil
                    await load()
                }
            }
            if let goal {
                overview(goal)
                if let state = goal.planState,
                   !(state.milestones ?? []).isEmpty || !(state.scratchpad ?? []).isEmpty {
                    planSection(state)
                }
                if let followUp = goal.followUp {
                    GoalFollowUpView(followUp: followUp) { choice in
                        try await model.answerGoalFollowUp(id, followUp: followUp.id, choice: choice)
                    }
                    .id(followUp.id)
                }
                if goal.status == .active && (goal.nextRun != nil || goal.followUp != nil) {
                    GoalNotificationAccess(model: model)
                }
                Divider()
                Button { artifacts = true } label: {
                    Label("Artifacts", systemImage: "doc.text")
                    Spacer()
                    Text((goal.artifactCount ?? goal.activity?.filter(\.artifact).count ?? 0).formatted()).foregroundStyle(.secondary)
                    Image(systemName: "chevron.right").foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                Divider()
                activity((goal.activity ?? []).filter { $0.id != goal.followUp?.id })
            } else if failure == nil {
                ProgressView("Opening goal…").frame(maxWidth: .infinity)
            }
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                do { try await Task.sleep(for: .seconds(7)) }
                catch { return }
            }
        }
        .sheet(item: $edit, onDismiss: { Task { await load() } }) { draft in
            GoalEditor(model: model, draft: draft) { _ in Task { await load() } }
        }
        .sheet(isPresented: $artifacts) {
            WorkspaceSheet(title: "Goal artifacts") { GoalArtifactList(model: model, goal: id) }
        }
        .alert("Delete goal?", isPresented: $confirmingDeletion, presenting: goal) { goal in
            Button("Delete goal", role: .destructive) {
                perform(returnToGoals: true) { try await model.deleteGoal(goal) }
            }
            Button("Cancel", role: .cancel) { }
        } message: { goal in
            Text("“\(goal.title)” and its activity and artifacts will be permanently deleted.")
        }
    }

    private func options(_ goal: NexGoal) -> some View {
        Menu {
            if goal.status == .active {
                Button("Run now", systemImage: "arrow.clockwise") {
                    perform(goal, "check")
                }
                Button("Pause", systemImage: "pause") { perform(goal, "paused") }
            } else {
                Button("Resume", systemImage: "play") { perform(goal, "active") }
            }
            if goal.status != .completed {
                Button("Mark complete", systemImage: "checkmark") { perform(goal, "completed") }
            }
            Button("Delete goal", systemImage: "trash", role: .destructive) { confirmingDeletion = true }
        } label: {
            Image(systemName: "ellipsis.circle")
                #if os(iOS)
                .frame(width: 35, height: 40)
                #endif
        }
        #if os(macOS)
        .help("Goal options")
        #endif
        .menuOrder(.fixed)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(busy)
        .accessibilityLabel("Goal options")
    }

    private func overview(_ goal: NexGoal) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(goal.status.rawValue.capitalized)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(WorkspaceStyle.accent)
                    .padding(.horizontal, 9).padding(.vertical, 5)
                    .background(WorkspaceStyle.accent.opacity(0.12), in: Capsule())
                Spacer()
                Button("Edit goal", systemImage: "pencil") { edit = GoalDraft(goal) }
                    .buttonStyle(.bordered)
                    .disabled(busy)
            }
            Text(goal.prompt).textSelection(.enabled)
            if let error = goal.error { WorkspaceIssue(message: error) }
            if let date = goal.nextRun, date != goal.followUp?.due {
                Label("Next run · \(NexDate.display(date))", systemImage: "clock")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if let date = goal.lastChecked {
                Label("Last run · \(NexDate.display(date))", systemImage: "checkmark.circle")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private func planSection(_ state: GoalPlanState) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Plan & Progress").font(.headline).accessibilityAddTraits(.isHeader)
                Spacer()
                if let phase = state.phase, !phase.isEmpty {
                    Text(phase.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(state.milestones ?? []) { step in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: milestoneSymbol(step.status))
                        .foregroundStyle(step.status == "done" ? WorkspaceStyle.accent : .secondary)
                        .padding(.top, 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(step.title).font(.subheadline.weight(.medium))
                        if let note = step.note, !note.isEmpty {
                            Text(note).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if let notes = state.scratchpad, !notes.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Working notes").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(notes, id: \.self) { note in
                        Text("• \(note)").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 2)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
    }

    private func milestoneSymbol(_ status: String) -> String {
        switch status {
        case "done": "checkmark.circle.fill"
        case "in_progress": "circle.inset.filled"
        case "blocked": "exclamationmark.circle"
        default: "circle"
        }
    }

    private func activity(_ entries: [GoalActivity]) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Activity").font(.headline).accessibilityAddTraits(.isHeader)
            if entries.isEmpty { Text("Updates will appear here.").foregroundStyle(.secondary) }
            ForEach(entries) { item in
                HStack(alignment: .top, spacing: 13) {
                    Image(systemName: item.artifact ? "circle.inset.filled" : "circle")
                        .font(.caption2)
                        .foregroundStyle(item.artifact ? WorkspaceStyle.accent : Color.secondary)
                        .padding(.top, 5)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(item.title).font(.subheadline.weight(.semibold))
                        if !item.detail.isEmpty {
                            Text(item.detail).font(.subheadline).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                        Text(NexDate.display(item.created)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private func load() async {
        do {
            let result = try await model.goal(id)
            guard !Task.isCancelled, !busy else { return }
            goal = result
            failure = nil
        } catch NexError.api(404, let message) {
            guard !Task.isCancelled, !busy else { return }
            goal = nil
            failure = message
        } catch {
            if !Task.isCancelled { failure = error.localizedDescription }
        }
    }

    private func perform(_ goal: NexGoal, _ action: String) {
        perform { _ = try await model.goalAction(goal, action: action) }
    }

    private func perform(returnToGoals: Bool = false, _ action: @escaping () async throws -> Void) {
        Task {
            guard !busy else { return }
            busy = true
            actionError = nil
            do {
                try await action()
                if returnToGoals {
                    busy = false
                    back()
                    return
                }
                if let goals = try? await model.goals() { await GoalNotifications.sync(goals, using: model) }
            } catch { actionError = error.localizedDescription }
            busy = false
            await load()
        }
    }
}

private struct GoalNotificationAccess: View {
    @Environment(\.scenePhase) private var scene
    @ObservedObject var model: Controller
    @State private var status: UNAuthorizationStatus?

    var body: some View {
        Group {
            if status == .notDetermined {
                Button("Enable device notifications", systemImage: "bell") {
                    Task {
                        await GoalNotifications.enable(using: model)
                        await refresh()
                    }
                }
                .font(.subheadline.weight(.semibold))
            } else if status == .denied {
                WorkspaceIssue(message: "Allow Nex notifications in this device’s settings to receive goal alerts.")
            }
        }
        .task { await refresh() }
        .onChange(of: scene) { _, phase in
            if phase == .active { Task { await refresh() } }
        }
    }

    private func refresh() async {
        status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }
}
