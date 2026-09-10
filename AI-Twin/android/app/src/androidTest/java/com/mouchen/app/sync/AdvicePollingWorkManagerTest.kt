package com.mouchen.app.sync

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.data.PendingAnalysisEntity
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Verifies that repeated queued responses preserve one delayed WorkManager request. */
@RunWith(AndroidJUnit4::class)
class AdvicePollingWorkManagerTest {
    private lateinit var context: Context
    private lateinit var workManager: WorkManager
    private lateinit var dao: MouchenDao

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        workManager = WorkManager.getInstance(context)
        dao = MouchenDatabase.get(context).dao()
        clearQueuedPolling()
    }

    @After
    fun tearDown() {
        clearQueuedPolling()
    }

    @Test
    fun repeatedQueuedEventsKeepTheOriginalDelayedWork() = runBlocking {
        assertTrue(commitQueuedEvent(FIRST_EVENT_ID))
        assertTrue(AdvicePollingWorker.enqueueAfterQueuedEvent(context))
        val firstId = onlyUnfinishedQueuedWorkId()

        // The Room queue is already non-empty, so the second event is owned by the first wake-up.
        assertEquals(false, commitQueuedEvent(SECOND_EVENT_ID))
        val secondId = onlyUnfinishedQueuedWorkId()

        assertEquals(firstId, secondId)
    }

    @Test
    fun persistedPendingEventRecoversAfterMissingWork() = runBlocking {
        assertTrue(commitQueuedEvent(FIRST_EVENT_ID))

        assertTrue(AdvicePollingWorker.recoverQueuedEventPolling(context))

        onlyUnfinishedQueuedWorkId()
        Unit
    }

    private fun onlyUnfinishedQueuedWorkId(): UUID {
        val unfinished = workManager
            .getWorkInfosForUniqueWork(AdvicePollingWorker.queuedAdviceWorkName())
            .get(5, TimeUnit.SECONDS)
            .filterNot { it.state.isFinished }
        assertEquals(1, unfinished.size)
        assertTrue(
            unfinished.single().state == WorkInfo.State.ENQUEUED ||
                unfinished.single().state == WorkInfo.State.BLOCKED ||
                unfinished.single().state == WorkInfo.State.RUNNING,
        )
        return unfinished.single().id
    }

    private fun clearQueuedPolling() {
        AdvicePollingWorker.cancelQueuedEventPolling(context)
        workManager.cancelUniqueWork(AdvicePollingWorker.queuedAdviceWorkName())
            .result
            .get(5, TimeUnit.SECONDS)
        runBlocking {
            dao.deleteAllPendingAnalysis()
            dao.deleteEvent(FIRST_EVENT_ID)
            dao.deleteEvent(SECOND_EVENT_ID)
        }
    }

    private suspend fun commitQueuedEvent(eventId: String): Boolean {
        dao.insertEvent(
            LocalEventEntity(
                id = eventId,
                source = "android-test",
                type = "notification.posted",
                payloadJson = "{}",
            ),
        )
        return dao.markEventSyncedWithPendingAnalysis(PendingAnalysisEntity(eventId = eventId))
    }

    private companion object {
        const val FIRST_EVENT_ID = "wm-test-queued-one"
        const val SECOND_EVENT_ID = "wm-test-queued-two"
    }
}
