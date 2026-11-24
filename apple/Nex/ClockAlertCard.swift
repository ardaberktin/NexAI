import SwiftUI

struct ClockAlertCard: View {
    let alert: ClockAlert
    var cancel: (() async throws -> ClockAlert)?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var cancelled = false
    @State private var cancelling = false
    @State private var error: String?

    var body: some View {
        if let due = alert.due {
            if alert.status == .scheduled && !cancelled {
                TimelineView(.periodic(from: .now, by: alert.kind == .timer ? 1 : 5)) { context in
                    card(status: alert.status(at: context.date), due: due, now: context.date)
                }
            } else {
                card(status: cancelled ? .cancelled : alert.status, due: due, now: .now)
            }
        }
    }

    private var tint: Color {
        alert.kind == .timer ? .accentColor : .orange
    }

    private func card(status: ClockAlert.Status, due: Date, now: Date) -> some View {
        let remaining = max(0, Int(ceil(due.timeIntervalSince(now))))
        let timeZone = TimeZone(identifier: alert.zone) ?? .current
        let time: Date.FormatStyle.TimeStyle = status == .scheduled && alert.kind == .alarm ? .omitted : .shortened
        let deadline = due.formatted(Date.FormatStyle(date: .abbreviated, time: time, timeZone: timeZone))
        return VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 12) {
                dial(status: status, due: due, now: now)
                VStack(alignment: .leading, spacing: 3) {
                    Text(alert.kind == .timer ? "Timer" : "Alarm")
                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    if !alert.label.isEmpty {
                        Text(verbatim: alert.label).font(.subheadline.weight(.semibold))
                    }
                    if status == .scheduled && alert.kind == .timer {
                        Text(countdown(remaining))
                            .font(.system(.title2, design: .rounded, weight: .semibold)).monospacedDigit()
                            .contentTransition(.numericText(countsDown: true))
                            .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: remaining)
                            .accessibilityLabel(Duration.seconds(remaining).formatted(
                                .units(allowed: [.hours, .minutes, .seconds])))
                    } else {
                        statusLabel(status, due: due, timeZone: timeZone)
                    }
                    Text(deadline).font(.caption).foregroundStyle(.secondary)
                    if status == .scheduled && alert.iphoneAlarmScheduled == true {
                        Text("iPhone alarm scheduled").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .accessibilityElement(children: .combine)
                Spacer(minLength: 0)
                if status == .scheduled, let cancel {
                    Button {
                        Task { await cancelAlert(cancel) }
                    } label: {
                        Image(systemName: "xmark.circle").font(.title3)
                            .foregroundStyle(.secondary)
                            .frame(width: 32, height: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Cancel alert")
                    .disabled(cancelling)
                }
            }
            if let error { Text(error).font(.caption).foregroundStyle(.secondary) }
        }
        .padding(12)
        .frame(maxWidth: 420, alignment: .leading)
        .background(tint.opacity(0.09), in: RoundedRectangle(cornerRadius: 16))
        .overlay { RoundedRectangle(cornerRadius: 16).strokeBorder(tint.opacity(0.2)) }
    }

    private func dial(status: ClockAlert.Status, due: Date, now: Date) -> some View {
        let symbol = switch status {
        case .scheduled: alert.kind.symbol
        case .fired: "checkmark"
        case .cancelled: "xmark"
        }
        return ZStack {
            Circle().stroke(tint.opacity(0.16), lineWidth: 3)
            if alert.kind == .timer && status == .scheduled, let started = WorkspaceDate.instant(alert.createdAt) {
                let elapsed = now.timeIntervalSince(started) / max(1, due.timeIntervalSince(started))
                let remaining = min(1, max(0, 1 - elapsed))
                Circle().trim(from: 0, to: remaining)
                    .stroke(tint, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                    .animation(reduceMotion ? nil : .linear(duration: 1), value: remaining)
            }
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .semibold))
                .contentTransition(.symbolEffect(.replace))
                .animation(reduceMotion ? nil : .easeOut, value: symbol)
        }
        .foregroundStyle(tint)
        .frame(width: 43, height: 43)
        .accessibilityHidden(true)
    }

    @ViewBuilder private func statusLabel(_ status: ClockAlert.Status, due: Date, timeZone: TimeZone) -> some View {
        Group {
            switch status {
            case .scheduled: Text(due.formatted(Date.FormatStyle(date: .omitted, time: .shortened, timeZone: timeZone)))
            case .fired: Text(alert.kind == .timer ? "Finished" : "Went off")
            case .cancelled: Text("Cancelled")
            }
        }
        .font(.system(.title3, design: .rounded, weight: .semibold))
    }

    private func countdown(_ seconds: Int) -> String {
        let minutes = seconds / 60
        if minutes < 60 { return String(format: "%02d:%02d", minutes, seconds % 60) }
        return String(format: "%d:%02d:%02d", minutes / 60, minutes % 60, seconds % 60)
    }

    private func cancelAlert(_ action: () async throws -> ClockAlert) async {
        cancelling = true
        error = nil
        defer { cancelling = false }
        do { cancelled = try await action().status == .cancelled }
        catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }
}
