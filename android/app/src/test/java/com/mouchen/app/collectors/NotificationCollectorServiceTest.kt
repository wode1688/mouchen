package com.mouchen.app.collectors

import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.sync.UrgencyCategory
import com.mouchen.app.sync.UrgencyClassifier
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationCollectorServiceTest {
    @Test
    fun activeNotificationBackfillRoutesEveryUrgentRowThroughEventFastLane() = runBlocking {
        val events = listOf(
            notification("检测到异地登录，请确认是否为本人"),
            notification("我现在胸痛了，呼吸困难"),
            notification("发布任务今天截止"),
        )
        val routed = mutableListOf<Pair<LocalEventEntity, Boolean>>()

        val written = writeNotificationBackfill(events) { event, enqueueSync ->
            routed += event to enqueueSync
            true
        }

        assertEquals(3, written)
        assertTrue(routed.all { it.second })
        assertEquals(
            listOf(
                UrgencyCategory.ACCOUNT_SECURITY,
                UrgencyCategory.URGENT_HEALTH,
                UrgencyCategory.DEADLINE,
            ),
            routed.map { UrgencyClassifier.classify(it.first) },
        )
    }

    @Test
    fun rejectedBackfillWriteDoesNotStopLaterEventsFromRouting() = runBlocking {
        val events = listOf(notification("普通通知一"), notification("普通通知二"))
        var calls = 0

        val written = writeNotificationBackfill(events) { _, enqueueSync ->
            assertTrue(enqueueSync)
            calls += 1
            calls == 2
        }

        assertEquals(2, calls)
        assertEquals(1, written)
    }

    private fun notification(text: String) = LocalEventEntity(
        source = "android.notification",
        type = "notification.posted",
        payloadJson = JSONObject()
            .put("text", text)
            .put("current_notification_backfill", true)
            .toString(),
    )
}
