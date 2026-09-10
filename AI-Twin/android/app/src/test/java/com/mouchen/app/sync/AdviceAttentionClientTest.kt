package com.mouchen.app.sync

import com.mouchen.app.data.AdviceEntity
import java.time.Instant
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AdviceAttentionClientTest {
    @Test
    fun claimIdentifiesAndroidAndParsesSecondDelivery() = runBlocking {
        val captured = AtomicReference<Request>()
        val client = client(
            responseBody = claimedResponse(deliveryNumber = 2),
            capture = captured::set,
        )

        val result = client.claim()

        assertTrue(result is AdviceAttentionClaimResult.Claimed)
        with(result as AdviceAttentionClaimResult.Claimed) {
            assertEquals(ADVICE_ID, advice.id)
            assertEquals(2, deliveryNumber)
            assertEquals(Instant.parse("2026-08-09T13:00:00Z").toEpochMilli(), leaseExpiresAt)
        }
        with(captured.get()) {
            assertEquals("/v1/advice-attention/claim", url.encodedPath)
            assertEquals("owner-1", header("X-User-Id"))
            assertEquals("Bearer token-1", header("Authorization"))
            val json = JSONObject(Buffer().also { body!!.writeTo(it) }.readUtf8())
            assertEquals(DEVICE_ID, json.getString("device_id"))
            assertEquals("android", json.getString("platform"))
            assertEquals("test-version", json.getString("app_version"))
        }
    }

    @Test
    fun noClaimIsANormalSuccessfulPoll() = runBlocking {
        assertEquals(AdviceAttentionClaimResult.None, client("{\"status\":\"none\"}").claim())
    }

    @Test
    fun unsupportedThirdDeliveryFailsClosed() {
        assertEquals(
            AdviceAttentionClaimResult.RetryableFailure,
            parseAdviceAttentionClaim(claimedResponse(deliveryNumber = 3)),
        )
    }

    @Test
    fun completeUsesClaimTokenAndReturnsNextEligibility() = runBlocking {
        val captured = AtomicReference<Request>()
        val service = client(
            responseBody =
                """{"status":"delivered","delivery_count":1,"next_eligible_at":"2026-08-09T14:00:00Z"}""",
            capture = captured::set,
        )

        val result = service.complete(ADVICE_ID, CLAIM_TOKEN)

        assertTrue(result is AdviceAttentionOperationResult.Completed)
        val completion = (result as AdviceAttentionOperationResult.Completed).completion
        assertEquals(1, completion.deliveryCount)
        assertEquals(Instant.parse("2026-08-09T14:00:00Z").toEpochMilli(), completion.nextEligibleAt)
        with(captured.get()) {
            assertEquals("/v1/advice-attention/$ADVICE_ID/complete", url.encodedPath)
            val json = JSONObject(Buffer().also { body!!.writeTo(it) }.readUtf8())
            assertEquals(DEVICE_ID, json.getString("device_id"))
            assertEquals(CLAIM_TOKEN, json.getString("claim_token"))
        }
    }

    @Test
    fun secondCompletionHasNoFutureNotificationPlan() {
        val result = parseAdviceAttentionCompletion(
            """{"status":"delivered","delivery_count":2,"next_eligible_at":null}""",
        )

        assertTrue(result is AdviceAttentionOperationResult.Completed)
        assertNull((result as AdviceAttentionOperationResult.Completed).completion.nextEligibleAt)
    }

    @Test
    fun failReleasesWithoutCompletingAndConflictIsTerminal() = runBlocking {
        val captured = AtomicReference<Request>()
        val released = client(
            responseBody = """{"status":"released","delivery_count":1}""",
            capture = captured::set,
        ).fail(ADVICE_ID, CLAIM_TOKEN, "notification_unavailable")

        assertEquals(AdviceAttentionOperationResult.Released, released)
        with(captured.get()) {
            assertEquals("/v1/advice-attention/$ADVICE_ID/fail", url.encodedPath)
            val json = JSONObject(Buffer().also { body!!.writeTo(it) }.readUtf8())
            assertEquals("notification_unavailable", json.getString("reason"))
        }
        assertEquals(
            AdviceAttentionOperationResult.Terminal,
            client(responseBody = "{}", responseCode = 409).complete(ADVICE_ID, CLAIM_TOKEN),
        )
    }

    @Test
    fun deviceIdentityAcceptsOnlyCanonicalUuidValues() {
        assertEquals(DEVICE_ID, normalizeDeviceId("  $DEVICE_ID "))
        assertNull(normalizeDeviceId("android-phone"))
        assertNull(normalizeDeviceId(null))
    }

    private fun client(
        responseBody: String,
        responseCode: Int = 200,
        capture: (Request) -> Unit = {},
    ): AdviceAttentionClient {
        val http = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()
                capture(request)
                Response.Builder()
                    .request(request)
                    .protocol(Protocol.HTTP_1_1)
                    .code(responseCode)
                    .message("test")
                    .body(responseBody.toResponseBody())
                    .build()
            }
            .build()
        return AdviceAttentionClient(
            connectionProvider = {
                BackendConnection(
                    baseUrl = "https://backend.example",
                    userId = "owner-1",
                    bearerToken = "token-1",
                )
            },
            deviceId = DEVICE_ID,
            appVersion = "test-version",
            client = http,
        )
    }

    private fun claimedResponse(deliveryNumber: Int): String =
        """
        {
          "status": "claimed",
          "claim_token": "$CLAIM_TOKEN",
          "lease_expires_at": "2026-08-09T13:00:00Z",
          "delivery_number": $deliveryNumber,
          "advice": {
            "id": "$ADVICE_ID",
            "domain": "work",
            "requested_level": 2,
            "goal_quote": "本周完成发布",
            "evidence": [],
            "action": "锁定发布范围",
            "first_step": "打开任务清单",
            "prediction": {},
            "dedupe_key": "release",
            "delivery": "immediate",
            "status": "active",
            "created_at": "2026-08-09T12:00:00Z"
          }
        }
        """.trimIndent()

    private companion object {
        const val ADVICE_ID = "550e8400-e29b-41d4-a716-446655440000"
        const val CLAIM_TOKEN = "claim-token-test"
        const val DEVICE_ID = "3c37c173-7f63-448c-b2be-967771f7d71c"
    }
}

class AdviceAttentionCoordinatorTest {
    @Test
    fun notificationFailureCallsFailAndNeverCompletesDelivery() = runBlocking {
        val store = FakePendingStore()
        var completes = 0
        var failures = 0
        val coordinator = coordinator(
            store = store,
            postNotification = { _, _ -> false },
            complete = { _, _ ->
                completes += 1
                completed()
            },
            fail = { _, _, _ ->
                failures += 1
                AdviceAttentionOperationResult.Released
            },
        )

        assertEquals(AdviceAttentionStepResult.Released, coordinator.deliverOne())
        assertEquals(0, completes)
        assertEquals(1, failures)
        assertNull(store.load())
    }

    @Test
    fun unavailableNotificationPermissionPreventsClaim() = runBlocking {
        val store = FakePendingStore()
        var claims = 0
        val coordinator = coordinator(
            store = store,
            notificationAvailable = { false },
            claim = {
                claims += 1
                AdviceAttentionClaimResult.None
            },
        )

        assertEquals(AdviceAttentionStepResult.None, coordinator.deliverOne())
        assertEquals(0, claims)
        assertNull(store.load())
    }

    @Test
    fun postedNotificationIsCompletedAndSecondOrdinalReachesNotifier() = runBlocking {
        val store = FakePendingStore()
        var deliveredOrdinal: Int? = null
        var persisted: AdviceEntity? = null
        val coordinator = coordinator(
            store = store,
            persistAdvice = { persisted = it },
            postNotification = { _, ordinal ->
                deliveredOrdinal = ordinal
                true
            },
        )

        val result = coordinator.deliverOne()

        assertTrue(result is AdviceAttentionStepResult.Delivered)
        assertEquals(2, deliveredOrdinal)
        assertEquals(ADVICE_ID, persisted?.id)
        assertNull(store.load())
    }

    @Test
    fun postedPendingClaimCompletesAfterRestartWithoutPostingAgain() = runBlocking {
        val store = FakePendingStore(
            PendingAdviceAttention(
                adviceId = ADVICE_ID,
                claimToken = CLAIM_TOKEN,
                deliveryNumber = 1,
                notificationPosted = true,
            ),
        )
        var notifications = 0
        var completions = 0
        val coordinator = coordinator(
            store = store,
            postNotification = { _, _ ->
                notifications += 1
                true
            },
            complete = { _, _ ->
                completions += 1
                completed()
            },
        )

        assertTrue(coordinator.recoverPending() is AdviceAttentionStepResult.Delivered)
        assertEquals(0, notifications)
        assertEquals(1, completions)
        assertNull(store.load())
    }

    @Test
    fun pendingClaimEncodingRoundTripsAndRejectsThirdDelivery() {
        val pending = PendingAdviceAttention(ADVICE_ID, CLAIM_TOKEN, 2, notificationPosted = true)

        assertEquals(pending, decodePendingAdviceAttention(encodePendingAdviceAttention(pending)))
        assertNull(
            decodePendingAdviceAttention(
                JSONObject(encodePendingAdviceAttention(pending)).put("delivery_number", 3).toString(),
            ),
        )
    }

    @Test
    fun onlyActiveImmediateAdviceMayReachAndroidNotificationManager() {
        assertTrue(isAdviceEligibleForAttention(advice()))
        assertFalse(isAdviceEligibleForAttention(advice().copy(delivery = "brief")))
        assertFalse(isAdviceEligibleForAttention(advice().copy(status = "withdrawn")))
    }

    private fun coordinator(
        store: FakePendingStore,
        notificationAvailable: () -> Boolean = { true },
        claim: suspend () -> AdviceAttentionClaimResult = {
            AdviceAttentionClaimResult.Claimed(
                advice = advice(),
                claimToken = CLAIM_TOKEN,
                deliveryNumber = 2,
                leaseExpiresAt = Long.MAX_VALUE,
            )
        },
        persistAdvice: suspend (AdviceEntity) -> Unit = {},
        postNotification: (AdviceEntity, Int) -> Boolean = { _, _ -> true },
        complete: suspend (String, String) -> AdviceAttentionOperationResult = { _, _ -> completed() },
        fail: suspend (String, String, String) -> AdviceAttentionOperationResult = { _, _, _ ->
            AdviceAttentionOperationResult.Released
        },
    ) = AdviceAttentionCoordinator(
        notificationAvailable = notificationAvailable,
        claim = claim,
        persistAdvice = persistAdvice,
        postNotification = postNotification,
        complete = complete,
        fail = fail,
        pendingStore = store,
    )

    private fun completed() = AdviceAttentionOperationResult.Completed(
        AdviceAttentionCompletion(deliveryCount = 2, nextEligibleAt = null),
    )

    private fun advice() = AdviceEntity(
        id = ADVICE_ID,
        domain = "work",
        level = 2,
        goalQuote = "本周完成发布",
        evidenceJson = "[]",
        action = "锁定发布范围",
        firstStep = "打开任务清单",
        predictionJson = "{}",
        dedupeKey = "release",
    )

    private class FakePendingStore(initial: PendingAdviceAttention? = null) : AdviceAttentionPendingStore {
        private var value = initial

        override fun load(): PendingAdviceAttention? = value

        override fun save(value: PendingAdviceAttention): Boolean {
            this.value = value
            return true
        }

        override fun clear(): Boolean {
            value = null
            return true
        }
    }

    private companion object {
        const val ADVICE_ID = "550e8400-e29b-41d4-a716-446655440000"
        const val CLAIM_TOKEN = "claim-token-test"
    }
}
