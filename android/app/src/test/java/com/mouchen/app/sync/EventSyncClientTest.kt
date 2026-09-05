package com.mouchen.app.sync

import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.LocalEventEntity
import java.time.Instant
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import okhttp3.Request
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EventSyncClientTest {
    @Test
    fun goalListResponseRestoresCanonicalServerRecord() {
        val records = parseGoalRecords(
            """
            [
              {
                "id": "goal-1",
                "user_id": "owner",
                "domain": "work",
                "quote": "Ship the private alpha this week",
                "title": "Private alpha",
                "target": {"weekly_hours": 12, "keywords": ["alpha"]},
                "is_redline": true,
                "valid_until": null,
                "version": 3,
                "created_at": "2026-08-03T06:30:00Z",
                "reaffirmed_at": "2026-08-03T06:30:00Z"
              }
            ]
            """.trimIndent(),
        )

        assertEquals(1, records.size)
        with(records.single()) {
            assertEquals("goal-1", id)
            assertEquals("work", domain)
            assertEquals("Private alpha", title)
            assertEquals("Ship the private alpha this week", quote)
            assertEquals(3, version)
            assertTrue(isRedline)
            assertEquals(12, org.json.JSONObject(targetJson).getInt("weekly_hours"))
            assertEquals(Instant.parse("2026-08-03T06:30:00Z").toEpochMilli(), createdAt)
            assertTrue(synced)
        }
    }

    @Test
    fun adviceListResponseMapsBackendRecord() {
        val records = parseAdviceRecords(
            """
            [
              {
                "id": "advice-1",
                "domain": "work",
                "requested_level": 2,
                "effective_level": "L3",
                "goal_quote": "本周完成发布",
                "evidence": [{"fact": "已经连续三天没有进展"}],
                "action": "今天锁定发布范围",
                "first_step": "打开任务清单并删去非必要项",
                "prediction": {"outcome": "明天可开始验收"},
                "adopted_expected_result": "锁定范围后明天进入验收",
                "adopted_confidence": 0.82,
                "dedupe_key": "release-drift",
                "status": "active",
                "created_at": "2026-08-03T06:30:00Z"
              }
            ]
            """.trimIndent(),
        )

        assertEquals(1, records.size)
        with(records.single()) {
            assertEquals("advice-1", id)
            assertEquals("work", domain)
            assertEquals(3, level)
            assertEquals("本周完成发布", goalQuote)
            assertEquals("今天锁定发布范围", action)
            assertEquals(
                "锁定范围后明天进入验收",
                org.json.JSONObject(predictionJson).getString("adopted_expected_result"),
            )
            assertEquals(
                0.82,
                org.json.JSONObject(predictionJson).getDouble("adopted_confidence"),
                0.0,
            )
            assertEquals("release-drift", dedupeKey)
            assertEquals(Instant.parse("2026-08-03T06:30:00Z").toEpochMilli(), createdAt)
        }
    }

    @Test
    fun newAdviceIsInsertedWithoutBypassingCentralAttentionClaim() {
        val incoming = advice(id = "new-advice", status = "active")

        assertEquals(
            AdviceMergeAction.Insert(incoming),
            adviceMergeAction(existing = null, incoming = incoming),
        )
    }

    @Test
    fun briefAdviceIsStoredWithoutInterruptingTheUser() {
        val incoming = advice(id = "brief-advice", status = "active", delivery = "brief")

        assertEquals(
            AdviceMergeAction.Insert(incoming),
            adviceMergeAction(existing = null, incoming = incoming),
        )
    }

    @Test
    fun existingAdviceOnlyProducesStatusUpdate() {
        val existing = advice(
            id = "known-advice",
            status = "active",
            action = "保留本地内容",
            notifiedAt = 123L,
        )
        val incoming = advice(id = "known-advice", status = "withdrawn", action = "服务端内容已变化")

        val action = adviceMergeAction(existing, incoming)

        assertTrue(action is AdviceMergeAction.UpdateStatus)
        assertEquals(
            AdviceMergeAction.UpdateStatus(
                advice = existing.copy(status = "withdrawn"),
            ),
            action,
        )
    }

    @Test
    fun missedImmediateAdviceStillOnlyMergesLocalState() {
        val existing = advice(id = "missed-advice", status = "active", notifiedAt = null)

        assertEquals(
            AdviceMergeAction.UpdateStatus(advice = existing),
            adviceMergeAction(existing, existing),
        )
    }

    @Test
    fun deliveredAdviceIsNotNotifiedTwice() {
        val existing = advice(id = "delivered-advice", status = "active", notifiedAt = 123L)

        assertEquals(
            AdviceMergeAction.UpdateStatus(advice = existing),
            adviceMergeAction(existing, existing),
        )
    }

    @Test
    fun pulledAdoptionPreservesLocalNotificationLedgers() {
        val existing = advice(
            id = "adopted-advice",
            status = "active",
            notifiedAt = 123L,
            followUpNotifiedAt = 456L,
        )
        val incoming = advice(id = "adopted-advice", status = "adopted")

        assertEquals(
            AdviceMergeAction.UpdateStatus(
                advice = existing.copy(status = "adopted"),
            ),
            adviceMergeAction(existing, incoming),
        )
    }

    @Test
    fun withdrawnImmediateAdviceIsNeverNotified() {
        val incoming = advice(id = "withdrawn-advice", status = "withdrawn")

        assertEquals(
            AdviceMergeAction.Insert(incoming),
            adviceMergeAction(existing = null, incoming = incoming),
        )
    }

    @Test
    fun unpublishedEventResponseDoesNotCreateAdvice() {
        val response = """{"evaluation":{"decision":"suppress"}}"""

        assertNull(parsePublishedAdvice(response))
    }

    @Test
    fun successfulSingleEventDeliveryIsIdempotentAndMarksOnlyAfterAcceptance() = runBlocking {
        var stored = LocalEventEntity(
            id = "event-1",
            source = "test",
            type = "ui.visible_text",
            payloadJson = "{}",
        )
        var sends = 0
        val calls = mutableListOf<String>()
        suspend fun deliver() = deliverSingleEvent(
            eventId = stored.id,
            loadEvent = {
                calls += "load"
                stored
            },
            sendEvent = {
                calls += "send"
                sends += 1
                SyncDelivery(accepted = true)
            },
            persistIncomingAdvice = { calls += "advice" },
            markDelivered = { _, _ ->
                calls += "mark"
                stored = stored.copy(synced = true)
                false
            },
        )

        assertEquals(SingleEventSyncResult.DELIVERED, deliver())
        assertEquals(SingleEventSyncResult.ALREADY_SYNCED, deliver())
        assertEquals(1, sends)
        assertEquals(listOf("load", "send", "mark", "load"), calls)
    }

    @Test
    fun failedSingleEventDeliveryNeverMarksTheDurableRowSynced() = runBlocking {
        var marked = false
        val event = LocalEventEntity(
            id = "event-failed",
            source = "test",
            type = "ui.visible_text",
            payloadJson = "{}",
        )

        val result = deliverSingleEvent(
            eventId = event.id,
            loadEvent = { event },
            sendEvent = { SyncDelivery(accepted = false) },
            persistIncomingAdvice = {},
            markDelivered = { _, _ ->
                marked = true
                false
            },
        )

        assertEquals(SingleEventSyncResult.FAILED, result)
        assertEquals(false, marked)
    }

    @Test
    fun cancelledLockWaiterReleasesItsRegistryReference() = runBlocking {
        val registry = EventDeliveryLockRegistry()
        val firstEntered = CompletableDeferred<Unit>()
        val releaseFirst = CompletableDeferred<Unit>()
        val first = launch(Dispatchers.Default) {
            registry.withLock("event-lock") {
                firstEntered.complete(Unit)
                releaseFirst.await()
            }
        }
        firstEntered.await()
        val second = launch(Dispatchers.Default) {
            registry.withLock("event-lock") { error("cancelled waiter entered the lock") }
        }
        while (registry.referenceCount("event-lock") < 2) yield()

        second.cancelAndJoin()

        assertEquals(1, registry.referenceCount("event-lock"))
        releaseFirst.complete(Unit)
        first.join()
        assertEquals(0, registry.activeEntryCount())
    }

    @Test
    fun semanticFastLaneHeaderIsOptIn() {
        val ordinary = Request.Builder()
            .url("https://example.com/v1/events")
            .applySemanticFastLane(false)
            .build()
        val urgent = Request.Builder()
            .url("https://example.com/v1/events")
            .applySemanticFastLane(true)
            .build()

        assertNull(ordinary.header("X-Semantic-Fast-Lane"))
        assertEquals("true", urgent.header("X-Semantic-Fast-Lane"))
    }

    @Test
    fun rawCloudHeaderRequiresBothOwnerSwitches() {
        fun request(minimized: Boolean, proactive: Boolean): Request = Request.Builder()
            .url("https://example.com/v1/events")
            .applyCloudApprovalHeaders(
                BackendConnection(
                    baseUrl = "https://example.com",
                    minimizedContextOnly = minimized,
                    proactiveCloudEnabled = proactive,
                ),
            )
            .build()

        assertEquals("false", request(minimized = true, proactive = false).header("X-Raw-Cloud-Approved"))
        assertEquals("false", request(minimized = true, proactive = true).header("X-Raw-Cloud-Approved"))
        assertEquals("false", request(minimized = false, proactive = false).header("X-Raw-Cloud-Approved"))
        assertEquals("true", request(minimized = false, proactive = true).header("X-Raw-Cloud-Approved"))
        assertEquals("true", request(minimized = false, proactive = true).header("X-Proactive-Cloud-Approved"))
    }

    @Test
    fun deliveryBackoffIsBoundedAndOverflowSafe() {
        assertEquals(16_000L, nextDeliveryAttemptAt(now = 1_000L, attemptsBeforeFailure = 0))
        assertEquals(31_000L, nextDeliveryAttemptAt(now = 1_000L, attemptsBeforeFailure = 1))
        assertEquals(21_601_000L, nextDeliveryAttemptAt(now = 1_000L, attemptsBeforeFailure = 99))
        assertEquals(Long.MAX_VALUE, nextDeliveryAttemptAt(Long.MAX_VALUE - 1, 0))
    }

    @Test
    fun queuedResponsesRequireAnIndependentAdvicePull() {
        assertTrue(isQueuedDelivery(202, ""))
        assertTrue(isQueuedDelivery(200, "{\"status\":\"queued\"}"))
        assertFalse(isQueuedDelivery(200, "{\"evaluation\":{\"decision\":\"suppress\"}}"))
    }

    @Test
    fun queuedDeliveryIsDurablyMarkedAndCarriesPullRequirement() = runBlocking {
        var marked = false
        val calls = mutableListOf<String>()
        val event = LocalEventEntity(
            id = "event-queued",
            source = "test",
            type = "ui.visible_text",
            payloadJson = "{}",
        )

        val outcome = deliverSingleEventOutcome(
            eventId = event.id,
            loadEvent = { event },
            sendEvent = {
                calls += "send"
                SyncDelivery(accepted = true, advicePullRequired = true)
            },
            persistIncomingAdvice = {},
            markDelivered = { _, queued ->
                assertTrue(queued)
                calls += "mark"
                marked = true
                true
            },
        )

        assertEquals(SingleEventSyncResult.DELIVERED, outcome.result)
        assertTrue(outcome.advicePullRequired)
        assertTrue(marked)
        assertEquals(listOf("send", "mark"), calls)
    }

    @Test
    fun queuedDemandPersistenceFailureLeavesEventReplayable() = runBlocking {
        var marked = false
        val event = LocalEventEntity(
            id = "event-queued-crash-window",
            source = "test",
            type = "ui.visible_text",
            payloadJson = "{}",
        )

        val outcome = deliverSingleEventOutcome(
            eventId = event.id,
            loadEvent = { event },
            sendEvent = { SyncDelivery(accepted = true, advicePullRequired = true) },
            persistIncomingAdvice = {},
            markDelivered = { _, _ -> error("Room transaction failed") },
        )

        assertEquals(SingleEventSyncResult.FAILED, outcome.result)
        assertFalse(marked)
    }

    private fun advice(
        id: String,
        status: String,
        action: String = "采取行动",
        delivery: String = "immediate",
        notifiedAt: Long? = null,
        followUpNotifiedAt: Long? = null,
    ) = AdviceEntity(
        id = id,
        domain = "work",
        level = 2,
        goalQuote = "完成发布",
        evidenceJson = "[]",
        action = action,
        firstStep = "打开任务清单",
        predictionJson = "{}",
        dedupeKey = id,
        delivery = delivery,
        status = status,
        notifiedAt = notifiedAt,
        followUpNotifiedAt = followUpNotifiedAt,
        createdAt = 1L,
    )
}
