package com.mouchen.app.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class GoalDaoTest {
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
    fun serverGoalIsRestoredButCannotOverwriteAnUnsentLocalGoal() = runBlocking {
        val local = GoalEntity(
            id = "goal-1",
            domain = "work",
            title = "Local unsent title",
            quote = "Local unsent quote",
            version = 7,
            synced = false,
        )
        dao.insertGoal(local)

        val ignored = dao.mergeServerGoal(
            local.copy(
                title = "Server title",
                quote = "Server quote",
                version = 1,
                synced = true,
            ),
        )

        assertEquals(ServerGoalMergeResult.LOCAL_PENDING, ignored)
        assertEquals(local, dao.observeGoals().first().single())

        val restored = GoalEntity(
            id = "goal-2",
            domain = "health",
            title = "Server health goal",
            quote = "Sleep before midnight",
            version = 1,
            synced = true,
        )
        assertEquals(ServerGoalMergeResult.INSERTED, dao.mergeServerGoal(restored))
        assertEquals(2, dao.observeGoals().first().size)

        val canonical = local.copy(
            title = "Canonical server title",
            quote = "Canonical server quote",
            version = 1,
            synced = true,
            deliveryAttempts = 0,
            nextDeliveryAttemptAt = 0L,
        )
        dao.markGoalSynced(local.id)
        assertEquals(ServerGoalMergeResult.UPDATED, dao.mergeServerGoal(canonical))
        assertEquals(canonical, dao.goal(local.id))
    }
}
