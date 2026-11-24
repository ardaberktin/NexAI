import Foundation
import UserNotifications

@main
@MainActor
enum RecordingNotificationsCheck {
    static func main() async {
        await checkQuietStops()
        await checkWatchFallback()
        await checkRecoveryWhenRearmingFails()
        print("Recording alert grace, cancellation, and Watch audio deadlines passed.")
    }

    private static func checkQuietStops() async {
        let delivery = NotificationProbe()
        let alerts = delivery.alerts(for: .iphone)
        alerts.interrupted()
        alerts.clear()
        await wait { delivery.cancellations == 1 }
        precondition(delivery.requests.isEmpty, "A quick recovery should cancel the alert.")

        delivery.holdNextAdd = true
        alerts.interrupted()
        await wait { delivery.addInProgress != nil }
        alerts.clear()
        delivery.addInProgress?.resume()
        await wait { delivery.cancellations == 2 }
        precondition(delivery.pending == nil, "Pausing must cancel an add that was already in flight.")

        alerts.interrupted()
        alerts.interrupted()
        await wait { delivery.requests.count == 2 }
        precondition(delivery.pending?.content.title == "Recording stopped on iPhone")
        precondition(delivery.delay == 10)
    }

    private static func checkWatchFallback() async {
        let delivery = NotificationProbe()
        let alerts = delivery.alerts(for: .watch)
        alerts.watchRecordingStarted(at: delivery.time)
        await wait { delivery.requests.count == 1 }
        precondition(delivery.pending?.content.title == "Check your Watch recording")
        precondition(delivery.delay == 60)

        let firstFrame = delivery.time
        delivery.time += .seconds(8)
        alerts.watchAudioReceived(at: firstFrame)
        alerts.watchAudioReceived(at: delivery.time)
        await wait { delivery.requests.count == 2 }
        precondition(delivery.delay == 60)
        precondition(delivery.requests.allSatisfy { $0.identifier == RecordingNotifications.identifier })

        alerts.interrupted()
        await wait { delivery.requests.count == 3 }
        precondition(delivery.pending?.content.title == "Recording stopped on Apple Watch")
        delivery.time += .seconds(2)
        alerts.watchRecordingStarted(at: delivery.time)
        await Task.yield()
        precondition(delivery.requests.count == 3, "Starting an engine alone should not count as recovery.")
        alerts.watchAudioReceived(at: delivery.time)
        await wait { delivery.requests.count == 4 }
        precondition(delivery.pending?.content.title == "Check your Watch recording")
        precondition(delivery.delay == 60)

        delivery.time += .seconds(61)
        alerts.interrupted()
        await Task.yield()
        precondition(delivery.requests.count == 4, "Do not repeat an alert after the fallback's deadline.")
        alerts.clear()
        await wait { delivery.cancellations == 1 }
        precondition(delivery.pending == nil)
    }

    private static func checkRecoveryWhenRearmingFails() async {
        let delivery = NotificationProbe()
        let alerts = delivery.alerts(for: .watch)
        alerts.interrupted()
        await wait { delivery.pending != nil }
        delivery.failNextAdd = true
        alerts.watchAudioReceived(at: delivery.time)
        await wait { delivery.cancellations == 1 }
        precondition(delivery.pending == nil, "Recovered audio must clear the old alert even if rearming fails.")
    }

    private static func wait(_ condition: () -> Bool) async {
        let deadline = ContinuousClock.now + .seconds(2)
        while !condition() && ContinuousClock.now < deadline { await Task.yield() }
        precondition(condition(), "The notification update did not complete.")
    }
}

@MainActor
private final class NotificationProbe {
    var time = ContinuousClock.now
    var holdNextAdd = false
    var failNextAdd = false
    var addInProgress: CheckedContinuation<Void, Never>?
    private(set) var pending: UNNotificationRequest?
    private(set) var requests: [UNNotificationRequest] = []
    private(set) var cancellations = 0

    var delay: TimeInterval? { (pending?.trigger as? UNTimeIntervalNotificationTrigger)?.timeInterval }

    func alerts(for device: RecordingDevice) -> RecordingNotifications {
        RecordingNotifications(device: device, now: { self.time }, schedule: { request in
            if self.failNextAdd {
                self.failNextAdd = false
                throw UNError(.notificationsNotAllowed)
            }
            if self.holdNextAdd {
                self.holdNextAdd = false
                await withCheckedContinuation { self.addInProgress = $0 }
                self.addInProgress = nil
            }
            self.requests.append(request)
            self.pending = request
        }, cancel: { id in
            precondition(id == RecordingNotifications.identifier)
            self.cancellations += 1
            self.pending = nil
        })
    }
}
