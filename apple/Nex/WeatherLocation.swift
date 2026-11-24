import CoreLocation
#if os(iOS)
import UIKit
#else
import AppKit
#endif

struct WeatherLocationResult: Encodable {
    let kind = "weather_location_result"
    let id: String
    let latitude: Double?
    let longitude: Double?

    init(id: String, location: CLLocation? = nil) {
        self.id = id
        latitude = location.map { ($0.coordinate.latitude * 100).rounded() / 100 }
        longitude = location.map { ($0.coordinate.longitude * 100).rounded() / 100 }
    }

    @MainActor
    static func current(for id: String) async -> Self {
        Self(id: id, location: await WeatherLocation().current())
    }
}

/// Each weather question gets its own location request and cancellation.
@MainActor
final class WeatherLocation: NSObject, @preconcurrency CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var completion: CheckedContinuation<CLLocation?, Never>?
    private var timeout: Task<Void, Never>?
    private var requesting = false
    private var retried = false

    func current() async -> CLLocation? {
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                guard !Task.isCancelled else {
                    continuation.resume(returning: nil)
                    return
                }
                completion = continuation
                manager.desiredAccuracy = kCLLocationAccuracyReduced
                manager.delegate = self
                timeout = Task { [weak self] in
                    do { try await Task.sleep(for: .seconds(22)) }
                    catch { return }
                    self?.finish(nil)
                }
                if manager.authorizationStatus == .notDetermined {
                    #if os(iOS)
                    let active = UIApplication.shared.applicationState == .active
                    #else
                    let active = NSApp.isActive
                    #endif
                    guard active else {
                        finish(nil)
                        return
                    }
                    manager.requestWhenInUseAuthorization()
                } else {
                    locationManagerDidChangeAuthorization(manager)
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.finish(nil) }
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        guard completion != nil, status != .notDetermined else { return }
        #if os(iOS)
        let authorized = status == .authorizedWhenInUse || status == .authorizedAlways
        #else
        let authorized = status == .authorizedAlways
        #endif
        guard authorized else { finish(nil); return }
        if let recent = usable(manager.location) { finish(recent); return }
        guard !requesting else { return }
        requesting = true
        manager.requestLocation()
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        if let location = locations.reversed().compactMap(usable).first { finish(location) }
        else { retryOnce() }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if (error as? CLError)?.code == .locationUnknown { retryOnce() }
        else { finish(nil) }
    }

    private func usable(_ location: CLLocation?) -> CLLocation? {
        guard let location, CLLocationCoordinate2DIsValid(location.coordinate), location.horizontalAccuracy >= 0,
              abs(location.timestamp.timeIntervalSinceNow) <= 300 else { return nil }
        return location
    }

    private func retryOnce() {
        guard completion != nil, !retried else { finish(nil); return }
        retried = true
        manager.requestLocation()
    }

    private func finish(_ location: CLLocation?) {
        guard let completion else { return }
        self.completion = nil
        timeout?.cancel()
        timeout = nil
        manager.delegate = nil
        manager.stopUpdatingLocation()
        completion.resume(returning: location)
    }
}

/// A single outstanding live request is cancelled whenever the turn changes.
@MainActor
final class LiveWeatherLocation {
    private var work: Task<Void, Never>?

    func respond(to id: String, over socket: URLSessionWebSocketTask) {
        cancel()
        work = Task {
            let result = await WeatherLocationResult.current(for: id)
            guard !Task.isCancelled, let data = try? JSONEncoder().encode(result) else { return }
            try? await socket.send(.string(String(decoding: data, as: UTF8.self)))
        }
    }

    func cancel() {
        work?.cancel()
        work = nil
    }
}
