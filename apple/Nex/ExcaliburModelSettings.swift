import SwiftUI

private struct ExcaliburModels: Decodable {
    struct Option: Decodable, Identifiable {
        let id: String
        let name: String
        let available: Bool
    }

    let active: String?
    let device: String?
    let models: [Option]
}

struct ExcaliburModelSettings: View {
    let model: Controller
    @State private var status: ExcaliburModels?
    @State private var pending = false
    @State private var error: String?

    var body: some View {
        Section("Excalibur language model") {
            if let status {
                Picker("Model for chat and voice", selection: Binding(get: { status.active ?? "" }, set: { choice in
                    Task { await request(choice) }
                })) {
                    if status.active == nil { Text("Starting…").tag("") }
                    ForEach(status.models) { option in
                        Text(option.available ? option.name : option.name + " (not installed)")
                            .tag(option.id).disabled(!option.available)
                    }
                }
                .disabled(pending || model.pairing?.account?.owner == false)
                if let device = status.device { LabeledContent("Running on", value: device) }
            }
            if pending { ProgressView(status == nil ? "Checking models…" : "Switching model…") }
            if let error {
                Text(error).foregroundStyle(.secondary)
                Button("Refresh models") { Task { await request() } }.disabled(pending)
            }
        }
        .task(id: model.pairing?.workspaceScope) { await request() }
    }

    @MainActor
    private func request(_ choice: String? = nil) async {
        guard !pending else { return }
        pending = true
        error = nil
        defer { pending = false }
        do {
            let body = try choice.map { try JSONEncoder().encode(["model": $0]) }
            let data = try await model.connection().bytes("/v1/live-model", method: choice == nil ? "GET" : "PUT",
                                                         body: body, timeout: choice == nil ? 12 : 200, port: 8767)
            status = try JSONDecoder().decode(ExcaliburModels.self, from: data)
        } catch {
            if !Task.isCancelled { self.error = error.localizedDescription }
        }
    }
}
