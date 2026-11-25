import SwiftUI

struct PhoneWatchSettings: View {
    @ObservedObject var model: Controller
    @ObservedObject private var relay = PhoneWatchRelay.shared

    var body: some View {
        Section {
            Toggle("Allow Nex on Apple Watch", isOn: Binding(
                get: { relay.enabled },
                set: { relay.setEnabled($0) }
            ))
            .disabled(model.serverProfile == nil)
            if let error = relay.syncError {
                Text(error).foregroundStyle(.red)
            }
        } footer: {
            Text("The paired watch can sync its microphone recordings through this iPhone. To open the private workspace on your watch, unlock Nex on iPhone first.")
        }
    }
}
