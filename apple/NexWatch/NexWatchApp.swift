import SwiftUI
import UserNotifications

@main
struct NexWatchApp: App {
    @State private var setup = WatchSetup()

    init() {
        UNUserNotificationCenter.current().delegate = WatchNotificationPresenter.shared
    }

    var body: some Scene {
        WindowGroup {
            if let setup = setup.services {
                WatchHome(services: setup)
            } else {
                ContentUnavailableView("Recording unavailable", systemImage: "externaldrive.badge.exclamationmark",
                                       description: Text(setup.error ?? "Nex could not open its local queue."))
            }
        }
        .backgroundTask(.watchConnectivity) {
            await setup.services?.sync.deliverPendingContent()
        }
    }
}

@MainActor
private struct WatchSetup {
    var services: WatchServices?
    var error: String?

    init() {
        do { services = try WatchServices(spool: Spool()) }
        catch { self.error = error.localizedDescription }
    }
}

@MainActor
final class WatchServices {
    let capture: WatchCapture
    let sync: WatchSync
    let workspace: WatchWorkspace
    let live: WatchLiveConversation

    init(spool spoolRoot: Spool) throws {
        let spool = try WatchSync.savedScope.map { try spoolRoot.scoped(to: $0) } ?? spoolRoot
        capture = WatchCapture(spool: spool)
        sync = WatchSync(spoolRoot: spoolRoot, spool: spool)
        workspace = WatchWorkspace(link: sync)
        live = WatchLiveConversation(capture: capture, sync: sync, spool: spool)
        capture.clipReady = { [weak sync] in sync?.sync() }
        capture.recordingChanged = { [weak sync] in sync?.reportRecording() }
        sync.isRecording = { [weak capture] in capture?.isRecording ?? false }
        sync.isConversing = { [weak live] in live?.microphoneOpen == true }
        capture.setPlaybackStop { [weak workspace] in workspace?.stopPlayback() }
        sync.accountChanged = { [weak workspace, weak capture, weak live] spool in
            workspace?.lock()
            live?.useSpool(spool)
            capture?.useSpool(spool)
        }
    }
}

private struct WatchHome: View {
    @Environment(\.scenePhase) private var phase
    let services: WatchServices
    @ObservedObject private var sync: WatchSync

    init(services: WatchServices) {
        self.services = services
        sync = services.sync
    }

    var body: some View {
        NavigationStack {
            WatchRecordingView(capture: services.capture, sync: services.sync, workspace: services.workspace,
                               live: services.live)
        }
        .id(sync.scope)
        .environmentObject(services.capture)
        .environmentObject(services.live)
        .environmentObject(services.workspace)
        .onChange(of: phase, initial: true) { _, next in
            services.capture.isForeground = next != .background
            if next == .background {
                services.live.background()
                services.workspace.lock()
            }
            if next == .active {
                services.live.foreground()
                services.capture.checkEngine()
                services.sync.reportRecording()
                services.sync.sync()
            }
        }
        .task {
            while !Task.isCancelled {
                services.sync.sync()
                do { try await Task.sleep(for: .seconds(15)) }
                catch { return }
            }
        }
        .task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(8)) }
                catch { return }
                services.capture.checkEngine(checkForStall: true)
                if services.capture.isRecording || services.capture.isForeground {
                    services.sync.reportRecording()
                }
            }
        }
    }
}
