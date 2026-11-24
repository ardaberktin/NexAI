import SwiftUI
import UniformTypeIdentifiers

struct GoogleConnectionSetup: View {
    @Environment(\.dismiss) private var dismiss
    @State private var clientID = ""
    #if os(macOS)
    @State private var showDesktopPicker = false
    @State private var importError: String?
    #endif
    let onSelect: (GoogleAccountClient) -> Void

    private var normalizedID: String {
        clientID.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Enable the Gmail API and Google Calendar API in Google Cloud, then configure the OAuth consent screen. Add your Google account as a test user if the project is in testing.")
                    Link("Open Google Auth Platform", destination: URL(string: "https://console.cloud.google.com/auth/clients")!)
                }
                Section {
                    Text("Create an iOS OAuth client using this bundle ID. The same client works on Mac and iPhone.")
                    Text("com.ardaberktin.nex")
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                    TextField("Google OAuth client ID", text: $clientID)
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        #endif
                    Button("Save and sign in") {
                        onSelect(GoogleAccountClient(clientId: normalizedID, clientSecret: nil))
                    }
                    .disabled(!normalizedID.hasSuffix(".apps.googleusercontent.com"))
                }
                #if os(macOS)
                Section {
                    Button("Use Desktop OAuth file…") { showDesktopPicker = true }
                    if let importError { Text(importError).foregroundStyle(.red) }
                } footer: {
                    Text("If you already have a Google Desktop OAuth client, import its JSON file.")
                }
                #endif
            }
            .formStyle(.grouped)
            .navigationTitle("Set up Google")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        #if os(macOS)
        .frame(minWidth: 500, minHeight: 490)
        .fileImporter(isPresented: $showDesktopPicker, allowedContentTypes: [.json]) { result in
            do { onSelect(try GoogleAccountClient.readDesktopFile(result.get())) }
            catch { importError = error.localizedDescription }
        }
        #endif
    }
}
