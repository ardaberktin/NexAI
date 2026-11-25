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

enum MCPToolAccess: String, Codable, CaseIterable, Identifiable {
    case off, read, write
    case readWrite = "read_write"

    var id: Self { self }
    var allowsRead: Bool { self == .read || self == .readWrite }
    var allowsWrite: Bool { self == .write || self == .readWrite }

    var title: LocalizedStringKey {
        switch self {
        case .off: "Off"
        case .read: "Read only"
        case .write: "Write only"
        case .readWrite: "Read & write"
        }
    }
}

struct MCPConnection: Decodable, Identifiable {
    struct Tool: Decodable, Identifiable {
        let name: String
        let readOnly: Bool
        let access: MCPToolAccess?
        let allowGoalWrites: Bool?

        var id: String { name }
        var selectedAccess: MCPToolAccess { access ?? (readOnly ? .read : .off) }
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
    var hasReadTools: Bool { tools.contains { $0.readOnly && $0.selectedAccess.allowsRead } }
}

struct MCPConnectionDraft: Encodable {
    let id = nexID()
    var name = ""
    var url = ""
    var transport = MCPTransport.http
    var headers: [String: String] = [:]

    private enum CodingKeys: String, CodingKey { case id, name, url, transport, headers }
}

private struct MCPToolAccessUpdate: Encodable {
    let tool: String
    let access: MCPToolAccess?
    let allowGoalWrites: Bool?

    enum CodingKeys: String, CodingKey {
        case tool, access
        case allowGoalWrites = "allow_goal_writes"
    }
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

    func updateMCPTool(
        _ item: MCPConnection, tool: MCPConnection.Tool,
        access: MCPToolAccess? = nil, allowGoalWrites: Bool? = nil
    ) async throws -> MCPConnection {
        let update = MCPToolAccessUpdate(tool: tool.name, access: access, allowGoalWrites: allowGoalWrites)
        return try await connection().send(item.path + "/tool-access", method: "PUT", body: update)
    }

    func removeMCPConnection(_ item: MCPConnection) async throws {
        let _: [String: Bool] = try await connection().send(item.path + "/remove", body: [String: String]())
    }
}
