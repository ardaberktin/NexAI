#if os(macOS)
import SwiftUI

struct MacWorkspaceView<Content: View>: View {
    @ObservedObject var model: Controller
    @Binding var selection: WorkspaceTab
    let availableTabs: [WorkspaceTab]
    @ViewBuilder var content: () -> Content
    @State private var visibility: NavigationSplitViewVisibility = .all
    @State private var showingCapture = false

    var body: some View {
        NavigationSplitView(columnVisibility: $visibility) {
            sidebar
                .navigationSplitViewColumnWidth(min: 174, ideal: 216, max: 300)
        } detail: {
            content()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .textBackgroundColor))
                .navigationTitle(selection.title)
                .toolbar {
                    if model.pairing != nil {
                        ToolbarItem(placement: .automatic) {
                            RecordingStatusPeers(local: .mac)
                        }
                    }
                    ToolbarItem(placement: .automatic) {
                        MacCaptureButton(capture: model.capture, showsTitle: selection == .chat) {
                            showingCapture = true
                        }
                    }
                }
        }
        .focusedSceneValue(\.workspaceTab, $selection)
        .focusedSceneValue(\.availableWorkspaceTabs, availableTabs)
        .sheet(isPresented: $showingCapture) {
            WorkspaceSheet(title: "Voice & recording") {
                CaptureWorkspaceControls(model: model, capture: model.capture)
            }
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                NexMark(size: 32)
                Text("Nex").font(.title2.weight(.semibold))
            }
            .padding(.horizontal, 18)
            .padding(.top, 8)
            .padding(.bottom, 14)

            List(
                selection: Binding<WorkspaceTab?>(
                    get: { selection },
                    set: { tab in
                        if let tab { selection = tab }
                    })
            ) {
                Section("Workspace") { rows([.chat, .daybook, .activities, .feed, .ideas, .goals]) }
                Section("Library") { rows([.files, .memory, .archive]) }
            }
            .listStyle(.sidebar)
            .accessibilityLabel("Workspace navigation")

            Divider().padding(.horizontal, 14)
            HStack(spacing: 9) {
                Image(systemName: "externaldrive")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Excalibur").font(.subheadline.weight(.medium))
                    Text(connectionStatus).font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                SettingsLink { Label("Settings", systemImage: "gearshape") }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.borderless)
                    .help("Open Settings")
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 14)
        }
    }

    private func rows(_ tabs: [WorkspaceTab]) -> some View {
        ForEach(tabs.filter(availableTabs.contains), id: \.self) { tab in
            Label(tab.title, systemImage: tab.symbol)
                .padding(.vertical, 3)
                .tag(tab)
        }
    }

    private var connectionStatus: LocalizedStringKey {
        if model.pairing == nil { return "Not paired" }
        if model.statusUnavailable { return "Unavailable" }
        return model.status == nil ? "Connecting…" : "Connected"
    }
}

private struct MacCaptureButton: View {
    @ObservedObject var capture: Capture
    let showsTitle: Bool
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            if showsTitle || capture.recording { label.labelStyle(.titleAndIcon) } else { label.labelStyle(.iconOnly) }
        }
        .help("Voice and recording controls")
        .accessibilityLabel(
            capture.recording ? "Voice and recording controls, recording active" : "Voice and recording controls")
    }

    private var label: some View {
        Label {
            Text(capture.recording ? "Mac recording" : "Voice & recording")
        } icon: {
            if capture.recording {
                Image(systemName: "record.circle.fill").foregroundStyle(.red)
            } else {
                Image(systemName: "waveform")
            }
        }
    }
}

private struct WorkspaceTabKey: FocusedValueKey {
    typealias Value = Binding<WorkspaceTab>
}

private struct AvailableWorkspaceTabsKey: FocusedValueKey {
    typealias Value = [WorkspaceTab]
}

extension FocusedValues {
    fileprivate var workspaceTab: Binding<WorkspaceTab>? {
        get { self[WorkspaceTabKey.self] }
        set { self[WorkspaceTabKey.self] = newValue }
    }

    fileprivate var availableWorkspaceTabs: [WorkspaceTab]? {
        get { self[AvailableWorkspaceTabsKey.self] }
        set { self[AvailableWorkspaceTabsKey.self] = newValue }
    }
}

struct MacWorkspaceCommands: Commands {
    @FocusedBinding(\.workspaceTab) private var selection
    @FocusedValue(\.availableWorkspaceTabs) private var availableTabs

    var body: some Commands {
        CommandMenu("Go") {
            ForEach(availableTabs ?? [], id: \.self) { tab in
                Button {
                    selection = tab
                } label: {
                    Text(tab.title)
                }
                .keyboardShortcut(KeyEquivalent(Character(String(tab.rawValue))), modifiers: .command)
            }
            .disabled(selection == nil)
        }
    }
}
#endif
