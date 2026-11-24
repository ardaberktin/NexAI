import SwiftUI

struct WatchGoalsView: View {
    @State private var revision = 0
    @State private var showCompletedGoals = false
    @State private var showCompletedClocks = false

    var body: some View {
        WatchPage(title: "Goals", key: String(revision), request: WatchRequest(.goals), refreshInterval: .seconds(12)) {
            (result: GoalOverview) in
            let clocks = result.clockAlerts ?? []
            NavigationLink(destination: WatchGoalEditor(draft: GoalDraft())) {
                Label("New goal", systemImage: "plus")
            }
            if result.items.isEmpty && clocks.isEmpty {
                Text("No goals yet.").foregroundStyle(.secondary)
            }
            goalRows(result.items.filter { $0.status != .completed })
            let completed = result.items.filter { $0.status == .completed }
            if !completed.isEmpty {
                completedSection("Completed goals", isExpanded: $showCompletedGoals) {
                    goalRows(completed)
                }
            }
            if !clocks.isEmpty { clockSection(clocks) }
        }
    }

    private func goalRows(_ goals: [NexGoal]) -> some View {
        ForEach(goals) { goal in
            NavigationLink {
                WatchGoalView(id: goal.id) { revision += 1 }
            } label: {
                WatchTimelineRow(title: goal.title, subtitle: goal.status.rawValue.capitalized)
            }
        }
    }

    private func clockSection(_ clocks: [ClockAlert]) -> some View {
        let groups = ClockAlert.groupedByCompletion(clocks, at: .now)
        return Section("Alarms & timers") {
            ForEach(groups.scheduled) { ClockAlertCard(alert: $0) }
            if !groups.completed.isEmpty {
                completedSection("Completed alarms and timers", isExpanded: $showCompletedClocks) {
                    ForEach(groups.completed) { ClockAlertCard(alert: $0) }
                }
            }
        }
    }

    @ViewBuilder private func completedSection<Content: View>(
        _ accessibilityTitle: LocalizedStringKey,
        isExpanded: Binding<Bool>,
        @ViewBuilder content: () -> Content
    ) -> some View {
        Button {
            isExpanded.wrappedValue.toggle()
        } label: {
            HStack {
                Text("Completed")
                Spacer(minLength: 4)
                Image(systemName: isExpanded.wrappedValue ? "chevron.down" : "chevron.right")
                    .accessibilityHidden(true)
            }
        }
        .accessibilityLabel(accessibilityTitle)
        .accessibilityValue(isExpanded.wrappedValue ? Text("Expanded") : Text("Collapsed"))
        if isExpanded.wrappedValue { content() }
    }
}

struct WatchGoalView: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @Environment(\.dismiss) private var dismiss
    let id: String
    var didDelete: (() -> Void)?
    @State private var revision = 0
    @State private var issue: String?
    @State private var running = false
    @State private var confirmingDeletion = false

    var body: some View {
        WatchPage(title: "Goal", key: String(revision), request: WatchRequest(.goal, id: id),
                  refreshInterval: .seconds(12)) { (goal: NexGoal) in
            Text(goal.title).font(.headline)
            Text(goal.prompt)
            if let latest = goal.latest { Text(latest) }
            if let run = goal.nextRun, run != goal.followUp?.due {
                Text("Next run").font(.caption2).foregroundStyle(.secondary)
                Text(NexDate.display(run)).font(.footnote)
            }
            if let followUp = goal.followUp {
                GoalFollowUpView(followUp: followUp) { choice in
                    try await workspace.answerGoalFollowUp(id, followUp: followUp.id, choice: choice)
                }
                .id(followUp.id)
            }
            if let issue = issue ?? goal.error { WatchFailure(message: issue) }
            NavigationLink("Edit goal", destination: WatchGoalEditor(draft: GoalDraft(goal)) { revision += 1 })
            NavigationLink("Files", destination: WatchFilesView(goal: id))
            if goal.status == .active {
                action("Run now", mode: "check", goal: goal)
                action("Pause", mode: "paused", goal: goal)
            } else {
                action("Resume", mode: "active", goal: goal)
            }
            if goal.status != .completed { action("Mark complete", mode: "completed", goal: goal) }
            deleteButton(goal)
            ForEach((goal.activity ?? []).filter { $0.id != goal.followUp?.id }) { item in
                NavigationLink {
                    WatchReadingView(title: item.title, detail: item.detail, subtitle: NexDate.display(item.created))
                } label: {
                    WatchTimelineRow(title: item.title, subtitle: NexDate.display(item.created))
                }
            }
        }
    }

    private func action(_ title: LocalizedStringKey, mode: String, goal: NexGoal) -> some View {
        Button(title) {
            perform(mode, goal: goal)
        }
        .disabled(running)
    }

    private func deleteButton(_ goal: NexGoal) -> some View {
        Button("Delete goal", systemImage: "trash", role: .destructive) { confirmingDeletion = true }
            .disabled(running)
            .alert("Delete goal?", isPresented: $confirmingDeletion) {
                Button("Delete goal", role: .destructive) { perform("delete", goal: goal) }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text("“\(goal.title)” and its activity and artifacts will be permanently deleted.")
            }
    }

    private func perform(_ mode: String, goal: NexGoal) {
        Task {
            guard !running else { return }
            running = true
            issue = nil
            defer { running = false }
            do {
                let body = try JSONEncoder().encode(GoalActionRequest(action: mode, revision: goal.revision))
                let request = WatchRequest(.goalAction, id: id, body: body)
                if mode == "delete" {
                    let _: GoalDeletion = try await workspace.get(request)
                    dismiss()
                    didDelete?()
                    return
                }
                let _: NexGoal = try await workspace.get(request)
            } catch is CancellationError { return }
            catch { issue = error.localizedDescription }
            revision += 1
        }
    }
}

struct WatchGoalEditor: View {
    @EnvironmentObject private var workspace: WatchWorkspace
    @Environment(\.dismiss) private var dismiss
    @State var draft: GoalDraft
    var didSave: (() -> Void)?
    @State private var saving = false
    @State private var issue: String?

    var body: some View {
        List {
            TextField("What should Nex track?", text: $draft.prompt)
            Picker("Schedule", selection: $draft.frequency) {
                ForEach(GoalFrequency.allCases) { Text($0.title).tag($0) }
            }
            if draft.frequency != .onDemand {
                WatchGoalSchedule(date: $draft.date, zone: draft.zone)
            }
            Button(saving ? "Saving…" : "Save goal") { Task { await save() } }
                .disabled(saving || draft.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            if let issue { WatchFailure(message: issue) }
        }
        .navigationTitle("Edit goal")
        .modifier(WatchPrivateScreen())
    }

    private func save() async {
        saving = true
        issue = nil
        defer { saving = false }
        do {
            let _: NexGoal = try await workspace.get(WatchRequest(.goalEdit, id: draft.id,
                                                        body: JSONEncoder().encode(draft)))
            didSave?()
            dismiss()
        } catch is CancellationError { }
        catch { issue = error.localizedDescription }
    }
}

private struct WatchGoalSchedule: View {
    @Binding var date: Date
    let zone: String

    var body: some View {
        Text(date.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened, timeZone: calendar.timeZone)))
            .font(.caption)
        Picker("Date", selection: selectedDay) {
            ForEach(days, id: \.self) { day in
                Text(day.formatted(Date.FormatStyle(date: .abbreviated, time: .omitted, timeZone: calendar.timeZone)))
                    .tag(day)
            }
        }
        Picker("Hour", selection: number(.hour)) { ForEach(0..<24) { Text($0.formatted()).tag($0) } }
        Picker("Minute", selection: number(.minute)) {
            ForEach(0..<60) { Text(String(format: "%02d", $0)).tag($0) }
        }
    }

    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: zone) ?? .current
        return calendar
    }

    private var days: [Date] {
        let today = calendar.startOfDay(for: .now)
        let selected = calendar.startOfDay(for: date)
        let upcoming = (0..<31).compactMap { calendar.date(byAdding: .day, value: $0, to: today) }
        return Array(Set(upcoming + [selected])).sorted()
    }

    private var selectedDay: Binding<Date> {
        Binding(get: { calendar.startOfDay(for: date) }, set: { day in
            let parts = calendar.dateComponents([.hour, .minute], from: date)
            date = calendar.date(bySettingHour: parts.hour ?? 0, minute: parts.minute ?? 0, second: 0, of: day) ?? day
        })
    }

    private func number(_ component: Calendar.Component) -> Binding<Int> {
        Binding(get: { calendar.component(component, from: date) }, set: { value in
            var parts = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: date)
            if component == .hour { parts.hour = value } else { parts.minute = value }
            date = calendar.date(from: parts) ?? date
        })
    }
}
