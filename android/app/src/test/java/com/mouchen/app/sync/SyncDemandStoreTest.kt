package com.mouchen.app.sync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SyncDemandStoreTest {
    @Test
    fun repeatedEventsCoalesceIntoOneScheduledWorker() {
        val first = requestSyncDemand(SyncDemandState(generation = 0, active = false))
        val second = requestSyncDemand(first.state)
        val third = requestSyncDemand(second.state)

        assertTrue(first.shouldSchedule)
        assertFalse(second.shouldSchedule)
        assertFalse(third.shouldSchedule)
        assertEquals(3L, third.state.generation)
        assertTrue(third.state.active)
    }

    @Test
    fun eventArrivingDuringWorkerPreventsDemandFromBeingCleared() {
        val scheduled = requestSyncDemand(SyncDemandState(generation = 0, active = false))
        val arrivedDuringRun = requestSyncDemand(scheduled.state)

        val staleCompletion = completeSyncDemand(arrivedDuringRun.state, scheduled.state.generation)
        assertFalse(staleCompletion.completed)
        assertTrue(staleCompletion.state.active)

        val currentCompletion = completeSyncDemand(staleCompletion.state, arrivedDuringRun.state.generation)
        assertTrue(currentCompletion.completed)
        assertFalse(currentCompletion.state.active)
    }

    @Test
    fun processRecoveryReschedulesAStaleActiveDemand() {
        val stale = SyncDemandState(generation = 41, active = true)

        val recovered = recoverSyncDemand(stale)

        assertTrue(recovered.shouldSchedule)
        assertTrue(recovered.state.active)
        assertEquals(42L, recovered.state.generation)
    }

    @Test
    fun failedCommitIsReturnedAndCompletionRequestsRetry() {
        val persistence = FakeSyncDemandPersistence(commitSucceeds = false)
        val store = SyncDemandStore(persistence)

        val lease = store.request("notification")

        assertTrue(lease.shouldSchedule)
        assertFalse(lease.persisted)
        assertFalse(store.completeIfUnchanged(lease.generation))
    }

    @Test
    fun rollbackAfterWorkEnqueueFailureClearsTombstone() {
        val persistence = FakeSyncDemandPersistence(commitSucceeds = true)
        val store = SyncDemandStore(persistence)
        val lease = store.request("notification")

        assertTrue(lease.shouldSchedule)
        assertTrue(store.rollbackScheduledRequest(lease.generation))
        assertFalse(store.snapshot().active)
    }

    @Test
    fun rollbackCannotClearAConcurrentNewerDemand() {
        val persistence = FakeSyncDemandPersistence(commitSucceeds = true)
        val store = SyncDemandStore(persistence)
        val first = store.request("first")
        val newer = store.request("newer")

        assertFalse(store.rollbackScheduledRequest(first.generation))
        assertTrue(store.snapshot().active)
        assertEquals(newer.generation, store.snapshot().generation)
    }

    private class FakeSyncDemandPersistence(
        private val commitSucceeds: Boolean,
    ) : SyncDemandPersistence {
        private var snapshot = SyncDemandSnapshot(generation = 0, active = false, trigger = "event")

        override fun read(): SyncDemandSnapshot = snapshot

        override fun write(snapshot: SyncDemandSnapshot): Boolean {
            // SharedPreferences updates its in-memory view even when the disk commit reports false.
            this.snapshot = snapshot
            return commitSucceeds
        }
    }
}
