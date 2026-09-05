import Foundation
import CryptoKit
import UserNotifications

enum AdviceNotificationError: LocalizedError {
    case permissionDenied

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            "iOS notification permission is not enabled"
        }
    }
}

actor AdviceAttentionStateStore {
    private let root: URL
    private let legacyDefaults: UserDefaults
    private let legacyKey = "pending-advice-attention-claim-v1"

    init(
        root: URL? = nil,
        legacyDefaults: UserDefaults = UserDefaults(suiteName: SharedCaptureStore.suiteName) ?? .standard
    ) {
        let manager = FileManager.default
        self.root = root
            ?? manager.containerURL(forSecurityApplicationGroupIdentifier: SharedCaptureStore.suiteName)
            ?? manager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        self.legacyDefaults = legacyDefaults
        try? manager.createDirectory(
            at: self.root,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        // V1 stored a full advice body in the preferences database. Delete it
        // instead of silently moving an unbound claim into a logged-in tenant.
        legacyDefaults.removeObject(forKey: legacyKey)
    }

    func load(namespace: String) -> PendingAdviceClaim? {
        guard let data = try? Data(contentsOf: fileURL(namespace: namespace)) else { return nil }
        return try? JSONDecoder().decode(PendingAdviceClaim.self, from: data)
    }

    func save(_ claim: PendingAdviceClaim, namespace: String) throws {
        let url = fileURL(namespace: namespace)
        try JSONEncoder().encode(claim).write(to: url, options: [.atomic, .completeFileProtection])
        try FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.complete],
            ofItemAtPath: url.path
        )
    }

    func markNotificationScheduled(namespace: String) throws {
        guard var claim = load(namespace: namespace) else { return }
        claim.notificationScheduled = true
        try save(claim, namespace: namespace)
    }

    func clear(namespace: String, adviceID: UUID? = nil) {
        if let adviceID, load(namespace: namespace)?.advice.id != adviceID { return }
        try? FileManager.default.removeItem(at: fileURL(namespace: namespace))
    }

    func clearAll() {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: nil
        ) else { return }
        for file in files where file.lastPathComponent.hasPrefix("attention-") {
            try? FileManager.default.removeItem(at: file)
        }
    }

    static func namespace(baseURL: String, userID: String) -> String {
        let digest = SHA256.hash(data: Data("\(baseURL)|\(userID)".utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private func fileURL(namespace: String) -> URL {
        root.appendingPathComponent("attention-\(namespace).json", isDirectory: false)
    }
}

actor AdviceNotificationService {
    private let center: UNUserNotificationCenter
    private let defaults: UserDefaults
    private let legacyMigrationKey = "central-attention-migration-v1"

    init(
        center: UNUserNotificationCenter = .current(),
        defaults: UserDefaults = UserDefaults(suiteName: SharedCaptureStore.suiteName) ?? .standard
    ) {
        self.center = center
        self.defaults = defaults
    }

    func requestPermission() async {
        _ = try? await center.requestAuthorization(options: [.alert, .badge, .sound])
    }

    func isDeliveryAuthorized() async -> Bool {
        let settings = await center.notificationSettings()
        let authorized = settings.authorizationStatus == .authorized
            || settings.authorizationStatus == .provisional
            || settings.authorizationStatus == .ephemeral
        guard authorized else { return false }
        return settings.alertSetting == .enabled
            || settings.notificationCenterSetting == .enabled
            || settings.lockScreenSetting == .enabled
    }

    /// Removes only requests created by the retired "notify every item returned
    /// by /v1/advice" path. Central-attention identifiers include an ordinal.
    func removeLegacyListNotificationsOnce() async {
        guard !defaults.bool(forKey: legacyMigrationKey) else { return }
        let pending = await center.pendingNotificationRequests()
        let delivered = await center.deliveredNotifications()
        let legacyPending = pending.map(\.identifier).filter(Self.isLegacyIdentifier)
        let legacyDelivered = delivered.map { $0.request.identifier }.filter(Self.isLegacyIdentifier)
        center.removePendingNotificationRequests(withIdentifiers: legacyPending)
        center.removeDeliveredNotifications(withIdentifiers: legacyDelivered)
        defaults.removeObject(forKey: "notified-advice-ids-v1")
        defaults.set(true, forKey: legacyMigrationKey)
    }

    func deliver(_ claim: PendingAdviceClaim, locale: AppLocale) async throws {
        guard await isDeliveryAuthorized() else {
            throw AdviceNotificationError.permissionDenied
        }

        let item = claim.advice
        let content = UNMutableNotificationContent()
        content.title = "\(L10n.text("AI替身", "My AI Twin", locale: locale)) L\(item.effectiveLevel.rawValue) · \(item.domain)"
        let firstStep = L10n.text("第一步", "First step", locale: locale)
        content.body = "\(item.action)\n\(firstStep): \(item.firstStep)"
        content.userInfo = [
            "advice_id": item.id.uuidString,
            "delivery_number": claim.deliveryNumber,
        ]
        content.sound = .default
        try await center.add(
            UNNotificationRequest(
                identifier: Self.identifier(adviceID: item.id, deliveryNumber: claim.deliveryNumber),
                content: content,
                trigger: nil
            )
        )
    }

    func cancel(adviceID: UUID) {
        let identifiers = [
            "advice-\(adviceID.uuidString)",
            Self.identifier(adviceID: adviceID, deliveryNumber: 1),
            Self.identifier(adviceID: adviceID, deliveryNumber: 2),
        ]
        center.removePendingNotificationRequests(withIdentifiers: identifiers)
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
    }

    static func identifier(adviceID: UUID, deliveryNumber: Int) -> String {
        "advice-\(adviceID.uuidString)-delivery-\(deliveryNumber)"
    }

    private static func isLegacyIdentifier(_ value: String) -> Bool {
        guard value.hasPrefix("advice-") else { return false }
        return UUID(uuidString: String(value.dropFirst("advice-".count))) != nil
    }
}
