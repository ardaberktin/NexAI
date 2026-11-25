import SwiftUI

struct HostedUserEditor: View {
    let model: Controller
    let user: HostedUser
    let owner: Bool
    let onSave: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var username = ""
    @State private var enabled = true
    @State private var administrator = false
    @State private var storage: HostedStorage?
    @State private var quota = ""
    @State private var resetting = false
    @State private var pending = false
    @State private var issue: String?

    var body: some View {
        HostedForm(title: user.username, pending: pending, error: issue, saveTitle: user.owner ? nil : "Save",
                   ready: accountChanged && !username.isEmpty, save: submit) {
            if !user.owner {
                Section("Account") {
                    HostedUsernameField(username: $username)
                    Toggle("Account enabled", isOn: $enabled)
                    if !enabled {
                        Text("The user is signed out and their archive is kept.").foregroundStyle(.secondary)
                    }
                    if owner {
                        Toggle("Administrator", isOn: $administrator)
                        if administrator {
                            Text("This user can manage member accounts.").foregroundStyle(.secondary)
                        }
                    }
                    Button("Reset Password") { resetting = true }
                }
            }
            Section("Audio Storage") {
                if let storage {
                    LabeledContent("Used", value: ByteCountFormatter.string(fromByteCount: storage.audioBytes,
                                                                             countStyle: .file))
                    TextField("Limit in GiB", text: $quota, prompt: Text("Unlimited"))
                        #if os(iOS)
                        .keyboardType(.numberPad)
                        #endif
                    Button("Save Audio Limit") { Task { await saveQuota() } }.disabled(!quotaReady)
                } else {
                    Button("Load Audio Storage") { Task { await loadStorage() } }
                }
            }
        }
        .sheet(isPresented: $resetting) {
            HostedCredentialEditor(model: model, action: .reset(user), onSave: onSave)
        }
        .task {
            username = user.username
            enabled = user.enabled
            administrator = user.admin
            await loadStorage()
        }
    }

    private var requestedLimit: Int64? { Int64(quota.trimmingCharacters(in: .whitespacesAndNewlines)) }
    private var accountChanged: Bool {
        !user.owner && (username != user.username || enabled != user.enabled || administrator != user.admin)
    }
    private var quotaChanged: Bool { storage.map { requestedLimit != $0.limitGiB } ?? false }

    private var quotaReady: Bool {
        let trimmed = quota.trimmingCharacters(in: .whitespacesAndNewlines)
        let valid = trimmed.isEmpty || requestedLimit.map { (1..<(1 << 23)).contains($0) } == true
        return valid && quotaChanged
    }

    private func loadStorage() async {
        do {
            let result = try await HostedAccountAPI(model.connection()).storage(user.id)
            guard !Task.isCancelled else { return }
            storage = result
            quota = result.limitGiB.map(String.init) ?? ""
        } catch {
            if !Task.isCancelled { issue = error.localizedDescription }
        }
    }

    private func submit() {
        guard accountChanged, !username.isEmpty, !pending else { return }
        pending = true
        issue = nil
        Task {
            defer { pending = false }
            do {
                let api = HostedAccountAPI(try model.connection())
                _ = try await api.update(user.id, username: username, enabled: enabled, admin: administrator)
                onSave()
                dismiss()
            } catch {
                self.issue = error.localizedDescription
            }
        }
    }

    private func saveQuota() async {
        guard quotaReady, !pending else { return }
        pending = true
        issue = nil
        defer { pending = false }
        do {
            storage = try await HostedAccountAPI(model.connection()).setStorage(user.id, limitGiB: requestedLimit)
            quota = storage?.limitGiB.map(String.init) ?? ""
        } catch { issue = error.localizedDescription }
    }
}
