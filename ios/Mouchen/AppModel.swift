import Combine
import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    @Published var configuration: ServerConfiguration?
    @Published private var healthPhase: HealthPhase = .disconnected
    @Published private(set) var locale: AppLocale = .systemPreferred
    @Published private(set) var isChangingLocale = false
    @Published var goals: [Goal] = []
    @Published var advice: [Advice] = []
    @Published var pendingEvents = 0
    @Published var isSyncing = false
    @Published var lastError: String?
    @Published var lastSyncAt: Date?
    @Published var answer = ""

    private enum HealthPhase {
        case disconnected
        case connected(String)
        case online(String)
        case syncFailed
    }

    var healthText: String {
        switch healthPhase {
        case .disconnected:
            return t("尚未连接", "Not connected")
        case .connected(let version):
            return t("已连接 · \(version)", "Connected · \(version)")
        case .online(let version):
            return t("云端在线 · \(version)", "Cloud online · \(version)")
        case .syncFailed:
            return t("同步失败", "Sync failed")
        }
    }

    func t(_ zh: String, _ en: String) -> String {
        L10n.text(zh, en, locale: locale)
    }

    static var suggestedServerURL: String {
        Bundle.main.object(forInfoDictionaryKey: "MouchenDefaultServerURL") as? String
            ?? "https://example.invalid"
    }

    private let keychain = KeychainStore()
    private let queue = EventQueue()
    private let notifications = AdviceNotificationService()
    private let attentionState = AdviceAttentionStateStore()
    private let locationCollector = OneShotLocationCollector()
    private var sessionEpoch: UInt64 = 0
    private var syncTask: Task<Void, Never>?
    private var authenticationTask: Task<AuthResponse, Error>?
    private var localePreferenceFence = LocalePreferenceFence()
    private var pendingLocaleSelection = LatestLocaleSelection()

    private struct SessionSnapshot: Equatable {
        let epoch: UInt64
        let baseURL: String
        let userID: String

        var attentionNamespace: String {
            AdviceAttentionStateStore.namespace(baseURL: baseURL, userID: userID)
        }
    }

    init() {
        if let stored = keychain.load(), let checked = try? stored.validated() {
            configuration = checked
            locale = checked.appLocale
        } else {
            // Do not let an older insecure HTTP configuration silently resume.
            keychain.delete()
            configuration = nil
        }
        SharedLocaleStore.save(locale.rawValue)
        Task {
            await notifications.removeLegacyListNotificationsOnce()
            await notifications.requestPermission()
            await refreshPendingCount()
            if configuration != nil { await sync() }
        }
    }

    func saveConfiguration(_ value: ServerConfiguration) async -> Bool {
        invalidateSessionWork()
        let operationEpoch = sessionEpoch
        var queueRollback: (from: String, to: String)?
        do {
            var checked = try value.validated()
            let client = APIClient(configuration: checked)
            let session = try await client.sessionInfo()
            try Task.checkCancellation()
            guard operationEpoch == sessionEpoch else { return false }
            guard session.userID == checked.userID else {
                throw MouchenError.configuration(t("登录身份与本地账号不一致", "The server session belongs to a different account"))
            }
            checked.locale = session.locale.map { AppLocale.normalized($0).rawValue } ?? checked.appLocale.rawValue
            let health = try await client.health()
            try Task.checkCancellation()
            guard operationEpoch == sessionEpoch else { return false }
            if let existing = configuration,
               existing.baseURL != checked.baseURL || existing.userID != checked.userID {
                // Queued observations belong to the account that collected
                // them. Never upload them after switching tenant or server.
                // A failed deletion must block the account switch. Otherwise
                // observations collected for the old tenant could later be
                // uploaded with the new tenant's credential.
                let existingNamespace = AdviceAttentionStateStore.namespace(
                    baseURL: existing.baseURL,
                    userID: existing.userID
                )
                try SharedCaptureStore.removeAll()
                await discardLocalAttentionState(configuration: existing)
                guard operationEpoch == sessionEpoch else { return false }
                let checkedNamespace = AdviceAttentionStateStore.namespace(
                    baseURL: checked.baseURL,
                    userID: checked.userID
                )
                try await queue.switchNamespace(from: existingNamespace, to: checkedNamespace)
                queueRollback = (from: checkedNamespace, to: existingNamespace)
                guard operationEpoch == sessionEpoch else {
                    try? await queue.switchNamespace(from: checkedNamespace, to: existingNamespace)
                    return false
                }
                goals = []
                advice = []
            }
            let checkedNamespace = AdviceAttentionStateStore.namespace(
                baseURL: checked.baseURL,
                userID: checked.userID
            )
            try await queue.activateNamespace(checkedNamespace)
            guard operationEpoch == sessionEpoch else { return false }
            try keychain.save(checked)
            configuration = checked
            locale = checked.appLocale
            SharedLocaleStore.save(locale.rawValue)
            queueRollback = nil
            healthPhase = .connected(health.version)
            lastError = nil
            await sync()
            return true
        } catch {
            if operationEpoch == sessionEpoch, let queueRollback {
                try? await queue.switchNamespace(from: queueRollback.from, to: queueRollback.to)
            }
            if operationEpoch == sessionEpoch { lastError = L10n.error(error, locale: locale) }
            return false
        }
    }

    func authenticate(
        serverURL: String,
        username: String,
        password: String,
        registrationCode: String?,
        createAccount: Bool,
        proactiveCloudEnabled: Bool,
        uploadFullContext: Bool
    ) async -> Bool {
        invalidateSessionWork()
        let operationEpoch = sessionEpoch
        do {
            let deviceID = try keychain.loadOrCreateDeviceID()
            let client = try AuthClient(baseURL: serverURL, locale: locale)
            let requestTask = Task<AuthResponse, Error> {
                if createAccount {
                    return try await client.register(
                        username: username,
                        password: password,
                        deviceID: deviceID,
                        deviceName: UIDevice.current.name,
                        registrationCode: registrationCode?.trimmingCharacters(in: .whitespacesAndNewlines)
                    )
                }
                return try await client.login(
                    username: username,
                    password: password,
                    deviceID: deviceID,
                    deviceName: UIDevice.current.name
                )
            }
            authenticationTask = requestTask
            let response = try await requestTask.value
            try Task.checkCancellation()
            guard operationEpoch == sessionEpoch else { return false }
            authenticationTask = nil
            guard response.tokenType.caseInsensitiveCompare("bearer") == .orderedSame,
                  response.session.deviceID == deviceID else {
                throw MouchenError.transport(t("服务器返回了无效的设备会话", "The server returned an invalid device session"))
            }
            return await saveConfiguration(ServerConfiguration(
                baseURL: serverURL,
                userID: response.session.userID,
                username: response.session.username,
                bearerToken: response.accessToken,
                proactiveCloudEnabled: proactiveCloudEnabled,
                uploadFullContext: uploadFullContext,
                locale: AppLocale.normalized(response.session.locale ?? locale.rawValue).rawValue
            ))
        } catch {
            if operationEpoch == sessionEpoch { lastError = L10n.error(error, locale: locale) }
            return false
        }
    }

    @discardableResult
    func disconnect() async -> Bool {
        let previousConfiguration = configuration
        invalidateSessionWork()
        let operationEpoch = sessionEpoch
        do {
            try SharedCaptureStore.removeAll()
            guard operationEpoch == sessionEpoch else { return false }
            if let previousConfiguration {
                let namespace = AdviceAttentionStateStore.namespace(
                    baseURL: previousConfiguration.baseURL,
                    userID: previousConfiguration.userID
                )
                try await queue.sealSignedOut(namespace: namespace, epoch: operationEpoch)
            }
            guard operationEpoch == sessionEpoch else { return false }
        } catch {
            lastError = t(
                "无法安全清除当前账号的本地事件，已阻止换号。",
                "Local account data could not be cleared safely, so account switching was blocked."
            )
            return false
        }
        if let previousConfiguration {
            await discardLocalAttentionState(configuration: previousConfiguration)
        }
        guard operationEpoch == sessionEpoch else { return false }
        keychain.delete()
        configuration = nil
        healthPhase = .disconnected
        locale = .systemPreferred
        SharedLocaleStore.save(locale.rawValue)
        goals = []
        advice = []
        await refreshPendingCount()
        return true
    }

    func logout() async {
        // Clear tenant-bound local material before revoking the credential. A
        // cleanup failure keeps the current account selected and prevents a
        // later account from inheriting its queue.
        let previousConfiguration = configuration
        guard await disconnect() else { return }
        if let previousConfiguration {
            try? await APIClient(configuration: previousConfiguration).logout()
        }
    }

    /// Before authentication the owner may follow or override the device
    /// language locally. Once authenticated, the server account preference is
    /// authoritative and is persisted with the tenant-bound Keychain record.
    func selectUnauthenticatedLocale(_ value: AppLocale) {
        guard configuration == nil else { return }
        locale = value
        SharedLocaleStore.save(value.rawValue)
        lastError = nil
    }

    func updateLocale(_ value: AppLocale) async {
        guard let current = configuration else {
            selectUnauthenticatedLocale(value)
            return
        }
        guard value != locale else { return }

        // A second picker change joins the existing single-writer loop. The
        // loop always keeps the newest selection and never allows two locale
        // PUT requests to race at the server.
        if isChangingLocale {
            pendingLocaleSelection.replace(with: value)
            locale = value
            SharedLocaleStore.save(value.rawValue)
            lastError = nil
            return
        }

        // Retire any sync that may have fetched the previous preference. The
        // locale fence independently retires its GET token, while the session
        // snapshot protects every other account-bound write.
        invalidateSessionWork()
        let snapshot = makeSnapshot(current)
        let mutationToken = localePreferenceFence.beginMutation()
        var confirmedLocale = current.appLocale
        pendingLocaleSelection.replace(with: value)
        locale = value
        SharedLocaleStore.save(value.rawValue)
        isChangingLocale = true
        let client = APIClient(configuration: current)

        while isCurrent(snapshot), localePreferenceFence.permits(mutationToken),
              let requestedLocale = pendingLocaleSelection.take() {
            do {
                let preferences = try await client.updateAccountLocale(requestedLocale)
                guard isCurrent(snapshot), localePreferenceFence.permits(mutationToken) else { return }
                confirmedLocale = try applyRemoteLocale(
                    preferences.locale,
                    snapshot: snapshot,
                    updateVisibleLocale: pendingLocaleSelection.isEmpty
                )
                lastError = nil
            } catch {
                guard isCurrent(snapshot), localePreferenceFence.permits(mutationToken) else { return }
                if pendingLocaleSelection.isEmpty {
                    locale = confirmedLocale
                    SharedLocaleStore.save(confirmedLocale.rawValue)
                    lastError = L10n.error(error, locale: confirmedLocale)
                }
            }
        }

        guard isCurrent(snapshot), localePreferenceFence.permits(mutationToken) else { return }
        pendingLocaleSelection.removeAll()
        localePreferenceFence.finishMutation(mutationToken)
        isChangingLocale = false

        // A failed response is ambiguous: the server may have committed the
        // PUT before the connection failed. A post-mutation GET reconciles the
        // server, Keychain, shared extension store, and visible picker.
        await sync()
    }

    func sync() async {
        guard let configuration, !isSyncing,
              let localeReadToken = localePreferenceFence.beginRead() else { return }
        let snapshot = makeSnapshot(configuration)
        let task = Task { [weak self] in
            guard let self else { return }
            await self.performSync(
                configuration: configuration,
                snapshot: snapshot,
                localeReadToken: localeReadToken
            )
        }
        syncTask = task
        await task.value
    }

    private func performSync(
        configuration: ServerConfiguration,
        snapshot: SessionSnapshot,
        localeReadToken: LocalePreferenceFence.ReadToken
    ) async {
        guard isCurrent(snapshot), !isSyncing else { return }
        isSyncing = true
        defer { if isCurrent(snapshot) { isSyncing = false } }
        do {
            try Task.checkCancellation()
            try await importSharedCaptures(snapshot: snapshot)
            guard isCurrent(snapshot) else { return }
            let client = APIClient(configuration: configuration)
            let queued = try await queue.first(limit: 100, namespace: snapshot.attentionNamespace)
            var uploaded = 0
            for item in queued {
                try Task.checkCancellation()
                try await client.send(event: item.event)
                guard isCurrent(snapshot) else { return }
                try await queue.remove(item.id, namespace: snapshot.attentionNamespace)
                guard isCurrent(snapshot) else { return }
                uploaded += 1
            }
            if uploaded > 0 { try? await client.runReview() }
            try Task.checkCancellation()
            guard isCurrent(snapshot) else { return }
            async let remoteGoals = client.goals()
            async let remoteAdvice = client.advice()
            async let remotePreferences = client.accountPreferences()
            let pulledGoals = try await remoteGoals
            let pulledAdvice = try await remoteAdvice
            let pulledPreferences = try await remotePreferences
            guard isCurrent(snapshot) else { return }
            guard localePreferenceFence.permits(localeReadToken) else { return }
            try applyRemoteLocale(pulledPreferences.locale, snapshot: snapshot)
            goals = pulledGoals
            advice = pulledAdvice
            for item in pulledAdvice where item.status != "active" {
                await notifications.cancel(adviceID: item.id)
                guard isCurrent(snapshot) else { return }
                await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: item.id)
            }
            try await processAdviceAttention(client: client, snapshot: snapshot)
            guard isCurrent(snapshot) else { return }
            let health = try await client.health()
            guard isCurrent(snapshot) else { return }
            healthPhase = .online(health.version)
            lastSyncAt = Date()
            lastError = nil
        } catch is CancellationError {
            return
        } catch {
            guard isCurrent(snapshot) else { return }
            healthPhase = .syncFailed
            lastError = L10n.error(error, locale: locale)
        }
        if isCurrent(snapshot) { await refreshPendingCount() }
    }

    func addGoal(domain: String, title: String, quote: String, weeklyHours: Double?, redline: Bool) async -> Bool {
        guard let configuration else { return false }
        let snapshot = makeSnapshot(configuration)
        do {
            var target: [String: JSONValue] = [:]
            if let weeklyHours { target["weekly_hours"] = .number(weeklyHours) }
            let created = try await APIClient(configuration: configuration).createGoal(
                GoalCreate(domain: domain, title: title, quote: quote, target: target, isRedline: redline)
            )
            guard isCurrent(snapshot) else { return false }
            goals.insert(created, at: 0)
            lastError = nil
            return true
        } catch {
            if isCurrent(snapshot) { lastError = L10n.error(error, locale: locale) }
            return false
        }
    }

    func recordProblem(_ text: String, domain: String) async {
        guard let configuration,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let snapshot = makeSnapshot(configuration)
        let event = EventPayload(
            source: "ios.self_report",
            type: "self_report.problem",
            occurredAt: ISO8601Text.now(),
            facts: ["text": .string(String(text.prefix(8_000))), "domain": .string(domain)],
            entities: [],
            confidence: 1,
            sensitivity: "sensitive",
            consentScope: "ios.owner.explicit",
            evidenceRef: "ios-problem-\(UUID().uuidString)"
        )
        await enqueue([event], snapshot: snapshot)
    }

    func collectCalendar() async {
        guard let configuration else { return }
        let selectedLocale = locale
        await performCollection {
            try await CalendarCollector.collect(fullContext: configuration.uploadFullContext, locale: selectedLocale)
        }
    }

    func collectContacts() async {
        guard let configuration else { return }
        let selectedLocale = locale
        await performCollection {
            [try await ContactsCollector.collect(fullContext: configuration.uploadFullContext, locale: selectedLocale)]
        }
    }

    func collectLocation() async {
        let selectedLocale = locale
        await performCollection { [try await self.locationCollector.collect(locale: selectedLocale)] }
    }

    func sendFeedback(_ item: Advice, kind: String) async {
        guard let configuration else { return }
        let snapshot = makeSnapshot(configuration)
        do {
            try await APIClient(configuration: configuration).feedback(adviceID: item.id, kind: kind)
            guard isCurrent(snapshot) else { return }
            await notifications.cancel(adviceID: item.id)
            guard isCurrent(snapshot) else { return }
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: item.id)
            await sync()
        } catch { if isCurrent(snapshot) { lastError = L10n.error(error, locale: locale) } }
    }

    func verify(_ item: Advice, correct: Bool) async {
        guard let configuration else { return }
        let snapshot = makeSnapshot(configuration)
        do {
            try await APIClient(configuration: configuration).outcome(
                adviceID: item.id,
                status: correct ? "correct" : "incorrect",
                actualResult: correct
                    ? t("iOS 用户确认预测结果已发生", "The iOS user confirmed that the predicted outcome occurred")
                    : t("iOS 用户确认预测结果未发生", "The iOS user confirmed that the predicted outcome did not occur")
            )
            guard isCurrent(snapshot) else { return }
            await sync()
        } catch { if isCurrent(snapshot) { lastError = L10n.error(error, locale: locale) } }
    }

    func ask(_ prompt: String) async {
        guard let configuration, !prompt.isEmpty else { return }
        let snapshot = makeSnapshot(configuration)
        do {
            let reply = try await APIClient(configuration: configuration).ask(prompt).content
            guard isCurrent(snapshot) else { return }
            answer = reply
            lastError = nil
        } catch { if isCurrent(snapshot) { lastError = L10n.error(error, locale: locale) } }
    }

    func purgeLocalQueue() async {
        guard let configuration else { return }
        let snapshot = makeSnapshot(configuration)
        try? await queue.removeAll(namespace: snapshot.attentionNamespace)
        if isCurrent(snapshot) { await refreshPendingCount() }
    }

    private func performCollection(_ operation: () async throws -> [EventPayload]) async {
        guard let configuration else { return }
        let snapshot = makeSnapshot(configuration)
        do {
            let events = try await operation()
            guard isCurrent(snapshot) else { return }
            await enqueue(events, snapshot: snapshot)
            guard isCurrent(snapshot) else { return }
            await sync()
        } catch { if isCurrent(snapshot) { lastError = L10n.error(error, locale: locale) } }
    }

    private func enqueue(_ events: [EventPayload], snapshot: SessionSnapshot) async {
        guard isCurrent(snapshot) else { return }
        for event in events {
            guard isCurrent(snapshot) else { return }
            try? await queue.enqueue(event, namespace: snapshot.attentionNamespace)
        }
        if isCurrent(snapshot) { await refreshPendingCount() }
    }

    private func importSharedCaptures(snapshot: SessionSnapshot) async throws {
        for capture in SharedCaptureStore.takeAll() {
            guard isCurrent(snapshot) else { return }
            try await queue.enqueue(
                EventPayload(
                    source: "ios.share_extension",
                    type: "shared.text",
                    occurredAt: ISO8601Text.string(capture.capturedAt),
                    facts: [
                        "text": .string(capture.text),
                        "source_application": .string(capture.sourceApplication ?? "unknown"),
                    ],
                    entities: [],
                    confidence: 1,
                    sensitivity: "sensitive",
                    consentScope: "ios.share.explicit",
                    evidenceRef: "ios-share-\(capture.id.uuidString)"
                ),
                namespace: snapshot.attentionNamespace
            )
            guard isCurrent(snapshot) else { return }
        }
    }

    private func refreshPendingCount() async {
        guard let configuration else {
            pendingEvents = 0
            return
        }
        let snapshot = makeSnapshot(configuration)
        let count = (try? await queue.count(namespace: snapshot.attentionNamespace)) ?? 0
        guard isCurrent(snapshot) else { return }
        pendingEvents = count
    }

    private func discardLocalAttentionState(configuration: ServerConfiguration) async {
        let namespace = AdviceAttentionStateStore.namespace(
            baseURL: configuration.baseURL,
            userID: configuration.userID
        )
        if let pending = await attentionState.load(namespace: namespace) {
            await notifications.cancel(adviceID: pending.advice.id)
        }
        await attentionState.clear(namespace: namespace)
    }

    /// The advice list is read-only synchronization. A system notification is
    /// allowed only after the shared server grants this installation a claim,
    /// which prevents Windows, Android and iOS from counting the same reminder.
    private func processAdviceAttention(client: APIClient, snapshot: SessionSnapshot) async throws {
        guard isCurrent(snapshot) else { return }
        let deviceID = try keychain.loadOrCreateDeviceID()

        if let pending = await attentionState.load(namespace: snapshot.attentionNamespace) {
            guard isCurrent(snapshot) else { return }
            try await settleAttentionClaim(pending, deviceID: deviceID, client: client, snapshot: snapshot)
            // Handle at most one server claim in a sync pass. In particular,
            // a permission failure must not release and immediately reclaim
            // the same advice in a tight loop.
            return
        }

        // The server consumes one of the two global delivery opportunities at
        // claim time. Never claim when iOS cannot enqueue a notification.
        guard await notifications.isDeliveryAuthorized() else { return }
        guard isCurrent(snapshot) else { return }

        let response = try await client.claimAdviceAttention(
            deviceID: deviceID,
            appVersion: Self.appVersion
        )
        guard isCurrent(snapshot) else { return }
        guard let claim = response.claimed else { return }
        try await attentionState.save(claim, namespace: snapshot.attentionNamespace)
        guard isCurrent(snapshot) else {
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: claim.advice.id)
            return
        }
        try await settleAttentionClaim(claim, deviceID: deviceID, client: client, snapshot: snapshot)
    }

    private func settleAttentionClaim(
        _ claim: PendingAdviceClaim,
        deviceID: String,
        client: APIClient,
        snapshot: SessionSnapshot
    ) async throws {
        guard isCurrent(snapshot) else { return }
        if !claim.notificationScheduled {
            guard !Self.isExpired(claim.leaseExpiresAt) else {
                await releaseFailedClaim(claim, deviceID: deviceID, client: client, reason: "claim_lease_expired", snapshot: snapshot)
                return
            }
            do {
                try await notifications.deliver(claim, locale: locale)
            } catch {
                await releaseFailedClaim(
                    claim,
                    deviceID: deviceID,
                    client: client,
                    reason: "ios_notification_failed:\(error.localizedDescription)",
                    snapshot: snapshot
                )
                return
            }
            guard isCurrent(snapshot) else {
                await notifications.cancel(adviceID: claim.advice.id)
                return
            }
            // Do not report a successful system enqueue as a failed delivery
            // merely because the local recovery marker could not be encoded.
            try await attentionState.markNotificationScheduled(namespace: snapshot.attentionNamespace)
        }

        do {
            guard isCurrent(snapshot) else { return }
            try await client.completeAdviceAttention(
                adviceID: claim.advice.id,
                deviceID: deviceID,
                claimToken: claim.claimToken
            )
            guard isCurrent(snapshot) else { return }
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: claim.advice.id)
        } catch MouchenError.server(let status, _) where status == 409 || status == 404 {
            guard isCurrent(snapshot) else { return }
            // A prior completion may have succeeded before its response was
            // lost, or another terminal feedback may have resolved the item.
            // Do not enqueue the same local notification again.
            await notifications.cancel(adviceID: claim.advice.id)
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: claim.advice.id)
        }
    }

    private func releaseFailedClaim(
        _ claim: PendingAdviceClaim,
        deviceID: String,
        client: APIClient,
        reason: String,
        snapshot: SessionSnapshot
    ) async {
        guard isCurrent(snapshot) else { return }
        do {
            try await client.failAdviceAttention(
                adviceID: claim.advice.id,
                deviceID: deviceID,
                claimToken: claim.claimToken,
                reason: reason
            )
            guard isCurrent(snapshot) else { return }
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: claim.advice.id)
        } catch MouchenError.server(let status, _) where status == 409 || status == 404 {
            guard isCurrent(snapshot) else { return }
            await notifications.cancel(adviceID: claim.advice.id)
            await attentionState.clear(namespace: snapshot.attentionNamespace, adviceID: claim.advice.id)
        } catch {
            // Keep the claim so the fail acknowledgement can be retried after
            // connectivity returns. Failed local delivery is never completed.
        }
    }

    private func invalidateSessionWork() {
        sessionEpoch &+= 1
        localePreferenceFence.invalidate()
        pendingLocaleSelection.removeAll()
        syncTask?.cancel()
        authenticationTask?.cancel()
        syncTask = nil
        authenticationTask = nil
        isSyncing = false
        isChangingLocale = false
    }

    @discardableResult
    private func applyRemoteLocale(
        _ rawValue: String,
        snapshot: SessionSnapshot,
        updateVisibleLocale: Bool = true
    ) throws -> AppLocale {
        guard isCurrent(snapshot), var updated = configuration else { throw CancellationError() }
        let serverLocale = AppLocale.normalized(rawValue)
        updated.locale = serverLocale.rawValue
        try keychain.save(updated)
        guard isCurrent(snapshot) else { throw CancellationError() }
        configuration = updated
        if updateVisibleLocale {
            locale = serverLocale
            SharedLocaleStore.save(serverLocale.rawValue)
        }
        lastError = nil
        return serverLocale
    }

    private func makeSnapshot(_ configuration: ServerConfiguration) -> SessionSnapshot {
        SessionSnapshot(
            epoch: sessionEpoch,
            baseURL: configuration.baseURL,
            userID: configuration.userID
        )
    }

    private func isCurrent(_ snapshot: SessionSnapshot) -> Bool {
        guard snapshot.epoch == sessionEpoch, let configuration else { return false }
        return configuration.baseURL == snapshot.baseURL && configuration.userID == snapshot.userID
    }

    private static var appVersion: String? {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
        switch (version, build) {
        case let (version?, build?): return "\(version) (\(build))"
        case let (version?, nil): return version
        case let (nil, build?): return build
        default: return nil
        }
    }

    private static func isExpired(_ value: String) -> Bool {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date <= Date() }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value).map { $0 <= Date() } ?? true
    }
}
