import Contacts
import CoreLocation
import CryptoKit
import EventKit
import Foundation

enum CalendarCollector {
    static func collect(fullContext: Bool, locale: AppLocale = .systemPreferred) async throws -> [EventPayload] {
        let store = EKEventStore()
        let granted = try await store.requestFullAccessToEvents()
        guard granted else {
            throw MouchenError.configuration(L10n.text("没有日历读取权限", "Calendar access was not granted", locale: locale))
        }
        let calendar = Calendar.current
        let start = calendar.date(byAdding: .day, value: -7, to: Date()) ?? Date()
        let end = calendar.date(byAdding: .day, value: 21, to: Date()) ?? Date()
        let events = Array(store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil)).prefix(300))
        return events.map { event in
            var facts: [String: JSONValue] = [
                "start_at": .string(ISO8601Text.string(event.startDate)),
                "end_at": .string(ISO8601Text.string(event.endDate)),
                "duration_minutes": .number(max(0, event.endDate.timeIntervalSince(event.startDate) / 60)),
                "all_day": .bool(event.isAllDay),
                "calendar": .string(fullContext ? event.calendar.title : "[authorized-calendar]"),
            ]
            facts["title"] = .string(fullContext ? (event.title ?? "") : "[local-only]")
            if fullContext, let location = event.location, !location.isEmpty { facts["location"] = .string(location) }
            return EventPayload(
                source: "ios.calendar",
                type: "calendar.scheduled",
                occurredAt: ISO8601Text.string(event.startDate),
                facts: facts,
                entities: [],
                confidence: 0.98,
                sensitivity: "sensitive",
                consentScope: "ios.calendar.explicit",
                evidenceRef: "ios-calendar-\(digest(event.eventIdentifier ?? UUID().uuidString))"
            )
        }
    }
}

enum ContactsCollector {
    static func collect(fullContext: Bool, locale: AppLocale = .systemPreferred) async throws -> EventPayload {
        let store = CNContactStore()
        let granted = try await store.requestAccess(for: .contacts)
        guard granted else {
            throw MouchenError.configuration(L10n.text("没有联系人读取权限", "Contacts access was not granted", locale: locale))
        }
        let keys = [CNContactIdentifierKey, CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey] as [CNKeyDescriptor]
        let request = CNContactFetchRequest(keysToFetch: keys)
        var count = 0
        var sample: [JSONValue] = []
        try store.enumerateContacts(with: request) { contact, stop in
            count += 1
            if sample.count < 200 {
                var item: [String: JSONValue] = ["id_hash": .string(digest(contact.identifier))]
                if fullContext {
                    item["name"] = .string(CNContactFormatter.string(from: contact, style: .fullName) ?? "")
                    item["organization"] = .string(contact.organizationName)
                }
                sample.append(.object(item))
            }
            if count >= 5_000 { stop.pointee = true }
        }
        return EventPayload(
            source: "ios.contacts",
            type: "contacts.snapshot",
            occurredAt: ISO8601Text.now(),
            facts: ["count": .number(Double(count)), "sample": .array(sample)],
            entities: [],
            confidence: 0.95,
            sensitivity: "restricted",
            consentScope: "ios.contacts.explicit",
            evidenceRef: "ios-contacts-\(Int(Date().timeIntervalSince1970 / 86_400))"
        )
    }
}

@MainActor
final class OneShotLocationCollector: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocation, Error>?
    private var requestLocale: AppLocale = .systemPreferred

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func collect(locale: AppLocale = .systemPreferred) async throws -> EventPayload {
        requestLocale = locale
        let location = try await withCheckedThrowingContinuation { continuation in
            guard self.continuation == nil else {
                continuation.resume(throwing: MouchenError.transport(
                    L10n.text("位置采集正在进行", "A location request is already in progress", locale: locale)
                ))
                return
            }
            self.continuation = continuation
            switch manager.authorizationStatus {
            case .authorizedAlways, .authorizedWhenInUse: manager.requestLocation()
            case .notDetermined: manager.requestWhenInUseAuthorization()
            default: finish(.failure(MouchenError.configuration(
                L10n.text("没有位置权限", "Location access was not granted", locale: locale)
            )))
            }
        }
        return EventPayload(
            source: "ios.location",
            type: "location.observed",
            occurredAt: ISO8601Text.string(location.timestamp),
            facts: [
                "latitude": .number(location.coordinate.latitude),
                "longitude": .number(location.coordinate.longitude),
                "accuracy_meters": .number(max(0, location.horizontalAccuracy)),
            ],
            entities: [],
            confidence: location.horizontalAccuracy > 0 ? 0.85 : 0.60,
            sensitivity: "restricted",
            consentScope: "ios.location.one-shot",
            evidenceRef: "ios-location-\(UUID().uuidString)"
        )
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse: manager.requestLocation()
        case .denied, .restricted:
            finish(.failure(MouchenError.configuration(
                L10n.text("位置权限被拒绝", "Location access was denied", locale: requestLocale)
            )))
        default: break
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        finish(.success(location))
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        finish(.failure(error))
    }

    private func finish(_ result: Result<CLLocation, Error>) {
        let current = continuation
        continuation = nil
        current?.resume(with: result)
    }
}

private func digest(_ value: String) -> String {
    SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
}
