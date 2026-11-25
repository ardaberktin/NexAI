import SwiftUI

struct MCPConnectionEditor: View {
    private struct Header: Identifiable {
        let id = UUID()
        var name = ""
        var value = ""
    }

    @Environment(\.dismiss) private var dismiss
    @State private var draft = MCPConnectionDraft()
    @State private var token = ""
    @State private var headers: [Header] = []
    @State private var issue: String?
    @State private var connecting = false
    let save: (MCPConnectionDraft) async throws -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Name", text: $draft.name)
                    TextField("Endpoint URL", text: $draft.url)
                        .autocorrectionDisabled()
                        #if os(iOS)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        #endif
                    Picker("Transport", selection: $draft.transport) {
                        ForEach(MCPTransport.allCases) { transport in
                            Text(transport.title).tag(transport)
                        }
                    }
                }
                Section("Authentication (Optional)") {
                    SecureField("Bearer Token", text: $token)
                    DisclosureGroup("Custom HTTP Headers") {
                        ForEach($headers) { $header in
                            VStack(alignment: .leading) {
                                TextField("Header Name", text: $header.name)
                                    .autocorrectionDisabled()
                                    #if os(iOS)
                                    .textInputAutocapitalization(.never)
                                    #endif
                                SecureField("Header Value", text: $header.value)
                                Button("Remove Header", role: .destructive) {
                                    headers.removeAll { $0.id == header.id }
                                }
                            }
                        }
                        Button("Add Header", systemImage: "plus") { headers.append(Header()) }
                            .disabled(headers.count >= 12)
                    }
                }
                if connecting { ProgressView("Connecting and discovering tools…") }
                if let issue { Text(issue).foregroundStyle(.red) }
            }
            .formStyle(.grouped)
            .disabled(connecting)
            .navigationTitle("Add MCP Server")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Connect") { Task { await connect() } }
                        .disabled(connecting || trimmed(draft.name).isEmpty || trimmed(draft.url).isEmpty)
                }
            }
            #if os(macOS)
            .frame(minWidth: 460, minHeight: 320)
            #endif
        }
        .interactiveDismissDisabled(connecting)
    }

    private func trimmed(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func connect() async {
        guard !connecting else { return }
        var outgoing = draft
        outgoing.name = trimmed(draft.name)
        outgoing.url = trimmed(draft.url)
        if !trimmed(token).isEmpty { outgoing.headers["authorization"] = "Bearer " + trimmed(token) }
        for header in headers {
            let name = trimmed(header.name).lowercased()
            guard !name.isEmpty, !trimmed(header.value).isEmpty, outgoing.headers[name] == nil else {
                issue = "Enter a name and value for each header. Use each header name once."
                return
            }
            outgoing.headers[name] = trimmed(header.value)
        }
        issue = nil
        connecting = true
        defer { connecting = false }
        do {
            try await save(outgoing)
            dismiss()
        } catch {
            if !(error is CancellationError) { issue = error.localizedDescription }
        }
    }
}
