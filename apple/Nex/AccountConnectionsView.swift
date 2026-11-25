import SwiftUI

struct AccountConnectionsView: View {
    @ObservedObject var model: Controller
    @ObservedObject private var signIn: AccountSignIn
    @State private var refreshing = false
    @State private var clientToSave: GoogleAccountClient?

    init(model: Controller) {
        self.model = model
        signIn = model.accountSignIn
    }

    var body: some View {
        Section {
            account("Gmail", key: "gmail")
            account("Google Calendar", key: "calendar")
            if model.serverProfile != nil {
                HStack {
                    Button(googleButton, systemImage: "person.badge.key") { signIn.start(.google, model: model) }
                    if !signIn.busy {
                        Button("Google client setup") { signIn.showGoogleSetup = true }
                    }
                }
                .buttonStyle(.borderless)
                .disabled(signIn.busy)
                if hasGoogleGrant || signIn.disconnecting == .google {
                    disconnectButton("Disconnect Google", provider: .google)
                }
            }

            account("TickTick", key: "ticktick")
            if model.serverProfile != nil {
                Button(tickTickButton, systemImage: "person.badge.key") { signIn.start(.ticktick, model: model) }
                    .disabled(signIn.busy)
                if hasGrant("ticktick") || signIn.disconnecting == .ticktick {
                    disconnectButton("Disconnect TickTick", provider: .ticktick)
                }
                if signIn.awaitingUnlock {
                    let message: LocalizedStringKey = signIn.disconnecting == nil
                        ? "Sign-in will continue when Nex connects to your unlocked workspace."
                        : "Disconnect will continue when Nex connects to your unlocked workspace."
                    Text(message).foregroundStyle(.secondary)
                }
                if signIn.provider != nil {
                    Button("Cancel sign-in", action: signIn.cancel)
                } else if signIn.awaitingUnlock {
                    Button("Cancel disconnect", action: signIn.cancel)
                }
                Button(refreshing ? "Refreshing…" : "Refresh connection status", systemImage: "arrow.clockwise") {
                    Task { await refresh() }
                }
                .disabled(refreshing)
            } else {
                Text("Pair with Excalibur to connect these accounts.")
                    .foregroundStyle(.secondary)
            }
            if let error = signIn.error { Text(error).foregroundStyle(.red) }
            if let confirmation = signIn.confirmation { Text(confirmation).foregroundStyle(.secondary) }
        }
        .task(id: model.serverProfile?.url) { await refresh() }
        .sheet(isPresented: $signIn.showGoogleSetup, onDismiss: saveSelectedClient) {
            GoogleConnectionSetup { client in
                clientToSave = client
                signIn.showGoogleSetup = false
            }
        }
    }

    private var googleButton: LocalizedStringKey {
        if signIn.provider == .google { return "Connecting Google…" }
        return hasGoogleGrant ? "Reconnect Google" : "Connect Google"
    }

    private var tickTickButton: LocalizedStringKey {
        if signIn.provider == .ticktick { return "Connecting TickTick…" }
        return hasGrant("ticktick") ? "Reconnect TickTick" : "Connect TickTick"
    }

    private var hasGoogleGrant: Bool { hasGrant("gmail") || hasGrant("calendar") }

    private func disconnectButton(_ label: LocalizedStringKey, provider: AccountProvider) -> some View {
        Button(signIn.disconnecting == provider ? "Disconnecting…" : label,
               systemImage: "minus.circle", role: .destructive) {
            signIn.disconnect(provider, model: model)
        }
        .disabled(signIn.busy)
    }

    private func hasGrant(_ key: String) -> Bool {
        guard !model.statusUnavailable, let state = model.status?.connections?[key] else { return false }
        return state == "connected" || state == "reconnect"
    }

    private func account(_ label: LocalizedStringKey, key: String) -> some View {
        LabeledContent(label) { Text(status(key)) }
    }

    private func status(_ key: String) -> LocalizedStringKey {
        guard model.serverProfile != nil else { return "Pair to connect" }
        switch model.statusUnavailable ? nil : model.status?.connections?[key] {
        case "connected": return "Connected (read-only)"
        case "reconnect": return "Reconnect needed"
        case "disconnected": return "Not connected"
        default: return refreshing ? "Checking…" : "Status unavailable"
        }
    }

    private func refresh() async {
        guard model.serverProfile != nil, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        try? await model.refreshStatus()
    }

    private func saveSelectedClient() {
        guard let clientToSave else { return }
        self.clientToSave = nil
        signIn.start(.google, model: model, client: clientToSave)
    }
}
