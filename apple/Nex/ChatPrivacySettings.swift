import SwiftUI

struct ChatPrivacySettings: View {
    @ObservedObject var model: Controller
    @ObservedObject var privacy: AppPrivacy
    @State private var error: String?

    var body: some View {
        Section {
            Toggle(ChatPrivacy.Setting.enabled.title, isOn: binding(.enabled))
                .disabled(!canEdit)
            if let settings = model.chatPrivacy {
                ForEach(ChatPrivacy.Setting.allCases.filter { $0 != .enabled }, id: \.self) { setting in
                    Toggle(setting.title, isOn: binding(setting))
                        .disabled(!canEdit || !settings[.enabled])
                }
            }
            if model.savingChatPrivacy {
                ProgressView("Saving Chat privacy…").controlSize(.small)
            }
            if let error { Text(error).foregroundStyle(.red) }
        } header: {
            Text("Private Chat")
        } footer: {
            VStack(alignment: .leading, spacing: 4) {
                Text("Choose which new messages go to the private inbox. The inbox keeps answers for up to seven days. Use Message options in Chat to choose privacy for one message.")
                Text("Requests that need protected information always use the private inbox. Web providers still receive the search terms needed to run a search.")
                if !privacy.connected {
                    Text("Connect and unlock Nex to change these settings.")
                } else if model.chatPrivacy == nil && error == nil {
                    Text("Update Excalibur if these settings remain unavailable.")
                }
            }
        }
        .task(id: privacy.connected) {
            error = nil
            guard privacy.connected else { return }
            do { try await model.refreshStatus() }
            catch is CancellationError { }
            catch { self.error = error.localizedDescription }
        }
    }

    private var canEdit: Bool { privacy.connected && model.chatPrivacy != nil && !model.savingChatPrivacy }

    private func binding(_ setting: ChatPrivacy.Setting) -> Binding<Bool> {
        Binding(get: { model.chatPrivacy?[setting] ?? (setting == .enabled) }, set: { enabled in
            Task {
                error = nil
                do { try await model.setChatPrivacy(setting, enabled: enabled) }
                catch is CancellationError { }
                catch { self.error = error.localizedDescription }
            }
        })
    }
}

private extension ChatPrivacy.Setting {
    var title: LocalizedStringKey {
        switch self {
        case .enabled: "Enable Private Chat"
        case .general: "Keep general messages private"
        case .web: "Keep web searches private"
        case .python: "Keep calculations and code private"
        case .weather: "Keep weather questions private"
        }
    }
}
