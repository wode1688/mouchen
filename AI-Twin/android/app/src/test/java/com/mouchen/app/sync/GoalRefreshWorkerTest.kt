package com.mouchen.app.sync

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class GoalRefreshWorkerTest {
    @Test
    fun goalRefreshHasAnIndependentSuccessContract() {
        assertTrue(
            goalPullSucceeded(
                SyncReport(0, 0, 0, goalPullAttempted = 1, goalPullSynced = 1),
            ),
        )
        assertFalse(
            goalPullSucceeded(
                SyncReport(0, 0, 0, goalPullAttempted = 1, goalPullFailed = 1),
            ),
        )
    }
}
