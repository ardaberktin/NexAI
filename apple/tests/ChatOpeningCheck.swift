import SwiftUI

// Stand-in for the workspace styling used by the shared Chat views.
enum WorkspaceStyle {
    static let conversationWidth: CGFloat = 900
    static let inset: CGFloat = 28
}

@main
struct ChatOpeningCheck: App {
    var body: some Scene {
        WindowGroup {
            OpeningFixture()
        }
        #if os(macOS)
        .defaultSize(width: 944, height: 660)
        #endif
    }
}

private struct OpeningFixture: View {
    @State private var phase: Phase = .populated
    @State private var measurement = Measurement()

    var body: some View {
        VStack(spacing: 0) {
            if phase != .closed {
                thread
            }
            Text("Composer").frame(height: 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task { await checkOpening() }
    }

    private var thread: some View {
        let newest = phase == .populated ? "39" : nil
        return ChatThreadScrollView(
            firstMessageID: newest.map { _ in "0" }, latestMessageID: newest, isSending: false,
            nextBefore: newest.map { _ in "older" }, loadOlder: {}, refresh: {}
        ) {
            if newest == nil { ProgressView("Opening Chat…") }
            ForEach(0..<(newest == nil ? 0 : 40), id: \.self) { row in
                VStack(alignment: .leading, spacing: 5) {
                    Text("Row \(row)").font(.headline)
                    MessageBody(String(repeating: "One test line of conversation.\n", count: row == 0 ? 1_000 : 1))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .id(String(row))
                .onGeometryChange(for: CGFloat.self) { geometry in
                    guard row == 39, let bounds = geometry.bounds(of: .scrollView) else { return 0 }
                    return max(0, min(bounds.maxY, geometry.size.height) - max(0, bounds.minY))
                } action: { visibleHeight in
                    if row == 39 { measurement.visibleHeight = visibleHeight }
                }
            }
        } pagingStatus: { EmptyView() }
    }

    private func checkOpening() async {
        do {
            try await Task.sleep(for: .milliseconds(500))
            let populated = measurement.visibleHeight >= 16

            phase = .closed
            try await Task.sleep(for: .milliseconds(200))
            measurement.visibleHeight = 0
            phase = .loading
            try await Task.sleep(for: .milliseconds(200))
            phase = .populated
            try await Task.sleep(for: .milliseconds(500))
            let loaded = measurement.visibleHeight >= 16

            let result = "Latest message visible on open: \(populated). Visible after loading: \(loaded).\n"
            FileHandle.standardError.write(Data(result.utf8))
            exit(populated && loaded ? 0 : 1)
        } catch is CancellationError {
            return
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(1)
        }
    }

    private enum Phase {
        case closed, loading, populated
    }

    private final class Measurement {
        var visibleHeight: CGFloat = 0
    }
}
