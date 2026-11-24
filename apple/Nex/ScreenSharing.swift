#if os(macOS)
import AppKit
import Combine
import Foundation
import ScreenCaptureKit

@MainActor
final class ScreenSharing: NSObject, ObservableObject {
    enum Phase {
        case idle, choosing, starting, sharing

        var action: String {
            switch self {
            case .idle: "Share Screen"
            case .choosing, .starting: "Cancel Screen Share"
            case .sharing: "Stop Sharing"
            }
        }
    }

    @Published private(set) var phase = Phase.idle
    @Published private(set) var error: String?
    @Published private(set) var looking = false
    @Published private(set) var observedAt: Date?
    @Published private(set) var suspended = false
    var contextChanged: ((String?, Date) -> Void)? {
        didSet { monitor.activity = contextChanged }
    }

    private let picker = SCContentSharingPicker.shared
    private let gemma: MacGemmaControl
    private let vision: ScreenVision
    private lazy var monitor = ScreenMonitor(vision: vision) { [weak self] time in self?.observedAt = time }
    private var stream: SCStream?
    private var frames: ScreenFrames?
    private var warming: Task<Void, Never>?
    private var question: Task<String, Error>?
    private var sleeping = false
    private var displaysSleeping = false
    private var inactiveSession = false
    private var history = [ScreenExchange]()
    private(set) var lastAnswerAt: Date?
    private var observers = Set<AnyCancellable>()

    var isSharing: Bool { phase == .sharing }
    var gemmaEnabled: Bool { gemma.phase == .enabled }

    init(gemma: MacGemmaControl) {
        self.gemma = gemma
        vision = ScreenVision(relay: gemma.relay)
        super.init()
        gemma.$phase.sink { [weak self] phase in
            if phase != .enabled { self?.stop() }
            self?.objectWillChange.send()
        }.store(in: &observers)
        NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)
            .sink { [weak self] _ in self?.stop() }.store(in: &observers)
        observeWorkspace(NSWorkspace.willSleepNotification) { $0.sleeping = true }
        observeWorkspace(NSWorkspace.didWakeNotification) { $0.sleeping = false }
        observeWorkspace(NSWorkspace.screensDidSleepNotification) { $0.displaysSleeping = true }
        observeWorkspace(NSWorkspace.screensDidWakeNotification) { $0.displaysSleeping = false }
        observeWorkspace(NSWorkspace.sessionDidResignActiveNotification) { $0.inactiveSession = true }
        observeWorkspace(NSWorkspace.sessionDidBecomeActiveNotification) { $0.inactiveSession = false }
    }

    func toggle() {
        if phase != .idle {
            stop()
            return
        }
        guard gemmaEnabled else { return }
        error = nil
        phase = .choosing
        var config = SCContentSharingPickerConfiguration()
        config.allowedPickerModes = [.singleDisplay, .singleWindow]
        config.allowsChangingSelectedContent = false
        config.excludedBundleIDs = [Bundle.main.bundleIdentifier].compactMap { $0 }
        picker.defaultConfiguration = config
        picker.add(self)
        picker.isActive = true
        picker.present()
    }

    func answer(_ question: String, language: String, spoken: Bool, personal: PersonalContext? = nil) async throws -> Reply {
        guard isSharing, !suspended, let frames else { throw CancellationError() }
        guard !looking else { throw NexError.capture("Nex is already looking at the shared screen.") }
        looking = true
        monitor.setAnswering(true)
        defer {
            if self.frames === frames {
                looking = false
                self.question = nil
                monitor.setAnswering(false)
            }
        }
        let request = Task {
            let image = await frames.snapshot()?.jpeg
            try Task.checkCancellation()
            let observations = monitor.evidence(for: question)
            guard image != nil || !observations.isEmpty else {
                throw NexError.capture("No screen context is available yet. Make the shared window visible and try again.")
            }
            return try await vision.answer(question, frame: image, history: history, observations: observations,
                                           language: language, spoken: spoken, personal: personal)
        }
        self.question = request
        let answer = try await withTaskCancellationHandler {
            try await request.value
        } onCancel: {
            request.cancel()
        }
        try Task.checkCancellation()
        guard self.frames === frames, !suspended else { throw CancellationError() }
        let text = spoken ? String(answer.unicodeScalars.prefix(1000)) : answer
        history = Array((history + [ScreenExchange(question: question, answer: text)]).suffix(3))
        lastAnswerAt = .now
        return Reply(text: text, language: language == "auto" ? "en" : language, sources: [], engine: "screen_model")
    }

    func stop() {
        guard phase != .idle else { return }
        if isSharing { contextChanged?(nil, .now) }
        phase = .idle
        let previous = stream
        stream = nil
        frames?.close()
        frames = nil
        warming?.cancel()
        warming = nil
        question?.cancel()
        question = nil
        looking = false
        observedAt = nil
        monitor.reset()
        history.removeAll()
        lastAnswerAt = nil
        vision.stop()
        picker.remove(self)
        picker.isActive = false
        if let previous { Task { try? await previous.stopCapture() } }
    }

    private func start(_ filter: SCContentFilter) async {
        guard phase == .choosing else { return }
        phase = .starting
        let output = ScreenFrames()
        let selected = SCStream(filter: filter, configuration: configuration(for: filter), delegate: self)
        stream = selected
        frames = output
        do {
            try selected.addStreamOutput(output, type: .screen, sampleHandlerQueue: output.queue)
            try await selected.startCapture()
            guard stream === selected else {
                try? await selected.stopCapture()
                return
            }
            phase = .sharing
            output.setSuspended(suspended)
            if !suspended { monitor.start(output) }
            warming = Task { [weak self] in
                guard let self else { return }
                do { try await vision.warm() }
                catch where !Task.isCancelled && self.stream === selected {
                    self.error = error.localizedDescription
                }
                catch { }
            }
        } catch {
            if stream === selected { failed("Nex couldn’t start screen sharing. Choose a visible window or display and try again.") }
        }
    }

    private func observeWorkspace(_ name: Notification.Name, update: @escaping (ScreenSharing) -> Void) {
        NSWorkspace.shared.notificationCenter.publisher(for: name)
            .receive(on: DispatchQueue.main).sink { [weak self] _ in
                guard let self else { return }
                update(self)
                self.updateAvailability()
            }.store(in: &observers)
    }

    private func updateAvailability() {
        let unavailable = sleeping || displaysSleeping || inactiveSession
        guard unavailable != suspended else { return }
        suspended = unavailable
        guard isSharing, let frames else { return }
        frames.setSuspended(unavailable)
        if unavailable {
            monitor.pause()
            contextChanged?(nil, .now)
            question?.cancel()
        } else {
            monitor.start(frames)
        }
    }

    private func configuration(for filter: SCContentFilter) -> SCStreamConfiguration {
        let config = SCStreamConfiguration()
        let size = filter.contentRect.size
        let scale = min(CGFloat(filter.pointPixelScale), 1920 / max(size.width, size.height, 1))
        config.width = max(2, Int(size.width * scale) & ~1)
        config.height = max(2, Int(size.height * scale) & ~1)
        config.minimumFrameInterval = CMTime(value: 1, timescale: 3)
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.queueDepth = 3
        config.showsCursor = true
        config.capturesAudio = false
        return config
    }

    private func failed(_ message: String) {
        stop()
        error = message
    }
}

extension ScreenSharing: SCContentSharingPickerObserver, SCStreamDelegate {
    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                                          for stream: SCStream?) {
        guard stream == nil else { return }
        Task { @MainActor [weak self] in await self?.start(filter) }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor [weak self] in
            guard let self, self.phase == .choosing || (stream != nil && self.stream === stream) else { return }
            self.stop()
        }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor [weak self] in
            guard self?.phase == .choosing else { return }
            self?.failed("macOS couldn’t open the screen sharing picker.")
        }
    }

    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        let userStopped = (error as? SCStreamError)?.code == .userStopped
        Task { @MainActor [weak self] in
            guard let self, self.stream === stream else { return }
            self.stop()
            if !userStopped {
                self.error = "macOS ended the screen share. Choose the source again to resume."
            }
        }
    }
}
#endif
