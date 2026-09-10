package com.mouchen.app.sync

import com.mouchen.app.data.AdviceEntity
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

class AdviceFeedbackClientTest {
    @Test
    fun adoptedAdviceSchedulesPredictionFollowUp() = runBlocking {
        var scheduled: AdviceEntity? = null
        val service = client(
            httpCode = 200,
            statusUpdater = { _, _ -> 1 },
            adviceLoader = { advice() },
            followUpScheduler = { scheduled = it },
        )

        val result = service.submit(ADVICE_ID, AdviceFeedbackKind.Adopted)

        assertTrue(result.accepted)
        assertEquals(ADVICE_ID, scheduled?.id)
    }

    @Test
    fun rejectedAdoptionDoesNotScheduleFollowUp() = runBlocking {
        var scheduled = 0
        val service = client(
            httpCode = 500,
            statusUpdater = { _, _ -> 1 },
            adviceLoader = { advice() },
            followUpScheduler = { scheduled += 1 },
        )

        val result = service.submit(ADVICE_ID, AdviceFeedbackKind.Adopted)

        assertFalse(result.accepted)
        assertEquals(0, scheduled)
    }

    @Test
    fun explicitErrorFeedbackPostsKindDismissesLocallyAndCancelsFollowUp() = runBlocking {
        val errorKinds = listOf(
            AdviceFeedbackKind.FactError,
            AdviceFeedbackKind.PredictionError,
            AdviceFeedbackKind.Irrelevant,
            AdviceFeedbackKind.TimingError,
        )

        errorKinds.forEach { kind ->
            val captured = AtomicReference<Request>()
            var localUpdate: Pair<String, String>? = null
            var cancelled: String? = null
            val service = client(
                httpCode = 200,
                capture = captured::set,
                statusUpdater = { id, status ->
                    localUpdate = id to status
                    1
                },
                followUpCanceller = { cancelled = it },
            )

            val result = service.submit(ADVICE_ID, kind)

            assertTrue(result.accepted)
            assertEquals(ADVICE_ID to "dismissed", localUpdate)
            assertEquals(ADVICE_ID, cancelled)
            with(captured.get()) {
                assertEquals("/v1/advice/$ADVICE_ID/feedback", url.encodedPath)
                val body = Buffer().also { body!!.writeTo(it) }.readUtf8()
                val json = JSONObject(body)
                assertEquals(kind.apiValue, json.getString("kind"))
                assertEquals(FEEDBACK_ID, json.getString("feedback_id"))
            }
        }
    }

    @Test
    fun laterPreservesHistoryDismissesCurrentNotificationAndKeepsFutureWakeup() = runBlocking {
        val captured = AtomicReference<Request>()
        var localUpdates = 0
        var attentionCancelled = 0
        var notificationDismissed: String? = null
        val service = client(
            httpCode = 200,
            capture = captured::set,
            statusUpdater = { _, _ ->
                localUpdates += 1
                1
            },
            attentionCanceller = { attentionCancelled += 1 },
            attentionDismisser = { notificationDismissed = it },
        )

        val result = service.submit(ADVICE_ID, AdviceFeedbackKind.Later)

        assertTrue(result.accepted)
        assertEquals(0, localUpdates)
        assertEquals(0, attentionCancelled)
        assertEquals(ADVICE_ID, notificationDismissed)
        val body = JSONObject(Buffer().also { captured.get().body!!.writeTo(it) }.readUtf8())
        assertEquals("later", body.getString("kind"))
        assertEquals(FEEDBACK_ID, body.getString("feedback_id"))
    }

    @Test
    fun acknowledgedPreservesHistoryAndTerminatesAttentionWithoutMarkingIrrelevant() = runBlocking {
        val captured = AtomicReference<Request>()
        var localUpdates = 0
        var attentionCancelled: String? = null
        var notificationDismissed: String? = null
        val service = client(
            httpCode = 200,
            capture = captured::set,
            statusUpdater = { _, _ ->
                localUpdates += 1
                1
            },
            attentionCanceller = { attentionCancelled = it },
            attentionDismisser = { notificationDismissed = it },
        )

        val result = service.submit(ADVICE_ID, AdviceFeedbackKind.Acknowledged)

        assertTrue(result.accepted)
        assertEquals(0, localUpdates)
        assertEquals(ADVICE_ID, attentionCancelled)
        assertEquals(ADVICE_ID, notificationDismissed)
        val body = JSONObject(Buffer().also { captured.get().body!!.writeTo(it) }.readUtf8())
        assertEquals("acknowledged", body.getString("kind"))
        assertEquals(FEEDBACK_ID, body.getString("feedback_id"))
    }

    @Test
    fun failedLaterDoesNotEndCurrentReminder() = runBlocking {
        var dismissed = 0
        val service = client(
            httpCode = 500,
            statusUpdater = { _, _ -> 1 },
            attentionDismisser = { dismissed += 1 },
        )

        val result = service.submit(ADVICE_ID, AdviceFeedbackKind.Later)

        assertFalse(result.accepted)
        assertEquals(0, dismissed)
    }

    @Test
    fun reachedOutcomePostsCorrectAndMarksAdviceVerified() = runBlocking {
        val captured = AtomicReference<Request>()
        var localUpdate: Pair<String, String>? = null
        val service = client(
            httpCode = 200,
            capture = captured::set,
            statusUpdater = { id, status ->
                localUpdate = id to status
                1
            },
        )

        val result = service.submitOutcome(ADVICE_ID, AdviceOutcomeKind.Correct)

        assertTrue(result.accepted)
        assertEquals(ADVICE_ID to "verified", localUpdate)
        with(captured.get()) {
            assertEquals("/v1/advice/$ADVICE_ID/outcome", url.encodedPath)
            assertEquals("owner-1", header("X-User-Id"))
            assertEquals("Bearer token-1", header("Authorization"))
            val body = Buffer().also { body!!.writeTo(it) }.readUtf8()
            val json = JSONObject(body)
            assertEquals("correct", json.getString("status"))
            assertEquals("用户在 Android 端确认：结果达成", json.getString("actual_result"))
        }
    }

    @Test
    fun failedOutcomeDoesNotChangeLocalStatus() = runBlocking {
        val captured = AtomicReference<Request>()
        var updates = 0
        val service = client(
            httpCode = 500,
            capture = captured::set,
            statusUpdater = { _, _ ->
                updates += 1
                1
            },
        )

        val result = service.submitOutcome(ADVICE_ID, AdviceOutcomeKind.Incorrect)

        assertFalse(result.accepted)
        assertEquals(0, updates)
        val buffer = Buffer().also { captured.get().body!!.writeTo(it) }
        assertEquals("incorrect", JSONObject(buffer.readUtf8()).getString("status"))
    }

    @Test
    fun alreadyRecordedOutcomeRepairsLocalVerifiedStatus() = runBlocking {
        var localStatus: String? = null
        var cancelled: String? = null
        val service = client(
            httpCode = 409,
            statusUpdater = { _, status ->
                localStatus = status
                1
            },
            followUpCanceller = { cancelled = it },
        )

        val result = service.submitOutcome(ADVICE_ID, AdviceOutcomeKind.Correct)

        assertTrue(result.accepted)
        assertEquals("verified", localStatus)
        assertEquals(ADVICE_ID, cancelled)
    }

    @Test
    fun guidanceRequiresNoteThenPostsItAndStopsFurtherAttention() = runBlocking {
        val captured = AtomicReference<Request>()
        var localStatus: String? = null
        var attentionCancelled: String? = null
        var notificationDismissed: String? = null
        val service = client(
            httpCode = 200,
            capture = captured::set,
            statusUpdater = { _, status ->
                localStatus = status
                1
            },
            attentionCanceller = { attentionCancelled = it },
            attentionDismisser = { notificationDismissed = it },
        )

        val empty = service.submitGuidance(ADVICE_ID, "   ")
        assertFalse(empty.accepted)
        assertNull(captured.get())

        val result = service.submitGuidance(ADVICE_ID, "  先关注现金流，不要建议扩张  ")

        assertTrue(result.accepted)
        assertNull(localStatus)
        assertEquals(ADVICE_ID, attentionCancelled)
        assertEquals(ADVICE_ID, notificationDismissed)
        with(captured.get()) {
            assertEquals("/v1/advice/$ADVICE_ID/feedback", url.encodedPath)
            val body = JSONObject(Buffer().also { body!!.writeTo(it) }.readUtf8())
            assertEquals("guidance", body.getString("kind"))
            assertEquals("先关注现金流，不要建议扩张", body.getString("note"))
            assertEquals(FEEDBACK_ID, body.getString("feedback_id"))
        }
    }

    @Test
    fun oversizedGuidanceIsRejectedBeforeNetwork() = runBlocking {
        val captured = AtomicReference<Request>()
        val service = client(
            httpCode = 200,
            capture = captured::set,
            statusUpdater = { _, _ -> 1 },
        )

        val result = service.submitGuidance(
            ADVICE_ID,
            "方".repeat(AdviceFeedbackClient.MAX_GUIDANCE_NOTE_LENGTH + 1),
        )

        assertFalse(result.accepted)
        assertNull(captured.get())
    }

    private fun client(
        httpCode: Int,
        capture: (Request) -> Unit = {},
        statusUpdater: suspend (String, String) -> Int,
        adviceLoader: suspend (String) -> AdviceEntity? = { null },
        followUpScheduler: (AdviceEntity) -> Unit = {},
        followUpCanceller: (String) -> Unit = {},
        attentionCanceller: (String) -> Unit = {},
        attentionDismisser: (String) -> Unit = {},
    ): AdviceFeedbackClient {
        val http = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()
                capture(request)
                Response.Builder()
                    .request(request)
                    .protocol(Protocol.HTTP_1_1)
                    .code(httpCode)
                    .message("test")
                    .body("{}".toResponseBody())
                    .build()
            }
            .build()
        return AdviceFeedbackClient(
            connectionProvider = {
                BackendConnection(
                    baseUrl = "https://backend.example",
                    userId = "owner-1",
                    bearerToken = "token-1",
                )
            },
            client = http,
            statusUpdater = statusUpdater,
            adviceLoader = adviceLoader,
            followUpScheduler = followUpScheduler,
            followUpCanceller = followUpCanceller,
            attentionCanceller = attentionCanceller,
            attentionDismisser = attentionDismisser,
            feedbackIdFactory = { FEEDBACK_ID },
        )
    }

    private fun advice() = AdviceEntity(
        id = ADVICE_ID,
        domain = "work",
        level = 2,
        goalQuote = "今天完成发布",
        evidenceJson = "[]",
        action = "先处理失败构建",
        firstStep = "打开错误日志",
        predictionJson = "{\"outcome\":\"发布恢复\",\"deadline\":\"2030-01-01T00:00:00Z\"}",
        dedupeKey = "build-failed",
        status = "adopted",
    )

    private companion object {
        const val ADVICE_ID = "550e8400-e29b-41d4-a716-446655440000"
        const val FEEDBACK_ID = "b97886bb-1212-4af0-a1ff-feb5f8bf8bd0"
    }
}
