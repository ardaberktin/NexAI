import SwiftUI
import UniformTypeIdentifiers

struct ExcaliburPairingSettings: View {
    @ObservedObject var model: Controller
    @State private var profile = ""
    @State private var showProfilePicker = false
    @State private var pending = false
    @State private var error: String?

    var body: some View {
        Section("Pairing") {
            if InstalledPairing.available {
                Button("Use installed pairing profile") { Task { await pair(installed: true) } }
                    .disabled(pending)
            }
            if let pairing = model.pairing {
                LabeledContent("Tailscale address", value: pairing.url)
                if let status = model.status {
                    LabeledContent("Chat", value: chatStatus(status.models.chat))
                    LabeledContent("Transcription", value: status.models.transcription == "configured" ? "Local Whisper configured" : "Waiting for model")
                    LabeledContent("Audio allowance",
                                   value: ByteCountFormatter.string(fromByteCount: Int64(status.remainingBytes),
                                                                    countStyle: .file))
                }
                Button("Update pairing profile") { showProfilePicker = true }
                Button("Forget pairing", role: .destructive) { model.unpair() }
            } else {
                Text("Connect Tailscale, then import the pairing profile for this device.")
                    .foregroundStyle(.secondary)
                SecureField("Paste this device’s pairing profile", text: $profile)
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    #endif
                Button(pending ? "Pairing…" : "Pair") { Task { await pair() } }
                    .disabled(pending || profile.isEmpty)
                Button("Open pairing file") { showProfilePicker = true }.disabled(pending)
            }
            if let error { Text(error).foregroundStyle(.red) }
        }
        .fileImporter(isPresented: $showProfilePicker, allowedContentTypes: [.json]) { result in
            do {
                let url = try result.get()
                let accessed = url.startAccessingSecurityScopedResource()
                defer { if accessed { url.stopAccessingSecurityScopedResource() } }
                let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard 0 < size && size < 4096 else {
                    throw NexError.pairing("Choose the small JSON pairing profile for this device.")
                }
                profile = try String(contentsOf: url, encoding: .utf8)
                Task { await pair() }
            } catch { self.error = error.localizedDescription }
        }
    }

    private func pair(installed: Bool = false) async {
        pending = true
        error = nil
        defer { pending = false }
        do {
            let pairing = try installed ? InstalledPairing.read() : JSONDecoder().decode(Pairing.self, from: Data(profile.utf8))
            try await model.pair(pairing)
            profile = ""
        } catch {
            self.error = error is DecodingError ? "Paste the complete pairing profile for this device." : error.localizedDescription
        }
    }

    private func chatStatus(_ state: String) -> String {
        switch state {
        case "excalibur_live": "Selected Excalibur model"
        case "configured": "Excalibur model configured"
        case "mac_connected": "Mac model connected"
        case "mac_waiting": "Waiting for Mac relay"
        default: "Archive search"
        }
    }
}
