package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class NotificationCollectorTest {
    @Test
    fun currentNotificationBackfillUsesStableIdentity() {
        val first = notificationEventId("chat.example", "0|chat.example|42|null|1000", 1_000L, "hello")

        assertEquals(first, notificationEventId("chat.example", "0|chat.example|42|null|1000", 1_000L, "hello"))
        assertNotEquals(first, notificationEventId("chat.example", "0|chat.example|43|null|1000", 1_000L, "hello"))
        assertNotEquals(first, notificationEventId("chat.example", "0|chat.example|42|null|1000", 1_000L, "updated body"))
    }
}
