import SwiftUI

struct MCPSettings: View {
    @ObservedObject var model: Controller
    @ObservedObject private var privacy: AppPrivacy
    @State private var items: [MCPConnection] = []
    @State private var error: String?
    @State private var operation: UUID?
    @State private var adding = false
    @State private var removing: MCPConnection?

    init(model: Controller) {
        self.model = model
        privacy = model.privacy
    }

    private var busy: Bool { operation != nil }

    private var scope: String? {
        privacy.connected ? model.serverProfile?.account.deviceId : nil
    }

    var body: some View {
        Section {
            if scope != nil {
                ForEach(items) { item in
                    DisclosureGroup {
                        controls(item).disabled(busy)
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(item.name)
                            Text(item.enabled ? "\(item.tools.count) tools · \(item.host)" : "Paused · \(item.host)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Button("Add MCP Server", systemImage: "plus") { adding = true }
                    .disabled(busy)
                if busy { ProgressView("Contacting Nex…") }
                if let error {
                    Text(error).foregroundStyle(.red)
                    Button("Reload Connections") { Task { await load() } }.disabled(busy)
                }
            } else if model.serverProfile == nil {
                Text("Sign in to Nex to connect an MCP server.").foregroundStyle(.secondary)
            } else {
                Text("Unlock your workspace to manage MCP servers.").foregroundStyle(.secondary)
            }
        } header: {
            Text("MCP Servers")
        } footer: {
            Text("Nex can use connected tools in Chat and approved voice requests. Tool inputs are sent to the connected server.")
        }
        .task(id: scope) { await load() }
        .onChange(of: scope) {
            items = []
            error = nil
            adding = false
            removing = nil
            operation = nil
        }
        .sheet(isPresented: $adding) {
            MCPConnectionEditor { draft in
                guard let scope else { throw CancellationError() }
                let generation = privacy.generation
                let item = try await model.addMCPConnection(draft)
                guard self.scope == scope, generation == privacy.generation else { throw CancellationError() }
                replace(item)
            }
        }
        .confirmationDialog("Remove MCP server?", isPresented: Binding(
            get: { removing != nil }, set: { if !$0 { removing = nil } }
        ), presenting: removing) { item in
            Button("Remove \(item.name)", role: .destructive) {
                Task {
                    await perform {
                        try await model.removeMCPConnection(item)
                        return items.filter { $0.id != item.id }
                    }
                }
            }
        } message: { _ in
            Text("Nex will lose access to these tools, including from goals that use them.")
        }
    }

    @ViewBuilder
    private func controls(_ item: MCPConnection) -> some View {
        LabeledContent("Transport") { Text(item.transport.title) }
        LabeledContent("Authentication") { Text(item.hasCredentials ? "Configured" : "None") }
        Toggle("Enabled", isOn: setting(item.enabled, item: item, key: "enabled"))
        Toggle("Allow read-only tools in goals", isOn: setting(item.allowGoals, item: item, key: "allow_goals"))
            .disabled(!item.enabled || (item.readTools.isEmpty && !item.allowGoals))
        ForEach(item.tools) { tool in
            LabeledContent(tool.name) {
                Text(tool.readOnly ? "Read only" : "May make changes")
                    .foregroundStyle(.secondary)
            }
        }
        Button("Refresh Tools", systemImage: "arrow.clockwise") {
            Task { await perform { replacing(try await model.refreshMCPConnection(item)) } }
        }
        Button("Remove Server", role: .destructive) { removing = item }
    }

    private func setting(_ value: Bool, item: MCPConnection, key: String) -> Binding<Bool> {
        Binding(get: { value }, set: { next in
            Task { await perform { replacing(try await model.updateMCPConnection(item, changes: [key: next])) } }
        })
    }

    private func replacing(_ item: MCPConnection) -> [MCPConnection] {
        (items.filter { $0.id != item.id } + [item]).sorted {
            $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
    }

    private func replace(_ item: MCPConnection) {
        items = replacing(item)
    }

    private func load() async {
        await perform { try await model.mcpConnections() }
    }

    private func perform(_ action: () async throws -> [MCPConnection]) async {
        guard let scope, !busy else { return }
        let generation = privacy.generation
        let request = UUID()
        operation = request
        error = nil
        defer { if operation == request { operation = nil } }
        do {
            let updated = try await action()
            guard !Task.isCancelled, scope == self.scope, generation == privacy.generation else { return }
            items = updated
        } catch {
            guard !Task.isCancelled, scope == self.scope, generation == privacy.generation else { return }
            self.error = error.localizedDescription
        }
    }
}
