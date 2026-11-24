#if os(macOS)
import SwiftUI

@MainActor
final class MacGemmaControl: ObservableObject {
    enum Phase: Equatable {
        case enabled, disabled, enabling, unloading
        case failed(enabling: Bool, message: String)
    }

    let relay: MacGemmaRelay
    @Published private(set) var phase: Phase

    init(relay: MacGemmaRelay = .local) {
        self.relay = relay
        phase = relay.isEnabled ? .enabled : .unloading
        if phase == .unloading {
            // Finish an interrupted shutdown before allowing the model to be used again.
            Task { await apply(enabled: false) }
        }
    }

    func setEnabled(_ enabled: Bool) async {
        guard phase != .enabling, phase != .unloading else { return }
        phase = enabled ? .enabling : .unloading
        await apply(enabled: enabled)
    }

    private func apply(enabled: Bool) async {
        do {
            try await Task.detached(priority: .userInitiated) { [relay] in
                if enabled { try relay.enable() }
                else { try relay.disable() }
            }.value
            phase = enabled ? .enabled : .disabled
        } catch {
            phase = .failed(enabling: enabled, message: error.localizedDescription)
        }
    }
}

struct MacGemmaSettings: View {
    @ObservedObject var gemma: MacGemmaControl
    @Environment(\.scenePhase) private var scene
    @State private var running: Bool?

    var body: some View {
        Section {
            switch gemma.phase {
            case .enabled:
                LabeledContent("Gemma 4 12B", value: enabledStatus)
                    .task(id: scene) { await refreshProcessStatus() }
                action("Disable and Unload Gemma", enable: false)
            case .disabled:
                LabeledContent("Gemma 4 12B", value: "Disabled and unloaded")
                action("Enable Gemma", enable: true)
            case .enabling, .unloading:
                HStack {
                    ProgressView().controlSize(.small)
                    Text(gemma.phase == .enabling ? "Enabling Gemma…" : "Unloading Gemma…")
                }
            case .failed(let enabling, let message):
                Text(message).foregroundStyle(.red)
                action(enabling ? "Retry Enabling Gemma" : "Retry Unloading Gemma", enable: enabling)
                action(enabling ? "Disable and Unload Gemma" : "Enable Gemma", enable: !enabling)
            }
        } header: {
            Text("Local model on this Mac")
        } footer: {
            Text("Disabling Gemma ends screen sharing and keeps the model off until you enable it again.")
        }
    }

    private func action(_ title: String, enable: Bool) -> some View {
        Button(title) { Task { await gemma.setEnabled(enable) } }
    }

    private var enabledStatus: String {
        switch running {
        case true: "Enabled · running"
        case false: "Enabled · unloaded"
        case nil: "Enabled"
        }
    }

    private func refreshProcessStatus() async {
        running = nil
        guard scene == .active else { return }
        let processes = gemma.relay.processes
        while !Task.isCancelled {
            let active = await Task.detached(priority: .utility) { try? !processes.running().isEmpty }.value
            guard !Task.isCancelled else { return }
            running = active
            do { try await Task.sleep(for: .seconds(5)) }
            catch { return }
        }
    }
}
#endif
