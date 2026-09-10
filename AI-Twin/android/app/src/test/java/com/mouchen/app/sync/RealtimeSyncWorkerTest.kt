package com.mouchen.app.sync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.runBlocking

class RealtimeSyncWorkerTest {
    @Test
    fun fullGoalBatchOverHundredIsNotMistakenForAnEmptyQueue() {
        val report = SyncReport(
            attempted = 0,
            synced = 0,
            failed = 0,
            goalsAttempted = 200,
            goalsSynced = 200,
        )

        assertFalse(isSyncBatchDrained(report, batchSize = 200))
        assertTrue(isSyncBatchDrained(report.copy(goalsAttempted = 199), batchSize = 200))
    }

    @Test
    fun advicePullFailureIsCountedForPipelineVisibility() {
        val report = SyncReport(
            attempted = 0,
            synced = 0,
            failed = 0,
            adviceAttempted = 1,
            adviceFailed = 1,
        )

        assertEquals(1, report.totalAttempted())
        assertEquals(0, report.totalSucceeded())
        assertEquals(1, report.totalFailed())
        assertTrue(hasSyncFailures(report))
    }

    @Test
    fun goalPullIsCountedForPipelineVisibility() {
        val success = SyncReport(
            attempted = 0,
            synced = 0,
            failed = 0,
            goalPullAttempted = 1,
            goalPullSynced = 1,
            goalsReceived = 2,
            goalsApplied = 2,
        )
        val failure = success.copy(goalPullSynced = 0, goalPullFailed = 1)

        assertEquals(1, success.totalAttempted())
        assertEquals(1, success.totalSucceeded())
        assertEquals(0, success.totalFailed())
        assertEquals(1, failure.totalFailed())
        assertTrue(hasSyncFailures(failure))
    }

    @Test
    fun cycleUploadsThenReviewsThenPullsAdvice() = runBlocking {
        val calls = mutableListOf<String>()

        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 5,
            sync = {
                calls += "upload"
                SyncReport(attempted = 1, synced = 1, failed = 0)
            },
            pullGoals = {
                calls += "goals"
                SyncReport(0, 0, 0, goalPullAttempted = 1, goalPullSynced = 1)
            },
            runGoalReview = {
                calls += "review"
                true
            },
            pullAdvice = {
                calls += "advice"
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
        )

        assertEquals(listOf("upload", "goals", "review", "advice"), calls)
        assertEquals(SyncCycleFailure.NONE, outcome.failure)
    }

    @Test
    fun goalPullFailureIsAuxiliaryAndDoesNotBackoffRealtimeEvidence() = runBlocking {
        val calls = mutableListOf<String>()

        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 5,
            sync = {
                calls += "upload"
                SyncReport(attempted = 1, synced = 1, failed = 0)
            },
            pullGoals = {
                calls += "goals"
                SyncReport(0, 0, 0, goalPullAttempted = 1, goalPullFailed = 1)
            },
            runGoalReview = {
                calls += "review"
                true
            },
            pullAdvice = {
                calls += "advice"
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
        )

        assertEquals(listOf("upload", "goals", "review", "advice"), calls)
        assertEquals(SyncCycleFailure.NONE, outcome.failure)
        assertTrue(outcome.goalPullFailed)
        assertEquals(3, outcome.succeeded)
        assertEquals(1, outcome.failed)
    }

    @Test
    fun auxiliaryPullFailuresRemainVisibleWithoutChangingRealtimeResult() {
        assertEquals("", auxiliaryPullStatus(advicePullFailed = false, goalPullFailed = false))
        assertEquals(
            "goal_pull_failed_after_delivery",
            auxiliaryPullStatus(advicePullFailed = false, goalPullFailed = true),
        )
        assertEquals(
            "advice_and_goal_pull_failed_after_delivery",
            auxiliaryPullStatus(advicePullFailed = true, goalPullFailed = true),
        )
    }

    @Test
    fun advicePullFailureHappensOnlyAfterEventUploadAndGoalReview() = runBlocking {
        val calls = mutableListOf<String>()

        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 5,
            sync = {
                calls += "upload"
                SyncReport(attempted = 1, synced = 1, failed = 0)
            },
            runGoalReview = {
                calls += "review"
                true
            },
            pullAdvice = {
                calls += "advice"
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceFailed = 1)
            },
        )

        assertEquals(listOf("upload", "review", "advice"), calls)
        assertEquals(SyncCycleFailure.ADVICE_PULL, outcome.failure)
        assertEquals(2, outcome.succeeded)
        assertEquals(1, outcome.failed)
    }

    @Test
    fun poisonDeliveryCannotStarveReviewOrAdvicePull() = runBlocking {
        val calls = mutableListOf<String>()

        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 5,
            sync = {
                calls += "upload"
                SyncReport(attempted = 1, synced = 0, failed = 1)
            },
            runGoalReview = {
                calls += "review"
                true
            },
            pullAdvice = {
                calls += "advice"
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
        )

        assertEquals(listOf("upload", "review", "advice"), calls)
        assertEquals(SyncCycleFailure.DELIVERY, outcome.failure)
    }

    @Test
    fun fullBatchLimitCannotStarveAdvicePull() = runBlocking {
        val calls = mutableListOf<String>()

        val outcome = executeSyncCycle(
            batchSize = 2,
            maxBatches = 1,
            sync = {
                calls += "upload"
                SyncReport(attempted = 2, synced = 2, failed = 0)
            },
            runGoalReview = {
                calls += "review"
                true
            },
            pullAdvice = {
                calls += "advice"
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
        )

        assertEquals(listOf("upload", "review", "advice"), calls)
        assertEquals(SyncCycleFailure.BATCH_LIMIT, outcome.failure)
    }

    @Test
    fun advicePollingHasAnIndependentSuccessContract() {
        assertTrue(
            advicePollSucceeded(
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1),
            ),
        )
        assertFalse(
            advicePollSucceeded(
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceFailed = 1),
            ),
        )
    }

    @Test
    fun ordinaryBatchAggregatesQueuedResponsesAndSchedulesOnePersistentPoll() = runBlocking {
        var schedules = 0

        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 5,
            sync = {
                SyncReport(
                    attempted = 2,
                    synced = 2,
                    failed = 0,
                    advicePullRequired = true,
                )
            },
            runGoalReview = { true },
            pullAdvice = {
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
            scheduleQueuedAdvicePull = {
                schedules += 1
                true
            },
        )

        assertEquals(1, schedules)
        assertEquals(SyncCycleFailure.NONE, outcome.failure)
    }

    @Test
    fun failedQueuedPollSchedulingRemainsVisibleForWorkerRetry() = runBlocking {
        val outcome = executeSyncCycle(
            batchSize = 200,
            maxBatches = 1,
            sync = {
                SyncReport(
                    attempted = 1,
                    synced = 1,
                    failed = 0,
                    advicePullRequired = true,
                )
            },
            runGoalReview = { true },
            pullAdvice = {
                SyncReport(0, 0, 0, adviceAttempted = 1, adviceSynced = 1)
            },
            scheduleQueuedAdvicePull = { false },
        )

        assertEquals(SyncCycleFailure.ADVICE_POLL_SCHEDULE, outcome.failure)
        assertTrue(outcome.failed > 0)
    }

    @Test
    fun explicitOwnerActionSchedulesEvenWhileOldRetryOwnsDemand() {
        val activeLease = SyncDemandLease(
            generation = 7,
            shouldSchedule = false,
            persisted = true,
        )

        assertFalse(shouldScheduleSyncRequest(activeLease, forceImmediate = false))
        assertTrue(shouldScheduleSyncRequest(activeLease, forceImmediate = true))
    }

    @Test
    fun asynchronousWorkEnqueueFailureIsObservable() {
        assertFalse(
            confirmedWorkEnqueue {
                throw IllegalStateException("injected WorkManager Operation failure")
            },
        )
        assertTrue(confirmedWorkEnqueue { Unit })
    }
}
