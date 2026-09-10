package com.mouchen.app.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PendingAnalysisDaoTest {
    private lateinit var database: MouchenDatabase
    private lateinit var dao: MouchenDao

    @Before
    fun setUp() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        database = Room.inMemoryDatabaseBuilder(context, MouchenDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        dao = database.dao()
    }

    @After
    fun tearDown() {
        database.close()
    }

    @Test
    fun queuedCommitIsAtomicAndOnlyEmptyTransitionNeedsANewWake() = runBlocking {
        insertEvent(FIRST_EVENT)
        insertEvent(SECOND_EVENT)

        assertTrue(dao.markEventSyncedWithPendingAnalysis(pending(FIRST_EVENT)))
        assertFalse(dao.markEventSyncedWithPendingAnalysis(pending(SECOND_EVENT)))

        assertTrue(dao.event(FIRST_EVENT)?.synced == true)
        assertTrue(dao.event(SECOND_EVENT)?.synced == true)
        assertEquals(2, dao.pendingAnalysisCount())

        insertEvent(DIRECT_EVENT)
        dao.insertPendingAnalysisIfAbsent(pending(DIRECT_EVENT))
        dao.markEventSyncedWithoutPendingAnalysis(DIRECT_EVENT)

        assertNull(dao.pendingAnalysis(DIRECT_EVENT))
        assertEquals(2, dao.pendingAnalysisCount())
    }

    @Test
    fun eachPendingEventGetsFourFiniteAttemptsWithIndependentBackoff() = runBlocking {
        dao.insertPendingAnalysisIfAbsent(pending(FIRST_EVENT))

        advance(FIRST_EVENT, attemptedAt = 5_000L, expectedAttempts = 1, expectedNext = 15_000L)
        advance(FIRST_EVENT, attemptedAt = 15_000L, expectedAttempts = 2, expectedNext = 35_000L)
        advance(FIRST_EVENT, attemptedAt = 35_000L, expectedAttempts = 3, expectedNext = 75_000L)
        val fourth = requireNotNull(dao.pendingAnalysis(FIRST_EVENT))
        assertNull(dao.finishPendingAnalysisPoll(listOf(fourth), attemptedAt = 75_000L))
        assertNull(dao.pendingAnalysis(FIRST_EVENT))
    }

    private suspend fun advance(
        eventId: String,
        attemptedAt: Long,
        expectedAttempts: Int,
        expectedNext: Long,
    ) {
        val observed = requireNotNull(dao.pendingAnalysis(eventId))
        assertEquals(expectedNext, dao.finishPendingAnalysisPoll(listOf(observed), attemptedAt))
        val current = requireNotNull(dao.pendingAnalysis(eventId))
        assertEquals(expectedAttempts, current.attempts)
        assertEquals(expectedNext, current.nextAttemptAt)
    }

    private suspend fun insertEvent(eventId: String) {
        dao.insertEvent(
            LocalEventEntity(
                id = eventId,
                source = "android-test",
                type = "notification.posted",
                payloadJson = "{}",
            ),
        )
    }

    private fun pending(eventId: String) = PendingAnalysisEntity(
        eventId = eventId,
        enqueuedAt = 0L,
    )

    private companion object {
        const val FIRST_EVENT = "pending-first"
        const val SECOND_EVENT = "pending-second"
        const val DIRECT_EVENT = "pending-direct"
    }
}
