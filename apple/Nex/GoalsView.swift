import SwiftUI

struct GoalsView: View {
    @ObservedObject var model: Controller
    let open: (String) -> Void
    let create: () -> Void
    @State private var goals: [NexGoal]?
    @State private var clocks: [ClockAlert] = []
    @State private var error: String?
    @State private var changing: Set<String> = []
    @State private var showCompletedGoals = false
    @State private var showCompletedClocks = false

    var body: some View {
        WorkspacePage(title: "Goals", subtitle: "Tell Nex what to do and when to do it") {
            Button("New goal", systemImage: "plus", action: create)
                #if os(macOS)
                .help("New goal")
                #else
                .buttonStyle(.borderedProminent)
                #endif
        } content: {
            if let error { WorkspaceIssue(message: error, retry: load) }
            if let goals {
                if goals.isEmpty && clocks.isEmpty {
                    WorkspaceEmpty(title: "What should Nex do?", symbol: "checkmark.square",
                                   detail: "Try “Give me today’s calendar, tasks, and weather in a short briefing.”")
                    Button("Create a goal", systemImage: "plus", action: create)
                        .buttonStyle(.borderedProminent)
                        .frame(maxWidth: .infinity)
                } else {
                    goalSections(goals)
                }
            } else if error == nil {
                ProgressView("Opening Goals…").frame(maxWidth: .infinity).padding(.vertical, 30)
            }
            if !clocks.isEmpty { clockSection }
        }
        .refreshable { await load() }
        .task {
            while !Task.isCancelled {
                await load()
                do { try await Task.sleep(for: .seconds(6)) }
                catch { return }
            }
        }
    }

    private func goalSections(_ goals: [NexGoal]) -> some View {
        let completed = goals.filter { $0.status == .completed }
        return VStack(alignment: .leading, spacing: 22) {
            goalSection("Active", symbol: "target", goals: goals.filter { $0.status == .active })
            goalSection("Paused", symbol: "pause.circle", goals: goals.filter { $0.status == .paused })
            if !completed.isEmpty {
                DisclosureGroup(isExpanded: $showCompletedGoals) {
                    goalRows(completed).padding(.top, 4)
                } label: {
                    sectionLabel("Completed", symbol: "checkmark.circle")
                        .accessibilityLabel("Completed goals")
                }
            }
        }
    }

    private var clockSection: some View {
        let now = Date.now
        let deadlines = clocks.compactMap { $0.status == .scheduled ? $0.due : nil }
            .filter { $0 > now }.sorted()
        return TimelineView(.explicit([now] + deadlines)) { context in
            let groups = ClockAlert.groupedByCompletion(clocks, at: context.date)
            VStack(alignment: .leading, spacing: 8) {
                sectionLabel("Alarms & timers", symbol: "clock")
                ForEach(groups.scheduled) { clock in
                    ClockAlertCard(alert: clock) { try await cancelClock(clock) }
                }
                if !groups.completed.isEmpty {
                    DisclosureGroup(isExpanded: $showCompletedClocks) {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(groups.completed) { ClockAlertCard(alert: $0) }
                        }
                        .padding(.top, 4)
                    } label: {
                        sectionLabel("Completed", symbol: "checkmark.circle")
                            .accessibilityLabel("Completed alarms and timers")
                    }
                }
            }
        }
    }

    private func sectionLabel(_ title: LocalizedStringKey, symbol: String) -> some View {
        Label(title, systemImage: symbol)
            .font(.subheadline.weight(.bold))
            .foregroundStyle(WorkspaceStyle.accent)
            .accessibilityAddTraits(.isHeader)
    }

    @ViewBuilder private func goalSection(_ title: LocalizedStringKey, symbol: String, goals: [NexGoal]) -> some View {
        if !goals.isEmpty {
            VStack(alignment: .leading, spacing: 9) {
                sectionLabel(title, symbol: symbol)
                goalRows(goals)
            }
        }
    }

    private func goalRows(_ goals: [NexGoal]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(goals) { goal in
                goalRow(goal)
                Divider().opacity(0.6)
            }
        }
    }

    private func goalRow(_ goal: NexGoal) -> some View {
        HStack(alignment: .top, spacing: 8) {
            let completed = goal.status == .completed
            Button(completed ? "Reopen goal" : "Complete goal",
                   systemImage: completed ? "checkmark.square.fill" : "square") {
                Task { await change(goal, completed ? "active" : "completed") }
            }
            .labelStyle(.iconOnly)
            .font(.title3)
            .buttonStyle(.plain)
            .frame(width: 38, height: 38)
            .contentShape(Rectangle())
            .foregroundStyle(WorkspaceStyle.accent)
            .accessibilityHint(goal.title)
            .disabled(changing.contains(goal.id))
            Button { open(goal.id) } label: {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(goal.title).font(.headline)
                        Text(goal.error ?? goal.latest ?? summary(goal))
                            .font(.subheadline)
                            .foregroundStyle(goal.error == nil ? Color.secondary : Color.orange)
                            .lineLimit(3)
                    }
                    Spacer(minLength: 6)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.vertical, 8)
        }
        .padding(.vertical, 4)
    }

    private func summary(_ goal: NexGoal) -> String {
        if let time = goal.nextRun { return "\(goal.frequency.title) · \(NexDate.display(time))" }
        return goal.frequency.title
    }

    private func load() async {
        do {
            let result = try await model.goalOverview(includeClocks: true)
            guard !Task.isCancelled, changing.isEmpty else { return }
            goals = result.items
            clocks = result.clockAlerts ?? []
            error = nil
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }

    private func cancelClock(_ clock: ClockAlert) async throws -> ClockAlert {
        let updated = try await model.cancelClock(clock.id)
        if let index = clocks.firstIndex(where: { $0.id == updated.id }) { clocks[index] = updated }
        return updated
    }

    private func change(_ goal: NexGoal, _ action: String) async {
        guard changing.insert(goal.id).inserted else { return }
        do {
            let updated = try await model.goalAction(goal, action: action)
            if let index = goals?.firstIndex(where: { $0.id == goal.id }) { goals?[index] = updated }
            if let goals { await GoalNotifications.sync(goals, using: model) }
            error = nil
        } catch { self.error = error.localizedDescription }
        changing.remove(goal.id)
    }
}
