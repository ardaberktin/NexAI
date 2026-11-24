import Combine
import Foundation

struct ReauthenticationDelay: Equatable {
    static let immediately = Self(seconds: 0)
    static let noAwayTimer = Self(seconds: -1)
    static let maximumSeconds = 12 * 60 * 60

    let seconds: Int

    init(seconds: Int) {
        self.seconds = (-1...Self.maximumSeconds).contains(seconds) ? seconds : 0
    }

    func deadline(from start: ContinuousClock.Instant) -> ContinuousClock.Instant? {
        guard self != .noAwayTimer else { return nil }
        return start + .seconds(seconds)
    }
}

@MainActor
final class AppAutoLock: ObservableObject {
    private static let preferenceKey = "biometricReauthenticationDelaySeconds"

    @Published private(set) var delay: ReauthenticationDelay
    @Published private(set) var isAway = false
    private let defaults: UserDefaults
    private var deadline: ContinuousClock.Instant?
    private var expiry: Task<Void, Never>?
    private var attemptedUnlock = false

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        delay = ReauthenticationDelay(seconds: defaults.object(forKey: Self.preferenceKey) as? Int ?? 0)
    }

    func setDelay(_ delay: ReauthenticationDelay) {
        self.delay = delay
        defaults.set(delay.seconds, forKey: Self.preferenceKey)
    }

    func leave(at now: ContinuousClock.Instant = .now, lock: @escaping @MainActor () -> Void) {
        guard !isAway else { return }
        isAway = true
        attemptedUnlock = false
        guard let deadline = delay.deadline(from: now) else { return }
        self.deadline = deadline
        if deadline <= now {
            lockIfDue(at: now, lock: lock)
        } else {
            expiry = Task { [weak self] in
                do { try await Task.sleep(until: deadline, clock: .continuous) }
                catch { return }
                guard !Task.isCancelled else { return }
                self?.lockIfDue(at: .now, lock: lock)
            }
        }
    }

    func enter(at now: ContinuousClock.Instant = .now, lock: () -> Void) {
        guard isAway else { return }
        // iOS can suspend the expiry task while Nex is in the background.
        lockIfDue(at: now, lock: lock)
        cancelExpiry()
        isAway = false
    }

    func cancelExpiry() {
        expiry?.cancel()
        expiry = nil
        deadline = nil
    }

    func takeAutomaticUnlockAttempt() -> Bool {
        guard !isAway, !attemptedUnlock else { return false }
        attemptedUnlock = true
        return true
    }

    private func lockIfDue(at now: ContinuousClock.Instant, lock: () -> Void) {
        guard let deadline, deadline <= now else { return }
        self.deadline = nil
        lock()
    }
}
