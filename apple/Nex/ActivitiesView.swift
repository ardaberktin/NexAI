import SwiftUI

struct ActivitiesView: View {
    @ObservedObject var model: Controller
    @Environment(\.scenePhase) private var scene
    @Binding var date: Date
    @State private var day: ActivityDay?
    @State private var error: String?
    @State private var selected: RecordedActivity?
    @State private var device: String?

    private var context: DaybookContext {
        DaybookContext(date: date, timeZone: TimeZone(identifier: NexDate.timeZone) ?? .gmt)
    }
    private var items: [RecordedActivity] {
        (day?.items ?? []).filter { device == nil || $0.device == device }
    }

    var body: some View {
        WorkspacePage(title: "Activities", subtitle: "") {
            DatePicker("Activity date", selection: $date, in: ...Date.now, displayedComponents: .date)
                .labelsHidden().fixedSize()
        } content: {
            if let error { WorkspaceIssue(message: error) { await load(context) } }
            if let day {
                let periods = items
                summary(periods)
                timeline(day, periods: periods)
            } else if error == nil {
                ProgressView("Loading activities…").frame(maxWidth: .infinity)
            }
        }
        .refreshable { await load(context) }
        .task(id: context) {
            let selectedDay = context
            day = nil
            error = nil
            await load(selectedDay)
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(12)) } catch { return }
                if scene == .active { await load(selectedDay) }
            }
        }
        .sheet(item: $selected) { activity in
            ActivityDetailView(model: model, activity: activity, day: context)
        }
    }

    @ViewBuilder private func summary(_ periods: [RecordedActivity]) -> some View {
        if let latest = periods.max(by: { $0.ended < $1.ended }) {
            latestObservation(latest)
            let coverage = ActivityCoverage(periods)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 18) { totals(coverage) }
                VStack(alignment: .leading, spacing: 5) { totals(coverage) }
            }
            .font(.subheadline).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private func timeline(_ day: ActivityDay, periods: [RecordedActivity]) -> some View {
        HStack {
            Text("Timeline").font(.title2.weight(.semibold))
            Spacer()
            Picker("Recording device", selection: $device) {
                Text("All devices").tag(String?.none)
                Text("Mac").tag(String?.some("mac"))
                Text("iPhone").tag(String?.some("iphone"))
                Text("Apple Watch").tag(String?.some("watch"))
            }
            .pickerStyle(.menu).fixedSize()
        }
        if day.pending > 0 {
            Label("\(day.pending) awaiting recognition", systemImage: "waveform")
                .font(.caption).foregroundStyle(.secondary)
        }
        if periods.isEmpty {
            WorkspaceEmpty(
                title: "No activity observations", symbol: "waveform.path",
                detail: "Activities appear here as Nex processes recordings for this date and device.")
        } else {
            LazyVStack(spacing: 8) {
                ForEach(periods.reversed()) { activity in
                    ActivityRow(activity: activity) { selected = activity }
                }
            }
        }
    }

    private func latestObservation(_ activity: RecordedActivity) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Latest observation", systemImage: activity.symbol)
                .font(.subheadline.weight(.semibold)).foregroundStyle(activity.tint)
            Text(activity.title).font(.system(.title, design: .rounded, weight: .bold))
            Text(activity.detail).font(.body).textSelection(.enabled)
            Text("\(activity.timeRange) · \(activity.deviceName) · \(activity.confidence)")
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(activity.tint.opacity(0.09), in: RoundedRectangle(cornerRadius: 20))
    }

    @ViewBuilder private func totals(_ coverage: ActivityCoverage) -> some View {
        let time = coverage.duration.formatted(.units(allowed: [.hours, .minutes, .seconds], maximumUnitCount: 2))
        Label("\(time) observed", systemImage: "clock")
        Label("\(coverage.kinds) activity types", systemImage: "square.grid.2x2")
    }

    private func load(_ selectedDay: DaybookContext) async {
        do {
            let result = try await model.activities(selectedDay)
            guard !Task.isCancelled, selectedDay == context else { return }
            day = result
            error = nil
        } catch {
            if !Task.isCancelled, selectedDay == context { self.error = error.localizedDescription }
        }
    }
}
