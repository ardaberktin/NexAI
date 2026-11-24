import SwiftUI

struct FeedSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: Controller
    @State private var originalSettings: FeedSettings
    @State private var settings: FeedSettings
    let didSave: (NewsFeed, Bool) -> Void
    @State private var error: String?
    @State private var busy = false
    @State private var confirming = false

    init(model: Controller, settings: FeedSettings, didSave: @escaping (NewsFeed, Bool) -> Void) {
        self.model = model
        var original = settings
        original.prompt = original.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        _originalSettings = State(initialValue: original)
        _settings = State(initialValue: original)
        self.didSave = didSave
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Canada news, Turkiye news, global news, f1 news",
                              text: $settings.prompt, axis: .vertical)
                        .lineLimit(3...8)
                        .accessibilityLabel("Feed prompt")
                } header: {
                    Text("Feed prompt")
                } footer: {
                    Text("Describe the news you want Nex to cover.")
                }
                Section("Daily edition") {
                    DatePicker("Create at", selection: editionTime, displayedComponents: .hourAndMinute)
                        .environment(\.timeZone, zone)
                    LabeledContent("Time zone", value: settings.zone.replacingOccurrences(of: "_", with: " "))
                }
                if prompt.unicodeScalars.count > 1_200 {
                    WorkspaceIssue(message: "Use up to 1,200 characters for your feed prompt.")
                }
                if let error { WorkspaceIssue(message: error) }
            }
            .formStyle(.grouped)
            .navigationTitle("Feed settings")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(busy)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: confirmSave)
                        .disabled(busy || (hasChanges && (prompt.isEmpty || prompt.unicodeScalars.count > 1_200)))
                }
            }
            .alert("Refresh today’s feed?", isPresented: $confirming) {
                Button("Refresh Now") { Task { await save(refreshNow: true) } }
                Button("Apply Next Time") { Task { await save(refreshNow: false) } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Use your new settings for today’s edition or for the next scheduled edition.")
            }
        }
        .interactiveDismissDisabled(busy)
        #if os(macOS)
        .frame(minWidth: 500, minHeight: 390)
        #endif
    }

    private var zone: TimeZone { TimeZone(identifier: settings.zone) ?? .gmt }
    private var prompt: String { settings.prompt.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var hasChanges: Bool { updatedSettings != originalSettings }

    private var updatedSettings: FeedSettings {
        var updated = settings
        updated.prompt = prompt
        return updated
    }

    private var editionTime: Binding<Date> {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return Binding {
            // A fixed day makes every hour selectable even on daylight saving transitions.
            calendar.date(from: DateComponents(year: 2024, month: 1, day: 15,
                                               hour: settings.hour, minute: settings.minute)) ?? .now
        } set: { date in
            settings.hour = calendar.component(.hour, from: date)
            settings.minute = calendar.component(.minute, from: date)
        }
    }

    private func confirmSave() {
        if hasChanges {
            confirming = true
        } else {
            dismiss()
        }
    }

    private func save(refreshNow: Bool) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let updated = try await model.updateFeed(updatedSettings, refreshNow: refreshNow)
            didSave(updated, refreshNow)
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}
