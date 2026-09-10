package com.mouchen.app.collectors

import java.time.ZonedDateTime
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

class UsageStatsCollectorTest {
    @Test
    fun firstCollectionBackfillsRollingSevenDays() {
        val end = ZonedDateTime.parse("2026-08-02T12:00:00+08:00").toInstant().toEpochMilli()
        val expected = end - 7L * 24 * 60 * 60 * 1000

        assertEquals(expected, usageCollectionStart(end, null))
    }

    @Test
    fun incrementalCollectionUsesWatermark() {
        val end = ZonedDateTime.parse("2026-08-02T12:00:00+08:00").toInstant().toEpochMilli()
        val watermark = end - 15 * 60 * 1000

        assertEquals(watermark, usageCollectionStart(end, watermark))
    }

    @Test
    fun futureWatermarkIsClampedToNow() {
        val end = ZonedDateTime.parse("2026-08-02T12:00:00+08:00").toInstant().toEpochMilli()

        assertEquals(end, usageCollectionStart(end, end + 60_000))
    }

    @Test
    fun staleWatermarkIsClampedToRollingSevenDays() {
        val end = ZonedDateTime.parse("2026-08-02T12:00:00+08:00").toInstant().toEpochMilli()

        assertEquals(end - 7L * 24 * 60 * 60 * 1000, usageCollectionStart(end, end - 30L * 24 * 60 * 60 * 1000))
    }

    @Test
    fun openSessionCrossingIncrementalWatermarkIsRetained() {
        val end = ZonedDateTime.parse("2026-08-02T12:00:00+08:00").toInstant().toEpochMilli()
        val watermark = end - 30 * 60 * 1000
        val sessionStartedTwoHoursAgo = end - 2 * 60 * 60 * 1000

        assertEquals(watermark, usageCollectionStart(end, watermark))
        assertEquals(true, shouldRetainOpenUsageSession(sessionStartedTwoHoursAgo, end))
        assertEquals(false, shouldRetainOpenUsageSession(end - 8L * 24 * 60 * 60 * 1000, end))
    }

    @Test
    fun foregroundSessionIdsAreStableAndBoundToEndTime() {
        val first = usageSessionId("example.app", 100L, 200L)

        assertEquals(first, usageSessionId("example.app", 100L, 200L))
        assertNotEquals(first, usageSessionId("example.app", 100L, 201L))
    }
}
