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
        Section("Connected accounts") {
            account("Gmail", key: "gmail")
            account("Google Calendar", key: "calendar")
            if model.pairing != nil {
                HStack {
                    Button(googleButton, systemImage: "person.badge.key") { signIn.start(.google, model: model) }
                    if signIn.provider == nil {
                        Button("Google client setup") { signIn.showGoogleSetup = true }
                    }
                }
                .buttonStyle(.borderless)
                .disabled(signIn.provider != nil)
            }

            account("TickTick", key: "ticktick")
            if model.pairing != nil {
                Button(tickTickButton, systemImage: "person.badge.key") { signIn.start(.ticktick, model: model) }
                    .disabled(signIn.provider != nil)
                if signIn.provider != nil {
                    if signIn.awaitingUnlock {
                        Text("Sign-in will continue when Nex connects to your unlocked workspace.")
                            .foregroundStyle(.secondary)
                    }
                    Button("Cancel sign-in", action: signIn.cancel)
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
        .task(id: model.pairing?.url) { await refresh() }
        .sheet(isPresented: $signIn.showGoogleSetup, onDismiss: saveSelectedClient) {
            GoogleConnectionSetup { client in
                clientToSave = client
                signIn.showGoogleSetup = false
            }
        }
    }

    private var googleButton: LocalizedStringKey {
        if signIn.provider == .google { return "Connecting Google…" }
        return needsReconnect("gmail") || needsReconnect("calendar") ? "Reconnect Google" : "Connect Google"
    }

    private var tickTickButton: LocalizedStringKey {
        if signIn.provider == .ticktick { return "Connecting TickTick…" }
        return needsReconnect("ticktick") ? "Reconnect TickTick" : "Connect TickTick"
    }

    private func needsReconnect(_ key: String) -> Bool {
        guard !model.statusUnavailable, let state = model.status?.connections?[key] else { return false }
        return state == "connected" || state == "reconnect"
    }

    private func account(_ label: LocalizedStringKey, key: String) -> some View {
        LabeledContent(label) { Text(status(key)) }
    }

    private func status(_ key: String) -> LocalizedStringKey {
        guard model.pairing != nil else { return "Pair to connect" }
        switch model.statusUnavailable ? nil : model.status?.connections?[key] {
        case "connected": return "Connected (read-only)"
        case "reconnect": return "Reconnect needed"
        case "disconnected": return "Not connected"
        default: return refreshing ? "Checking…" : "Status unavailable"
        }
    }

    private func refresh() async {
        guard model.pairing != nil, !refreshing else { return }
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
