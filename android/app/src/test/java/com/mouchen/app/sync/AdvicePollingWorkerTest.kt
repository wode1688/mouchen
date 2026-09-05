package com.mouchen.app.sync

import com.mouchen.app.data.MAX_PENDING_ANALYSIS_ATTEMPTS
import com.mouchen.app.data.PendingAnalysisEntity
import com.mouchen.app.data.pendingAnalysisNextAttemptAt
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AdvicePollingWorkerTest {
    private val emptySuccessfulPull = SyncReport(
        attempted = 0,
        synced = 0,
        failed = 0,
        adviceAttempted = 1,
        adviceSynced = 1,
        adviceReceived = 0,
    )

    @Test
    fun queuedPollRetriesPastFifteenSecondsThenStopsAtFiniteBound() {
        assertEquals(5_000L, queuedAdvicePollEarliestElapsedMs(0))
        assertEquals(15_000L, queuedAdvicePollEarliestElapsedMs(1))
        assertEquals(35_000L, queuedAdvicePollEarliestElapsedMs(2))
        assertEquals(75_000L, queuedAdvicePollEarliestElapsedMs(3))
        assertEquals(
            QueuedAdvicePollDecision.RETRY,
            queuedAdvicePollDecision(emptySuccessfulPull, runAttemptCount = 0),
        )
        assertEquals(
            QueuedAdvicePollDecision.RETRY,
            queuedAdvicePollDecision(emptySuccessfulPull, runAttemptCount = 2),
        )
        assertEquals(
            QueuedAdvicePollDecision.COMPLETE,
            queuedAdvicePollDecision(emptySuccessfulPull, runAttemptCount = 3),
        )
    }

    @Test
    fun historicalAdviceCannotEndANewQueuedPollEarly() {
        val historicalAdvice = emptySuccessfulPull.copy(adviceReceived = 1)

        assertEquals(
            QueuedAdvicePollDecision.RETRY,
            queuedAdvicePollDecision(historicalAdvice, runAttemptCount = 0),
        )
        assertEquals(
            QueuedAdvicePollDecision.COMPLETE,
            queuedAdvicePollDecision(historicalAdvice, runAttemptCount = 3),
        )
        assertTrue(advicePollSucceeded(historicalAdvice))
    }

    @Test
    fun eachRoomPendingRowKeepsItsOwnFiniteBackoffWindow() {
        val pending = PendingAnalysisEntity(eventId = "queued", enqueuedAt = 0L)

        assertEquals(5_000L, pending.nextAttemptAt)
        assertEquals(15_000L, pendingAnalysisNextAttemptAt(5_000L, completedAttempts = 1))
        assertEquals(35_000L, pendingAnalysisNextAttemptAt(15_000L, completedAttempts = 2))
        assertEquals(75_000L, pendingAnalysisNextAttemptAt(35_000L, completedAttempts = 3))
        assertEquals(4, MAX_PENDING_ANALYSIS_ATTEMPTS)
    }
}
