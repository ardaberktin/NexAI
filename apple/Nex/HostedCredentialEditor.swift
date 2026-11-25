import SwiftUI

enum HostedCredentialAction {
    case owner, createUser, rename(String), changePassword, reset(HostedUser)

    var title: String {
        switch self {
        case .owner: "Set Up Owner Account"
        case .createUser: "Add User"
        case .rename: "Edit Username"
        case .changePassword: "Change Password"
        case .reset: "Reset Password"
        }
    }

    var usesUsername: Bool {
        switch self {
        case .owner, .createUser, .rename: true
        case .changePassword, .reset: false
        }
    }

    var usesPassword: Bool {
        if case .rename = self { return false }
        return true
    }

    var usesCurrentPassword: Bool {
        if case .changePassword = self { return true }
        return false
    }
}

struct HostedCredentialEditor: View {
    let model: Controller
    let action: HostedCredentialAction
    let onSave: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var username = ""
    @State private var password = ""
    @State private var confirmation = ""
    @State private var currentPassword = ""
    @State private var pending = false
    @State private var error: String?

    var body: some View {
        HostedForm(title: action.title, pending: pending, error: error, saveTitle: saveTitle,
                   ready: ready, save: submit) {
            if case .owner = action {
                Text("This account will inherit the existing archive. Other devices will need to sign in again.")
                    .foregroundStyle(.secondary)
            }
            if case .reset(let user) = action {
                Text("Resetting the password signs \(user.username) out on all devices.")
                    .foregroundStyle(.secondary)
            }
            if action.usesUsername { HostedUsernameField(username: $username) }
            if action.usesCurrentPassword {
                SecureField("Current password", text: $currentPassword).textContentType(.password)
                Text("Other devices will need to sign in again.").foregroundStyle(.secondary)
            }
            if action.usesPassword { HostedPasswordFields(password: $password, confirmation: $confirmation) }
        }
        .onAppear {
            if case .rename(let current) = action { username = current }
        }
    }

    private var saveTitle: String {
        switch action {
        case .owner, .createUser: "Create"
        case .reset: "Reset"
        default: "Save"
        }
    }

    private var ready: Bool {
        (!action.usesUsername || !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) &&
            (!action.usesPassword || HostedPasswordFields.isValid(password, confirmation)) &&
            (!action.usesCurrentPassword || !currentPassword.isEmpty)
    }

    private func submit() {
        guard ready, !pending else { return }
        pending = true
        error = nil
        let secret = password
        let oldSecret = currentPassword
        password = ""
        confirmation = ""
        currentPassword = ""
        Task {
            defer { pending = false }
            do {
                try await save(password: secret, current: oldSecret)
                onSave()
                dismiss()
            } catch is CancellationError {
                return
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func save(password: String, current: String) async throws {
        let api = HostedAccountAPI(try model.connection())
        switch action {
        case .owner:
            try await model.createOwnerAccount(username: username, password: password)
        case .createUser:
            _ = try await api.create(username: username, password: password)
        case .rename:
            let device = model.pairing?.account?.deviceId
            let profile = try await api.rename(username)
            if let device { try model.rememberUsername(profile.user.username, deviceId: device) }
        case .changePassword:
            _ = try await api.changePassword(current: current, password: password)
        case .reset(let user):
            try await api.resetPassword(user.id, password: password)
        }
    }
}
