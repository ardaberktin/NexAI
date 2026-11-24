import Foundation

@main
struct AppAutoLockCheck {
    @MainActor
    static func main() async throws {
        let suite = "Nex.AppAutoLockCheck.\(UUID())"
        guard let defaults = UserDefaults(suiteName: suite) else { fatalError("Could not create test preferences") }
        defer { defaults.removePersistentDomain(forName: suite) }
        let policy = AppAutoLock(defaults: defaults)
        let start = ContinuousClock.now
        var locks = 0
        let lock: @MainActor () -> Void = { locks += 1 }

        precondition(policy.delay == .immediately)
        precondition(policy.takeAutomaticUnlockAttempt())
        precondition(!policy.takeAutomaticUnlockAttempt())
        policy.leave(at: start, lock: lock)
        policy.enter(at: start, lock: lock)
        precondition(locks == 1)
        precondition(policy.takeAutomaticUnlockAttempt())

        let custom = ReauthenticationDelay(seconds: 75)
        policy.setDelay(custom)
        precondition(AppAutoLock(defaults: defaults).delay == custom)
        policy.leave(at: start, lock: lock)
        precondition(policy.isAway && !policy.takeAutomaticUnlockAttempt())
        policy.enter(at: start + .seconds(74), lock: lock)
        precondition(!policy.isAway && locks == 1)

        policy.leave(at: start, lock: lock)
        policy.leave(at: start + .seconds(20), lock: lock)
        policy.enter(at: start + .seconds(75), lock: lock)
        precondition(locks == 2, "Duplicate away events must keep the original deadline")

        policy.setDelay(.noAwayTimer)
        policy.leave(at: start, lock: lock)
        policy.enter(at: start + .seconds(100_000), lock: lock)
        precondition(locks == 2)
        precondition(AppAutoLock(defaults: defaults).delay == .noAwayTimer)
        precondition(ReauthenticationDelay(seconds: -2) == .immediately)
        precondition(ReauthenticationDelay(seconds: ReauthenticationDelay.maximumSeconds + 1) == .immediately)

        policy.setDelay(ReauthenticationDelay(seconds: 1))
        policy.leave(lock: lock)
        try await Task.sleep(for: .milliseconds(1200))
        precondition(locks == 3, "Nex must lock if it can run when the timer expires")
        policy.enter(lock: lock)
        precondition(locks == 3, "Returning after expiry must not lock again")

        policy.leave(lock: lock)
        policy.enter(lock: lock)
        try await Task.sleep(for: .milliseconds(1200))
        precondition(locks == 3, "A quick return must cancel the scheduled lock")
        print("App auto-lock checks passed")
    }
}
