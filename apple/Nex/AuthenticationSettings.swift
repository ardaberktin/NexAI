import SwiftUI

struct AuthenticationSettings: View {
    private static let presets = [15, 30, 60, 5 * 60, 15 * 60, 30 * 60, 60 * 60]
    private static let custom = -2

    @ObservedObject var autoLock: AppAutoLock
    let lock: () -> Void
    @State private var editingDelay = false

    var body: some View {
        Section {
            Picker("Require Biometrics", selection: selection) {
                Text("Immediately").tag(ReauthenticationDelay.immediately.seconds)
                ForEach(Self.presets, id: \.self) { seconds in
                    Text(Self.durationLabel(seconds)).tag(seconds)
                }
                if isCustom { Text(Self.durationLabel(autoLock.delay.seconds)).tag(autoLock.delay.seconds) }
                Text("Custom…").tag(Self.custom)
                Text("No Away Timer").tag(ReauthenticationDelay.noAwayTimer.seconds)
            }
            .pickerStyle(.menu)
            Button("Lock Nex Now", systemImage: "lock", action: lock)
        } header: {
            Text("Authentication")
        } footer: {
            Text("The timer starts when you leave Nex. New launches and expired Excalibur sessions require biometrics.")
            #if os(macOS)
            Text("Locking your Mac also locks Nex.")
            #endif
        }
        .sheet(isPresented: $editingDelay) {
            CustomReauthenticationDelay(seconds: autoLock.delay.seconds) { autoLock.setDelay($0) }
        }
    }

    private var isCustom: Bool {
        autoLock.delay.seconds > 0 && !Self.presets.contains(autoLock.delay.seconds)
    }

    private var selection: Binding<Int> {
        Binding {
            autoLock.delay.seconds
        } set: { seconds in
            if seconds == Self.custom { editingDelay = true }
            else { autoLock.setDelay(ReauthenticationDelay(seconds: seconds)) }
        }
    }

    private static func durationLabel(_ seconds: Int) -> String {
        let formatter = DateComponentsFormatter()
        formatter.allowedUnits = [.hour, .minute, .second]
        formatter.unitsStyle = .full
        return formatter.string(from: TimeInterval(seconds)) ?? seconds.formatted()
    }
}

private struct CustomReauthenticationDelay: View {
    @Environment(\.dismiss) private var dismiss
    @State private var hours: String
    @State private var minutes: String
    @State private var seconds: String
    let save: (ReauthenticationDelay) -> Void

    init(seconds: Int, save: @escaping (ReauthenticationDelay) -> Void) {
        let initial = seconds > 0 ? seconds : 60
        _hours = State(initialValue: String(initial / 3600))
        _minutes = State(initialValue: String(initial / 60 % 60))
        _seconds = State(initialValue: String(initial % 60))
        self.save = save
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Custom Reauthentication Delay").font(.headline)
            VStack(spacing: 10) {
                field("Hours", value: $hours)
                field("Minutes", value: $minutes)
                field("Seconds", value: $seconds)
            }
            Text("Choose a delay from 1 second to 12 hours.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    guard let delay else { return }
                    save(delay)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(delay == nil)
            }
        }
        .padding(24)
        #if os(macOS)
        .frame(width: 360)
        #else
        .presentationDetents([.medium, .large])
        #endif
    }

    private func field(_ label: LocalizedStringKey, value: Binding<String>) -> some View {
        LabeledContent(label) {
            TextField(label, text: value)
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 72)
                #if os(iOS)
                .keyboardType(.numberPad)
                #endif
        }
    }

    private var delay: ReauthenticationDelay? {
        let maximumHours = ReauthenticationDelay.maximumSeconds / 3600
        guard let hours = Int(hours.isEmpty ? "0" : hours), (0...maximumHours).contains(hours),
              let minutes = Int(minutes.isEmpty ? "0" : minutes), (0...59).contains(minutes),
              let seconds = Int(seconds.isEmpty ? "0" : seconds), (0...59).contains(seconds) else { return nil }
        let total = hours * 3600 + minutes * 60 + seconds
        guard (1...ReauthenticationDelay.maximumSeconds).contains(total) else { return nil }
        return ReauthenticationDelay(seconds: total)
    }
}
