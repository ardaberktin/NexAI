#if os(iOS)
import SwiftUI

struct PhoneWorkspaceView<Content: View>: View {
    @Environment(\.colorScheme) private var scheme
    @ObservedObject var model: Controller
    let selection: WorkspaceTab
    let availableTabs: [WorkspaceTab]
    let select: (WorkspaceTab) -> Void
    @Binding var tool: WorkspaceTool?
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 0) {
                header
                if model.pairing != nil {
                    RecordingStatusPeers(local: .iphone)
                }
            }
            .overlay(alignment: .bottom) { Divider().opacity(0.45) }
            content()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    WorkspaceTabBar(selection: selection, availableTabs: availableTabs, select: select)
                }
        }
        .background(WorkspaceStyle.background(scheme).ignoresSafeArea())
        .sheet(item: $tool) { selection in
            switch selection {
            case .settings(let category):
                SettingsView(model: model, initialCategory: category)
            case .memory:
                WorkspaceSheet(title: selection.title) { PersonalContextView(model: model) }
            case .archive:
                WorkspaceSheet(title: selection.title) { ArchiveView(model: model) }
            case .capture:
                WorkspaceSheet(title: selection.title) {
                    CaptureWorkspaceControls(model: model, capture: model.capture)
                }
            }
        }
    }

    private var header: some View {
        HStack {
            Menu {
                ForEach([WorkspaceTool.capture, .memory, .archive, .settings()]) { item in
                    Button(item.title, systemImage: item.symbol) { tool = item }
                }
            } label: {
                Image(systemName: "line.3.horizontal")
                    .font(.system(size: 19, weight: .medium))
                    .frame(width: 44, height: 44)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            }
            .menuIndicator(.hidden)
            .fixedSize()
            .accessibilityLabel("Workspace menu")
            Spacer()
            HStack(spacing: 8) {
                NexMark(size: 31)
                Text("Nex").font(.system(.headline, design: .rounded, weight: .bold))
            }
            Spacer()
            CaptureStatusButton(capture: model.capture, open: { tool = .capture })
        }
        .frame(maxWidth: WorkspaceStyle.readable)
        .padding(.horizontal, WorkspaceStyle.inset)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
    }
}

enum WorkspaceTool: Hashable, Identifiable {
    case memory
    case archive
    case settings(SettingsCategory? = nil)
    case capture

    var id: Self { self }

    var title: LocalizedStringKey {
        switch self {
        case .memory: "Memory"
        case .archive: "Archive"
        case .settings: "Settings"
        case .capture: "Voice & recording"
        }
    }

    var symbol: String {
        switch self {
        case .memory: "person.text.rectangle"
        case .archive: "clock.arrow.circlepath"
        case .settings: "gearshape"
        case .capture: "waveform"
        }
    }
}

private struct CaptureStatusButton: View {
    @ObservedObject var capture: Capture
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            Image(systemName: capture.recording ? "record.circle.fill" : "waveform")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(capture.recording ? Color.red : Color.secondary)
                .frame(width: 44, height: 44)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(capture.recording ? "Recording controls, recording active" : "Voice and recording controls")
    }
}
#endif
