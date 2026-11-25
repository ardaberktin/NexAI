import SwiftUI

struct HostedAccountSettings: View {
    @ObservedObject var model: Controller
    @ObservedObject private var privacy: AppPrivacy
    @State private var profile: HostedProfile?
    @State private var users: [HostedUser] = []
    @State private var panel: AccountPanel?
    @State private var issue: String?
    @State private var loading = false
    @State private var deviceToRevoke: HostedDevice?

    init(model: Controller) {
        self.model = model
        privacy = model.privacy
    }

    var body: some View {
        Group {
            if let account = model.serverProfile?.account {
                accountSection(account)
                if let profile {
                    deviceSection(profile.devices)
                    if profile.user.admin { administration(profile.user) }
                }
            } else {
                NexServerSignInFields(model: model)
            }
        }
        .task(id: privacy.connected ? model.serverProfile?.account.deviceId : nil) { await load() }
        .sheet(item: $panel) { selected in
            switch selected {
            case .credentials(let action):
                HostedCredentialEditor(model: model, action: action, onSave: refresh)
            case .user(let user):
                HostedUserEditor(model: model, user: user, owner: profile?.user.owner == true, onSave: refresh)
            }
        }
        .confirmationDialog("Sign out this device?", isPresented: Binding(
            get: { deviceToRevoke != nil }, set: { if !$0 { deviceToRevoke = nil } }
        ), presenting: deviceToRevoke) { device in
            Button("Sign Out \(device.name)", role: .destructive) {
                Task { await revoke(device) }
            }
        }
    }

    private func accountSection(_ account: HostedAccount) -> some View {
        Section("Profile") {
            LabeledContent("Username", value: profile?.user.username ?? account.username)
            if let profile { LabeledContent("Role", value: profile.user.role) }
            if privacy.connected, let profile {
                Button("Edit Username") { panel = .credentials(.rename(profile.user.username)) }
                Button("Change Password") { panel = .credentials(.changePassword) }
            }
            if let link = model.serverProfile?.serverLink {
                ShareLink(item: link) { Label("Share Server Link", systemImage: "square.and.arrow.up") }
            }
            Button("Sign Out", role: .destructive) { Task { await model.signOut() } }
                .disabled(model.signingOut)
            if loading { ProgressView("Loading account settings…") }
            if !privacy.connected { Text("Unlock the workspace to edit account settings.").foregroundStyle(.secondary) }
            if let issue {
                Text(issue).foregroundStyle(.red)
                Button("Refresh Account Settings") { refresh() }.disabled(loading || !privacy.connected)
            }
        }
    }

    private func deviceSection(_ devices: [HostedDevice]) -> some View {
        Section("Signed-In Devices") {
            ForEach(devices) { device in
                if device.current {
                    LabeledContent(device.name, value: "This device")
                } else {
                    HStack {
                        Text(device.name)
                        Spacer()
                        Button("Sign Out") { deviceToRevoke = device }
                            .accessibilityLabel("Sign out \(device.name)")
                    }
                }
            }
        }
    }

    private func administration(_ current: HostedUser) -> some View {
        Section("Manage Users") {
            Button { panel = .credentials(.createUser) } label: {
                Label("Add User", systemImage: "person.badge.plus")
            }
            ForEach(users) { user in
                if current.owner || !user.admin {
                    Button { panel = .user(user) } label: {
                        LabeledContent(user.username, value: user.enabled ? user.role : "Disabled")
                    }
                } else {
                    LabeledContent(user.username, value: user.enabled ? user.role : "Disabled")
                }
            }
        }
    }

    private func refresh() { Task { await load() } }

    private func load() async {
        guard privacy.connected, !loading, let account = model.serverProfile?.account else { return }
        loading = true
        defer { loading = false }
        issue = nil
        do {
            let api = HostedAccountAPI(try model.connection())
            let loaded = try await api.profile()
            guard !Task.isCancelled, model.serverProfile?.account.deviceId == account.deviceId else { return }
            profile = loaded
            try model.rememberUsername(loaded.user.username, deviceId: account.deviceId)
            let people = try await loaded.user.admin ? api.users() : []
            guard !Task.isCancelled, model.serverProfile?.account.deviceId == account.deviceId else { return }
            users = people
        } catch {
            if !Task.isCancelled { issue = error.localizedDescription }
        }
    }

    private func revoke(_ device: HostedDevice) async {
        do {
            profile = try await HostedAccountAPI(model.connection()).revoke(device.id)
            issue = nil
        } catch { issue = error.localizedDescription }
    }
}

private enum AccountPanel: Identifiable {
    case credentials(HostedCredentialAction)
    case user(HostedUser)

    var id: String {
        switch self {
        case .credentials(let action): action.title
        case .user(let user): user.id
        }
    }
}
