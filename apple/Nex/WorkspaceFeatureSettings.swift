import SwiftUI

struct WorkspaceFeatureSettings: View {
    @ObservedObject var model: Controller
    @State private var error: String?

    var body: some View {
        Section {
            ForEach(WorkspaceFeature.allCases) { feature in
                Toggle(isOn: binding(for: feature)) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(feature.title)
                        Text(feature.detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
                .disabled(model.workspaceFeatures == nil || model.status == nil || model.statusUnavailable ||
                          model.savingWorkspaceFeature)
            }
            if model.savingWorkspaceFeature {
                ProgressView("Saving workspace settings…").controlSize(.small)
            }
            if let error { Text(error).foregroundStyle(.red) }
        } header: {
            Text("Workspace features")
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text("All five start on. Turning a feature off hides its tab and pauses its work on both devices. Chat and Files are always available.")
                if model.pairing == nil {
                    Text("Pair with Excalibur to change these settings.")
                } else if model.statusUnavailable || model.status == nil {
                    Text("Connect to Excalibur to sync these settings.")
                } else if model.workspaceFeatures == nil {
                    Text("Update the Excalibur service to change these settings.")
                }
            }
        }
        .task(id: model.pairing?.url) {
            guard model.pairing != nil else { return }
            try? await model.refreshStatus()
        }
    }

    private func binding(for feature: WorkspaceFeature) -> Binding<Bool> {
        Binding(get: { model.workspaceFeatureEnabled(feature) }, set: { enabled in
            Task {
                error = nil
                do { try await model.setWorkspaceFeature(feature, enabled: enabled) }
                catch { self.error = error.localizedDescription }
            }
        })
    }
}

private extension WorkspaceFeature {
    var title: LocalizedStringKey {
        switch self {
        case .daybook: "Daybook"
        case .activities: "Activities"
        case .feed: "Feed"
        case .ideas: "Ideas"
        case .goals: "Goals"
        }
    }

    var detail: LocalizedStringKey {
        switch self {
        case .daybook: "Daily briefings, conversation timelines, and Daybook questions."
        case .activities: "Activity recognition, the timeline, and Current activity in Chat."
        case .feed: "Daily news editions and saved stories."
        case .ideas: "Suggested prompts and actions."
        case .goals: "Goal tracking, Gmail and Calendar watches, timed reminders, alarms, and timers."
        }
    }
}
