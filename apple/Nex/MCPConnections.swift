import SwiftUI

enum MCPTransport: String, Codable, CaseIterable, Identifiable {
    case http, sse

    var id: Self { self }

    var title: LocalizedStringKey {
        switch self {
        case .http: "Streamable HTTP"
        case .sse: "Legacy HTTP with SSE"
        }
    }
}

struct MCPConnection: Decodable, Identifiable {
    struct Tool: Decodable, Identifiable {
        let name: String
        let readOnly: Bool

        var id: String { name }
    }

    let id: String
    let name: String
    let host: String
    let transport: MCPTransport
    let enabled: Bool
    let allowGoals: Bool
    let hasCredentials: Bool
    let tools: [Tool]

    var path: String { "/v1/workspace/mcp/" + id }
    var readTools: [Tool] { tools.filter(\.readOnly) }
}

struct MCPConnectionDraft: Encodable {
    let id = nexID()
    var name = ""
    var url = ""
    var transport = MCPTransport.http
    var headers: [String: String] = [:]

    private enum CodingKeys: String, CodingKey { case id, name, url, transport, headers }
}

@MainActor
extension Controller {
    func mcpConnections() async throws -> [MCPConnection] {
        let result: ListResponse<MCPConnection> = try await connection().get("/v1/workspace/mcp")
        return result.items
    }

    func addMCPConnection(_ draft: MCPConnectionDraft) async throws -> MCPConnection {
        try await connection().send("/v1/workspace/mcp", body: draft, timeout: 25)
    }

    func refreshMCPConnection(_ item: MCPConnection) async throws -> MCPConnection {
        try await connection().send(item.path + "/refresh", body: [String: String](), timeout: 25)
    }

    func updateMCPConnection(_ item: MCPConnection, changes: [String: Bool]) async throws -> MCPConnection {
        try await connection().send(item.path, method: "PUT", body: changes)
    }

    func removeMCPConnection(_ item: MCPConnection) async throws {
        let _: [String: Bool] = try await connection().send(item.path + "/remove", body: [String: String]())
    }
}
