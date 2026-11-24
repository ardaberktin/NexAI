#if os(macOS)
import SwiftUI

struct ScreenSharingControls: View {
    @ObservedObject var sharing: ScreenSharing

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(sharing.phase.action, systemImage: sharing.isSharing ? "stop.rectangle" : "macwindow") {
                    sharing.toggle()
                }
                .buttonStyle(.bordered)
                .disabled(!sharing.gemmaEnabled)
                .help(sharing.gemmaEnabled ? sharing.phase.action : "Enable Gemma in Settings to share your screen.")
                if sharing.looking {
                    ProgressView().controlSize(.small)
                    Text("Looking at your screen").font(.subheadline)
                } else if sharing.isSharing {
                    Label(sharing.suspended ? "Screen capture paused" : "Tracking screen activity",
                          systemImage: sharing.suspended ? "pause.circle" : "eye")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }
            if sharing.isSharing, !sharing.suspended, let latest = sharing.observedAt {
                Text("Last observed \(latest.formatted(date: .omitted, time: .shortened))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let error = sharing.error {
                Text(error).font(.callout).foregroundStyle(.orange)
            }
        }
    }
}

struct ScreenSharingMenu: View {
    @ObservedObject var sharing: ScreenSharing

    var body: some View {
        Button(sharing.phase.action) { sharing.toggle() }
            .disabled(!sharing.gemmaEnabled)
    }
}
#endif
