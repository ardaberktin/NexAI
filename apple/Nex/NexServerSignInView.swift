import SwiftUI

struct NexServerSignInView: View {
    @ObservedObject var model: Controller

    var body: some View {
        Form {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Connect to Nex", systemImage: "server.rack")
                        .font(.title2.bold())
                    Text("Connect to Tailscale and use the server link and account from your administrator.")
                        .foregroundStyle(.secondary)
                }
            }
            NexServerSignInFields(model: model)
        }
        .formStyle(.grouped)
        .frame(maxWidth: 520, maxHeight: 640)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct NexServerSignInFields: View {
    @ObservedObject var model: Controller
    @State private var link = ""
    @State private var username = ""
    @State private var password = ""
    @State private var confirmation = ""
    @State private var submitting = false
    @State private var error: String?

    var body: some View {
        Section("Nex account") {
            TextField("Server link", text: $link, prompt: Text("nex://…"))
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                #endif
            TextField("Username", text: $username)
                .textContentType(.username)
                .autocorrectionDisabled()
                #if os(iOS)
                .textInputAutocapitalization(.never)
                #endif
            if creatingOwner {
                HostedPasswordFields(password: $password, confirmation: $confirmation)
                Text("This setup link creates the owner of the server.")
                    .foregroundStyle(.secondary)
                Button("Sign In Instead") {
                    if let setup = try? NexServerLink(link),
                       let regular = NexServerLink.serverLink(address: setup.address, pin: setup.pin) {
                        link = regular
                        confirmation = ""
                        error = nil
                    }
                }
            } else {
                SecureField("Password", text: $password)
                    .textContentType(.password)
                    .onSubmit(signIn)
            }
            if let message = error ?? model.message {
                Text(message).foregroundStyle(.secondary)
            }
            Button(submitting ? "Connecting…" : creatingOwner ? "Create Owner Account" : "Sign In", action: signIn)
                .buttonStyle(.borderedProminent)
                .disabled(!available)
        }
        .disabled(submitting || model.signingOut)
        .onAppear {
            if link.isEmpty, let resetLink = model.resetSignInLink { link = resetLink }
        }
    }

    private var creatingOwner: Bool { (try? NexServerLink(link))?.setup != nil }

    private var available: Bool {
        !submitting && !model.signingOut && !link.isEmpty && !username.isEmpty &&
            (creatingOwner ? HostedPasswordFields.isValid(password, confirmation) : !password.isEmpty)
    }

    private func signIn() {
        guard available else { return }
        submitting = true
        error = nil
        let enteredPassword = password
        password = ""
        confirmation = ""
        Task {
            defer { submitting = false }
            do {
                try await model.signIn(link: link, username: username, password: enteredPassword)
            } catch is CancellationError {
                return
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
