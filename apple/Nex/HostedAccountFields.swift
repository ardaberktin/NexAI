import SwiftUI

struct HostedUsernameField: View {
    @Binding var username: String

    var body: some View {
        TextField("Username", text: $username)
            .textContentType(.username)
            .autocorrectionDisabled()
            #if os(iOS)
            .textInputAutocapitalization(.never)
            #endif
    }
}

struct HostedPasswordFields: View {
    @Binding var password: String
    @Binding var confirmation: String

    static func isValid(_ password: String, _ confirmation: String) -> Bool {
        password.unicodeScalars.count >= 12 && password.utf8.count <= 512 && password.utf8.elementsEqual(confirmation.utf8)
    }

    var body: some View {
        SecureField("New password", text: $password).textContentType(.newPassword)
        SecureField("Confirm password", text: $confirmation).textContentType(.newPassword)
        if !confirmation.isEmpty && !password.utf8.elementsEqual(confirmation.utf8) {
            Text("The passwords don’t match.").foregroundStyle(.secondary)
        } else {
            Text("Use at least 12 characters.").foregroundStyle(.secondary)
        }
    }
}

struct HostedForm<Content: View>: View {
    let title: String
    let pending: Bool
    let error: String?
    let saveTitle: String?
    let ready: Bool
    var destructive = false
    let save: () -> Void
    @ViewBuilder let content: Content
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                content
                if let error { Text(error).foregroundStyle(.red) }
            }
            .formStyle(.grouped)
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(saveTitle == nil ? "Done" : "Cancel") { dismiss() }.disabled(pending)
                }
                if let saveTitle {
                    ToolbarItem(placement: .confirmationAction) {
                        Button(saveTitle, role: destructive ? .destructive : nil, action: save)
                            .disabled(pending || !ready)
                    }
                }
            }
            .disabled(pending)
            .overlay { if pending { ProgressView() } }
        }
        .interactiveDismissDisabled(pending)
        #if os(macOS)
        .frame(minWidth: 440, minHeight: 330)
        #endif
    }
}
