package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ImapUidPagingTest {
    @Test
    fun findsFirstActualMessageAcrossLargeUidGap() {
        val uids = listOf(10L, 20L, 1_000_000L, 1_500_000L)

        val sequence = firstSequenceAfterUid(uids.size, 20L) { uids[it - 1] }

        assertEquals(3, sequence)
    }

    @Test
    fun startsAtOldestMessageForEmptyWatermark() {
        val uids = listOf(50_000L, 75_000L)

        val sequence = firstSequenceAfterUid(uids.size, 0L) { uids[it - 1] }

        assertEquals(1, sequence)
    }

    @Test
    fun returnsNullWhenWatermarkIsCurrent() {
        val uids = listOf(10L, 20L, 30L)

        val sequence = firstSequenceAfterUid(uids.size, 30L) { uids[it - 1] }

        assertNull(sequence)
    }
}
