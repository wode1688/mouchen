package com.mouchen.app.sync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.runBlocking

class UrgentEventSyncWorkerTest {
    @Test
    fun failedDeliveryReceivesTwoPriorityRetriesThenFallsBack() {
        assertEquals(
            UrgentFailurePlan.RetryAt(11_000L),
            urgentFailurePlan(attemptsBeforeFailure = 0, now = 1_000L),
        )
        assertEquals(
            UrgentFailurePlan.RetryAt(31_000L),
            urgentFailurePlan(attemptsBeforeFailure = 1, now = 1_000L),
        )
        assertEquals(
            UrgentFailurePlan.FallbackToNormal,
            urgentFailurePlan(attemptsBeforeFailure = 2, now = 1_000L),
        )
    }

    @Test
    fun allUrgentEventsShareOneBoundedWorkChain() {
        assertEquals("android:event-1", eventEvidenceRef("event-1"))
        assertEquals("mouchen-urgent-event-drain", UrgentEventSyncWorker.urgentWorkName())
    }

    @Test
    fun urgentClientUsesBoundedTimeoutsAndNeverRedirectsCredentials() {
        val client = buildUrgentEventHttpClient()

        assertEquals(8_000, client.connectTimeoutMillis)
        assertEquals(20_000, client.writeTimeoutMillis)
        assertEquals(12_000, client.readTimeoutMillis)
        assertFalse(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }

    @Test
    fun failedOrdinaryEnqueueRetainsUrgentRowForWorkerRetry() = runBlocking {
        var madeDue = false
        var urgentRowDeleted = false

        val handled = scheduleUrgentFallback(
            makeOrdinaryDeliveryDue = { madeDue = true },
            enqueueOrdinaryWorker = { false },
            deleteUrgentRow = { urgentRowDeleted = true },
        )

        assertFalse(handled)
        assertTrue(madeDue)
        assertFalse(urgentRowDeleted)
    }
}
