import SwiftUI

struct NexServerSettings: View {
    @ObservedObject var model: Controller

    var body: some View {
        if let profile = model.serverProfile {
            Section("Nex server") {
                LabeledContent("Tailscale address", value: profile.url)
                if let status = model.status {
                    LabeledContent("Chat", value: chatStatus(status.models.chat))
                    LabeledContent("Transcription", value: status.models.transcription == "configured"
                                   ? "Local Whisper configured" : "Waiting for model")
                    LabeledContent("Audio allowance",
                                   value: ByteCountFormatter.string(fromByteCount: Int64(status.remainingBytes),
                                                                    countStyle: .file))
                }
            }
        } else {
            NexServerSignInFields(model: model)
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
