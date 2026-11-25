import Combine
import CoreLocation
#if os(iOS)
import UIKit
#endif

@MainActor
final class WeatherSharing: NSObject, ObservableObject, @preconcurrency CLLocationManagerDelegate {
    @Published private(set) var enabled = false
    @Published private(set) var busy = false
    @Published private(set) var status: String?

    private struct Preference: Codable {
        let pin: String
        let consent: String
        var enabled: Bool
    }

    private struct Consent: Codable { let consent: String }
    private struct SharingResult: Decodable { let shared: Bool }
    private struct ConsentRequest: Encodable {}
    private struct LocationFix: Encodable {
        let consent: String
        let latitude: Double
        let longitude: Double
        let observed_at: Double
    }

    private let base = "/v1/workspace/weather-location"
    private let preferenceKey = "nex.weatherSharing"
    private let manager = CLLocationManager()
    private var profile: ServerProfile?
    private var preference: Preference?
    private var foreground = false
    private var clearing: Task<Void, Never>?
    private var sending: Task<Void, Never>?
    private var clearAfter = Date.distantPast

    override init() {
        super.init()
        if let data = UserDefaults.standard.data(forKey: preferenceKey) {
            preference = try? JSONDecoder().decode(Preference.self, from: data)
        }
        manager.desiredAccuracy = kCLLocationAccuracyReduced
        manager.delegate = self
    }

    func connect(_ profile: ServerProfile?) {
        self.profile = profile
        enabled = profile?.workspaceScope == preference?.pin && preference?.enabled == true
        configureLocation()
        retryClear()
    }

    func setForeground(_ active: Bool) {
        foreground = active
        configureLocation()
        if active { retryClear() }
    }

    func setEnabled(_ value: Bool, using model: Controller) async {
        guard !busy, let profile = model.serverProfile else { return }
        busy = true
        defer { busy = false }
        status = nil
        do {
            let api = try model.connection()
            if value {
                if [.denied, .restricted].contains(manager.authorizationStatus) {
                    status = "Allow location access for Nex in this device’s system settings."
                    return
                }
                await clearing?.value
                if let preference, preference.pin == profile.workspaceScope, !preference.enabled {
                    try await clear(preference.consent, using: api)
                    save(nil)
                }
                let result: Consent = try await api.send(base, method: "PUT", body: ConsentRequest())
                guard model.serverProfile?.workspaceScope == profile.workspaceScope else { return }
                save(Preference(pin: profile.workspaceScope, consent: result.consent, enabled: true))
                requestPermission()
                configureLocation()
            } else {
                turnOff()
                retryClear()
            }
        } catch {
            status = error.localizedDescription
        }
    }

    func disconnect() {
        turnOff()
        retryClear()
        profile = nil
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if enabled && [.denied, .restricted].contains(manager.authorizationStatus) {
            turnOff()
            retryClear()
            status = "Location access is off in this device’s system settings."
        }
        configureLocation()
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard enabled, let profile, let preference, preference.pin == profile.workspaceScope,
              let location = locations.last, CLLocationCoordinate2DIsValid(location.coordinate),
              location.horizontalAccuracy >= 0, abs(location.timestamp.timeIntervalSinceNow) < 900 else { return }
        let rounded = WeatherLocationResult(id: "shared", location: location)
        guard let latitude = rounded.latitude, let longitude = rounded.longitude else { return }
        let fix = LocationFix(consent: preference.consent, latitude: latitude, longitude: longitude,
                              observed_at: location.timestamp.timeIntervalSince1970)
        sending?.cancel()
        sending = Task { await upload(fix, profile: profile) }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if enabled && foreground { status = "Waiting for a location update." }
    }

    private func requestPermission() {
        let authorization = manager.authorizationStatus
        #if os(iOS)
        if authorization == .notDetermined || authorization == .authorizedWhenInUse {
            manager.requestAlwaysAuthorization()
        }
        #else
        if authorization == .notDetermined { manager.requestWhenInUseAuthorization() }
        #endif
    }

    private func configureLocation() {
        let authorization = manager.authorizationStatus
        #if os(iOS)
        if enabled && authorization == .authorizedAlways && CLLocationManager.significantLocationChangeMonitoringAvailable() {
            manager.startMonitoringSignificantLocationChanges()
        } else {
            manager.stopMonitoringSignificantLocationChanges()
        }
        let authorized = authorization == .authorizedAlways || authorization == .authorizedWhenInUse
        #else
        let authorized = authorization == .authorizedAlways
        #endif
        if !enabled || !foreground { manager.stopUpdatingLocation() }
        if enabled && authorized && foreground { manager.requestLocation() }
        if enabled && authorization == .notDetermined { status = "Waiting for location permission." }
    }

    private func turnOff() {
        guard var preference, preference.pin == profile?.workspaceScope else { return }
        preference.enabled = false
        save(preference)
        sending?.cancel()
        clearAfter = .distantPast
        configureLocation()
    }

    private func save(_ value: Preference?) {
        preference = value
        UserDefaults.standard.set(value.flatMap { try? JSONEncoder().encode($0) }, forKey: preferenceKey)
        enabled = value?.enabled == true
    }

    private func clear(_ consent: String, using api: NexAPI) async throws {
        let _: SharingResult = try await api.send(base + "/clear", method: "PUT", body: Consent(consent: consent))
    }

    func retryClear() {
        guard clearing == nil, Date.now >= clearAfter, let profile, let preference,
              preference.pin == profile.workspaceScope, !preference.enabled else {
            return
        }
        clearing = Task {
            defer { clearing = nil }
            do {
                try await clear(preference.consent, using: NexAPI(profile))
                if self.preference?.consent == preference.consent && !enabled {
                    save(nil)
                    status = nil
                }
            } catch {
                if self.preference?.consent == preference.consent && !enabled {
                    clearAfter = .now.addingTimeInterval(30)
                    status = "Location sharing is off. Excalibur will clear the last location after this device reconnects."
                }
            }
        }
    }

    private func upload(_ fix: LocationFix, profile: ServerProfile) async {
        #if os(iOS)
        let background = UIApplication.shared.beginBackgroundTask(withName: "Nex weather location")
        defer { if background != .invalid { UIApplication.shared.endBackgroundTask(background) } }
        #endif
        do {
            let result: SharingResult = try await NexAPI(profile).send(base + "/fix", method: "PUT", body: fix, timeout: 8)
            if !Task.isCancelled && enabled && result.shared { status = "Sharing approximate location with Excalibur." }
        } catch {
            if !Task.isCancelled && enabled { status = "Couldn’t share location. Nex will try again on the next update." }
        }
    }
}
