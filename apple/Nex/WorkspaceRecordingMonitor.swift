import SwiftUI

struct WorkspaceRecordingMonitor: ViewModifier {
    @Environment(\.scenePhase) private var scene
    @ObservedObject var model: Controller
    @State private var presence = RecordingPresence()

    func body(content: Content) -> some View {
        content
            .environment(\.recordingPresence, presence)
            .task(id: scene != .background && model.pairing != nil) {
                guard scene != .background, model.pairing != nil else {
                    presence = RecordingPresence()
                    return
                }
                await observe()
            }
    }

    private func observe() async {
        var previous: String?
        while !Task.isCancelled {
            let query = previous.map { [URLQueryItem(name: "previous", value: $0)] } ?? []
            do {
                let report: RecordingStatusSnapshot = try await model.connection().get(
                    RecordingStatusSnapshot.path, query: query, timeout: 20)
                guard !Task.isCancelled else { return }
                presence = RecordingPresence(snapshot: report, receivedAt: .now)
                previous = report.cursor
            } catch {
                previous = nil
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
    }
}
