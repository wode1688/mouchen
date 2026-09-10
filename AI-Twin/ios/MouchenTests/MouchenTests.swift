import Foundation
import XCTest
@testable import Mouchen

final class MouchenTests: XCTestCase {
    func testRemoteHTTPIsRejected() {
        let value = ServerConfiguration(
            baseURL: "http://203.0.113.10",
            userID: "owner",
            bearerToken: "secret",
            proactiveCloudEnabled: true,
            uploadFullContext: false
        )
        XCTAssertThrowsError(try value.validated())
    }

    func testRemoteHTTPSRequiresToken() {
        let value = ServerConfiguration(
            baseURL: "https://example.com",
            userID: "owner",
            bearerToken: "",
            proactiveCloudEnabled: true,
            uploadFullContext: false
        )
        XCTAssertThrowsError(try value.validated())
    }

    func testServerURLRejectsEmbeddedCredentialsAndQuery() {
        let values = [
            "https://user:password@example.com",
            "https://example.com?token=secret",
            "https://example.com#fragment",
        ]
        for baseURL in values {
            let value = ServerConfiguration(
                baseURL: baseURL,
                userID: "owner",
                bearerToken: "secret",
                proactiveCloudEnabled: false,
                uploadFullContext: false
            )
            XCTAssertThrowsError(try value.validated())
        }
    }

    func testAuthResponseUsesServerDerivedTenantAndDeviceIdentity() throws {
        let json = #"""
        {
          "access_token": "mch_at_example.secret",
          "token_type": "bearer",
          "expires_at": "2026-09-01T00:00:00Z",
          "session": {
            "user_id": "2e926fd0-3691-4d96-9d84-8569d6493811",
            "username": "owner",
            "device_id": "ios-installation",
            "locale": "en-US",
            "scopes": ["mouchen:read", "mouchen:write"],
            "expires_at": "2026-09-01T00:00:00Z"
          }
        }
        """#

        let response = try JSONDecoder().decode(AuthResponse.self, from: Data(json.utf8))

        XCTAssertEqual(response.tokenType, "bearer")
        XCTAssertEqual(response.session.username, "owner")
        XCTAssertEqual(response.session.deviceID, "ios-installation")
        XCTAssertEqual(response.session.userID, "2e926fd0-3691-4d96-9d84-8569d6493811")
        XCTAssertEqual(response.session.locale, "en-US")
    }

    func testLocaleNormalizationAndCopy() {
        XCTAssertEqual(AppLocale.normalized("en_US"), .enUS)
        XCTAssertEqual(AppLocale.normalized("zh-Hans-CN"), .zhCN)
        XCTAssertEqual(L10n.text("中文", "English", locale: .enUS), "English")
        XCTAssertEqual(L10n.text("中文", "English", locale: .zhCN), "中文")
    }

    func testLocalePreferenceFenceRejectsGetThatStartedBeforePut() throws {
        var fence = LocalePreferenceFence()
        let staleRead = try XCTUnwrap(fence.beginRead())

        let mutation = fence.beginMutation()

        XCTAssertFalse(fence.permits(staleRead))
        XCTAssertTrue(fence.permits(mutation))
        XCTAssertNil(fence.beginRead())
    }

    func testLocalePreferenceFenceBlocksGetUntilPutFinishes() throws {
        var fence = LocalePreferenceFence()
        let mutation = fence.beginMutation()

        XCTAssertNil(fence.beginRead())
        fence.finishMutation(mutation)

        let committedRead = try XCTUnwrap(fence.beginRead())
        XCTAssertTrue(fence.permits(committedRead))
        XCTAssertFalse(fence.permits(mutation))
    }

    func testRapidLocaleSelectionsKeepOnlyTheNewestValue() {
        var selections = LatestLocaleSelection()

        selections.replace(with: .enUS)
        selections.replace(with: .zhCN)

        XCTAssertEqual(selections.take(), .zhCN)
        XCTAssertNil(selections.take())
    }

    func testAdviceLevelAcceptsBackendNumberAndLabel() throws {
        XCTAssertEqual(try JSONDecoder().decode(AdviceLevel.self, from: Data("3".utf8)), .l3)
        XCTAssertEqual(try JSONDecoder().decode(AdviceLevel.self, from: Data("\"L4\"".utf8)), .l4)
    }

    func testEventPayloadUsesBackendFieldNames() throws {
        let event = EventPayload(
            source: "ios.self_report",
            type: "self_report.problem",
            occurredAt: "2026-08-04T00:00:00.000Z",
            facts: ["text": .string("test")],
            entities: [],
            confidence: 1,
            sensitivity: "sensitive",
            consentScope: "ios.owner.explicit",
            evidenceRef: "ios-test"
        )
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(event)) as? [String: Any])
        XCTAssertEqual(object["occurred_at"] as? String, event.occurredAt)
        XCTAssertEqual(object["consent_scope"] as? String, event.consentScope)
        XCTAssertEqual(object["evidence_ref"] as? String, event.evidenceRef)
    }

    func testAttentionClaimDecodesBackendEnvelope() throws {
        let adviceID = UUID()
        let token = UUID()
        let json = """
        {
          "status": "claimed",
          "claim_token": "\(token.uuidString)",
          "lease_expires_at": "2026-08-09T12:00:00+00:00",
          "delivery_number": 2,
          "advice": {
            "id": "\(adviceID.uuidString)",
            "domain": "work",
            "requested_level": 2,
            "effective_level": "L2",
            "goal_quote": "Finish the release",
            "action": "Resolve the blocker",
            "first_step": "Open the failing check",
            "alternative": null,
            "prediction": {
              "outcome": "Release stays blocked",
              "deadline": "2026-08-10T00:00:00+00:00",
              "confidence": 0.8
            },
            "delivery": "immediate",
            "status": "active",
            "created_at": "2026-08-09T00:00:00+00:00"
          }
        }
        """
        let response = try JSONDecoder().decode(AdviceAttentionClaimResponse.self, from: Data(json.utf8))
        let claim = try XCTUnwrap(response.claimed)
        XCTAssertEqual(claim.advice.id, adviceID)
        XCTAssertEqual(claim.claimToken, token)
        XCTAssertEqual(claim.deliveryNumber, 2)
        XCTAssertFalse(claim.notificationScheduled)
    }

    func testAttentionNoneResponseHasNoClaim() throws {
        let response = try JSONDecoder().decode(
            AdviceAttentionClaimResponse.self,
            from: Data(#"{"status":"none"}"#.utf8)
        )
        XCTAssertNil(response.claimed)
    }

    func testAttentionNotificationIdentifierIncludesDeliveryOrdinal() {
        let adviceID = UUID()
        XCTAssertNotEqual(
            AdviceNotificationService.identifier(adviceID: adviceID, deliveryNumber: 1),
            AdviceNotificationService.identifier(adviceID: adviceID, deliveryNumber: 2)
        )
    }

    func testPendingAttentionStatePersistsScheduledMarker() async throws {
        let suite = "com.mouchen.ios.tests.attention.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("mouchen-attention-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = AdviceAttentionStateStore(root: root, legacyDefaults: defaults)
        let namespace = AdviceAttentionStateStore.namespace(
            baseURL: "https://example.com",
            userID: "owner"
        )
        let advice = Advice(
            id: UUID(),
            domain: "work",
            requestedLevel: .l2,
            effectiveLevel: .l2,
            goalQuote: "Finish the release",
            action: "Resolve the blocker",
            firstStep: "Open the failing check",
            alternative: nil,
            prediction: Prediction(outcome: "Blocked", deadline: "2026-08-10T00:00:00Z", confidence: 0.8),
            delivery: "immediate",
            status: "active",
            createdAt: "2026-08-09T00:00:00Z"
        )
        let pending = PendingAdviceClaim(
            advice: advice,
            claimToken: UUID(),
            leaseExpiresAt: "2026-08-09T12:00:00Z",
            deliveryNumber: 1,
            notificationScheduled: false
        )
        try await store.save(pending, namespace: namespace)
        try await store.markNotificationScheduled(namespace: namespace)
        let stored = await store.load(namespace: namespace)
        XCTAssertEqual(stored?.notificationScheduled, true)
        XCTAssertNil(defaults.data(forKey: "pending-advice-attention-claim-v1"))
    }

    func testAttentionStateIsTenantNamespaced() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("mouchen-attention-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = AdviceAttentionStateStore(root: root)
        let advice = Advice(
            id: UUID(), domain: "work", requestedLevel: .l2, effectiveLevel: .l2,
            goalQuote: "Goal", action: "Action", firstStep: "Step", alternative: nil,
            prediction: Prediction(outcome: "Outcome", deadline: "2026-08-13T00:00:00Z", confidence: 0.7),
            delivery: "immediate", status: "active", createdAt: "2026-08-12T00:00:00Z"
        )
        let claim = PendingAdviceClaim(
            advice: advice, claimToken: UUID(), leaseExpiresAt: "2026-08-13T00:00:00Z",
            deliveryNumber: 1, notificationScheduled: false
        )
        let first = AdviceAttentionStateStore.namespace(baseURL: "https://one.example", userID: "u1")
        let second = AdviceAttentionStateStore.namespace(baseURL: "https://one.example", userID: "u2")
        try await store.save(claim, namespace: first)
        let firstValue = await store.load(namespace: first)
        let secondValue = await store.load(namespace: second)
        XCTAssertNotNil(firstValue)
        XCTAssertNil(secondValue)
    }

    func testSharedCaptureUsesProtectedFileInsteadOfPreferences() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("mouchen-capture-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let capture = SharedCapture(text: "private shared text", sourceApplication: "test")
        SharedCaptureStore.append(capture, root: root)

        let file = SharedCaptureStore.storageURL(root: root)
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
        XCTAssertEqual(SharedCaptureStore.takeAll(root: root).first?.text, "private shared text")
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
    }

    func testOriginComparisonRejectsCrossHostPortAndDowngrade() throws {
        let origin = try XCTUnwrap(URL(string: "https://example.com"))
        XCTAssertTrue(OriginBoundSessionDelegate.sameOrigin(origin, try XCTUnwrap(URL(string: "https://example.com/path"))))
        XCTAssertFalse(OriginBoundSessionDelegate.sameOrigin(origin, try XCTUnwrap(URL(string: "https://evil.example/path"))))
        XCTAssertFalse(OriginBoundSessionDelegate.sameOrigin(origin, try XCTUnwrap(URL(string: "https://example.com:444/path"))))
        XCTAssertTrue(OriginBoundSessionDelegate.isHTTPSDowngrade(origin, try XCTUnwrap(URL(string: "http://example.com/path"))))
    }

    func testQueueNamespaceRejectsStaleAccountAfterSwitch() async throws {
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("mouchen-queue-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: file) }
        let queue = EventQueue(fileURL: file)
        let event = EventPayload(
            source: "ios.test", type: "test", occurredAt: ISO8601Text.now(),
            facts: ["text": .string("private")], entities: [], confidence: 1,
            sensitivity: "sensitive", consentScope: "test", evidenceRef: "test"
        )
        try await queue.enqueue(event, namespace: "tenant-one")
        try await queue.switchNamespace(from: "tenant-one", to: "tenant-two")
        do {
            try await queue.enqueue(event, namespace: "tenant-one")
            XCTFail("stale tenant write should fail")
        } catch {
            XCTAssertEqual(error.localizedDescription, "Local event queue belongs to a different account")
        }
        let remaining = try await queue.count(namespace: "tenant-two")
        XCTAssertEqual(remaining, 0)
    }
}
