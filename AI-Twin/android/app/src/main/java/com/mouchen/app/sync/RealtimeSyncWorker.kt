package com.mouchen.app.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.status.PipelineStatusStore
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException

class RealtimeSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val inputTrigger = inputData.getString(INPUT_TRIGGER).orEmpty().ifBlank { "event" }
        val demandStore = SyncDemandStore(applicationContext)
        var demand = demandStore.snapshot()
        val trigger = demand.trigger.ifBlank { inputTrigger }
        val status = PipelineStatusStore(applicationContext)
        val queuedEpoch = inputData.getLong(INPUT_AUTH_EPOCH, -1L)
        val queuedUser = inputData.getString(INPUT_AUTH_USER).orEmpty()
        val queuedOrigin = inputData.getString(INPUT_AUTH_ORIGIN).orEmpty()
        val queuedFence = if (queuedEpoch > 0L && queuedUser.isNotBlank() && queuedOrigin.isNotBlank()) {
            AuthFence(queuedEpoch, queuedUser, queuedOrigin)
        } else {
            captureAuthFence(applicationContext)
        }
        if (queuedFence == null || !queuedFence.isCurrent(applicationContext)) return Result.success()
        status.markSyncRunning(trigger)
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse {
                status.markSyncFailed("database_unavailable")
                return Result.retry()
        }
        val connectionStore = BackendConnectionStore(applicationContext)
        val storedConnection = connectionStore.load()
        val connection = authenticatedConnection(applicationContext, storedConnection)
        if (!storedConnection.enabled) {
            demandStore.release()
            status.markSyncDisabled()
            return Result.success()
        }
        val fence = captureAuthFence(applicationContext)
        if (connection == null || fence == null || fence != queuedFence || !connection.enabled) {
            demandStore.release()
            status.markSyncAuthRequired()
            return Result.success()
        }
        if (!AdvicePollingWorker.recoverQueuedEventPolling(applicationContext)) {
            status.markSyncFailed("advice_poll_recovery_failed")
            return Result.retry()
        }
        val client = EventSyncClient(applicationContext, dao, connectionStore)
        var attempted = 0
        var succeeded = 0
        var failed = 0
        return try {
            var demandCycles = 0
            var advicePullFailedAfterDelivery = false
            var goalPullFailedAfterDelivery = false
            while (true) {
                if (!fence.isCurrent(applicationContext)) return Result.success()
                val cycle = executeSyncCycle(
                    batchSize = BATCH_SIZE,
                    maxBatches = MAX_BATCHES,
                    sync = client::sync,
                    pullGoals = { client.pullGoals() },
                    runGoalReview = client::runGoalReview,
                    pullAdvice = client::pullAdvice,
                    scheduleQueuedAdvicePull = {
                        AdvicePollingWorker.enqueueAfterQueuedEvent(applicationContext)
                    },
                )
                if (!fence.isCurrent(applicationContext)) return Result.success()
                attempted += cycle.attempted
                succeeded += cycle.succeeded
                failed += cycle.failed
                if (cycle.goalPullFailed) {
                    goalPullFailedAfterDelivery = true
                    // Goal restoration has its own retry chain and must never put new phone
                    // evidence behind WorkManager's backoff for this realtime worker.
                    GoalRefreshWorker.enqueue(applicationContext)
                }
                when (cycle.failure) {
                    SyncCycleFailure.DELIVERY -> {
                        status.markSyncFinished(attempted, succeeded, failed, "delivery_failed")
                        SessionHealthWorker.enqueueNow(applicationContext, replace = true)
                        return Result.retry()
                    }
                    SyncCycleFailure.BATCH_LIMIT -> {
                        // Keep ownership of the active demand. WorkManager retries this same
                        // request; arrivals only advance the generation.
                        status.markSyncContinuation(attempted, succeeded, "batch_limit_reached")
                        return Result.retry()
                    }
                    SyncCycleFailure.GOAL_REVIEW -> {
                        status.markSyncFinished(attempted, succeeded, failed, "goal_review_failed")
                        return Result.retry()
                    }
                    SyncCycleFailure.ADVICE_PULL -> {
                        // Advice GET is a secondary reconciliation channel. The event POST already
                        // returned any immediate advice, so a failed GET must not retain demand or
                        // put future event uploads behind WorkManager backoff.
                        advicePullFailedAfterDelivery = true
                    }
                    SyncCycleFailure.ADVICE_POLL_SCHEDULE -> {
                        status.markSyncFinished(attempted, succeeded, failed, "advice_poll_schedule_failed")
                        return Result.retry()
                    }
                    SyncCycleFailure.NONE -> Unit
                }

                if (demandStore.completeIfUnchanged(demand.generation)) {
                    status.markSyncFinished(
                        attempted,
                        succeeded,
                        failed,
                        auxiliaryPullStatus(
                            advicePullFailed = advicePullFailedAfterDelivery,
                            goalPullFailed = goalPullFailedAfterDelivery,
                        ),
                    )
                    return Result.success()
                }

                demandCycles += 1
                if (demandCycles >= MAX_DEMAND_CYCLES) {
                    // A bounded successor prevents a hot event stream from monopolizing one worker.
                    // The active flag stays set, so producers cannot append further requests.
                    demand = demandStore.snapshot()
                    status.markSyncContinuation(attempted, succeeded, "new_events_arrived")
                    enqueueContinuation(applicationContext, demand.trigger)
                    return Result.success()
                }
                demand = demandStore.snapshot()
            }
            @Suppress("UNREACHABLE_CODE")
            Result.success()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            if (fence.isCurrent(applicationContext)) status.markSyncFailed("unexpected_sync_error")
            Result.retry()
        }
    }

    companion object {
        private const val UNIQUE_WORK = "mouchen-realtime-sync"
        private const val BATCH_SIZE = 200
        private const val MAX_BATCHES = 5
        private const val MAX_DEMAND_CYCLES = 3
        private const val INPUT_TRIGGER = "trigger"
        private const val INPUT_AUTH_EPOCH = "auth_epoch"
        private const val INPUT_AUTH_USER = "auth_user"
        private const val INPUT_AUTH_ORIGIN = "auth_origin"

        fun enqueue(context: Context, trigger: String = "event"): Boolean {
            return enqueue(context, trigger, forceImmediate = false)
        }

        /** Routes a durable event to the urgent one-event path or the normal coalesced queue. */
        suspend fun enqueueForEvent(context: Context, event: LocalEventEntity): Boolean {
            val category = UrgencyClassifier.classify(event)
                ?: return enqueue(context, "event:${event.source}")
            return UrgentEventSyncWorker.enqueue(context, event.id, category) ||
                enqueue(context, "urgent_enqueue_fallback:${event.source}")
        }

        /**
         * Replaces a pending backoff/retry with fresh work. Use only for an explicit owner action
         * (for example saving corrected credentials or tapping "scan now"). Ordinary sensor
         * events still coalesce behind the active worker and cannot create an unbounded chain.
         */
        fun enqueueImmediate(context: Context, trigger: String): Boolean {
            return enqueue(context, trigger, forceImmediate = true)
        }

        private fun enqueue(context: Context, trigger: String, forceImmediate: Boolean): Boolean {
            val appContext = context.applicationContext
            val demandStore = SyncDemandStore(appContext)
            val lease = demandStore.request(trigger)
            if (!shouldScheduleSyncRequest(lease, forceImmediate)) return true
            val status = PipelineStatusStore(appContext)
            status.markSyncQueued(if (lease.persisted) trigger else "$trigger:state_unpersisted")
            return if (
                confirmedWorkEnqueue {
                    enqueueRequest(
                        appContext,
                        trigger,
                        if (forceImmediate) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.APPEND_OR_REPLACE,
                    )
                }
            ) {
                true
            } else {
                recoverFromEnqueueFailure(appContext, demandStore, lease, trigger, status)
            }
        }

        fun recover(context: Context, trigger: String = "process_start"): Boolean {
            val appContext = context.applicationContext
            val demandStore = SyncDemandStore(appContext)
            val lease = demandStore.recover(trigger)
            val status = PipelineStatusStore(appContext)
            status.markSyncQueued(if (lease.persisted) trigger else "$trigger:state_unpersisted")
            return if (
                confirmedWorkEnqueue {
                    enqueueRequest(appContext, trigger, ExistingWorkPolicy.KEEP)
                }
            ) {
                true
            } else {
                recoverFromEnqueueFailure(appContext, demandStore, lease, trigger, status)
            }
        }

        private fun recoverFromEnqueueFailure(
            appContext: Context,
            demandStore: SyncDemandStore,
            lease: SyncDemandLease,
            trigger: String,
            status: PipelineStatusStore,
        ): Boolean {
            if (demandStore.rollbackScheduledRequest(lease.generation)) {
                status.markSyncFailed("work_enqueue_failed")
                return false
            }
            // A newer event advanced the generation during the failed enqueue. Preserve that
            // active demand and give it one direct replacement work instead of clearing it.
            return if (
                confirmedWorkEnqueue {
                    enqueueRequest(appContext, trigger, ExistingWorkPolicy.APPEND_OR_REPLACE)
                }
            ) {
                true
            } else {
                status.markSyncFailed("work_enqueue_failed_with_newer_demand")
                false
            }
        }

        private fun enqueueContinuation(context: Context, trigger: String) {
            enqueueRequest(
                context.applicationContext,
                trigger.ifBlank { "continuation" },
                ExistingWorkPolicy.APPEND_OR_REPLACE,
            )
        }

        private fun enqueueRequest(appContext: Context, trigger: String, policy: ExistingWorkPolicy) {
            val fence = captureAuthFence(appContext)
            val constraints = Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build()
            val request = OneTimeWorkRequestBuilder<RealtimeSyncWorker>()
                .setConstraints(constraints)
                .setBackoffCriteria(androidx.work.BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setInputData(
                    workDataOf(
                        INPUT_TRIGGER to trigger,
                        INPUT_AUTH_EPOCH to (fence?.epoch ?: -1L),
                        INPUT_AUTH_USER to fence?.userId.orEmpty(),
                        INPUT_AUTH_ORIGIN to fence?.origin.orEmpty(),
                    ),
                )
                .build()
            WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_WORK,
                policy,
                request,
            ).result.get(WORK_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        }

        private const val WORK_OPERATION_TIMEOUT_SECONDS = 5L
    }
}

internal fun isSyncBatchDrained(report: SyncReport, batchSize: Int): Boolean =
    report.attempted < batchSize && report.goalsAttempted < batchSize

internal fun SyncReport.totalAttempted(): Int =
    attempted + goalsAttempted + goalPullAttempted + adviceAttempted

internal fun SyncReport.totalSucceeded(): Int = synced + goalsSynced + goalPullSynced + adviceSynced

internal fun SyncReport.totalFailed(): Int = failed + goalsFailed + goalPullFailed + adviceFailed

internal fun hasSyncFailures(report: SyncReport): Boolean = report.totalFailed() > 0

internal fun shouldScheduleSyncRequest(lease: SyncDemandLease, forceImmediate: Boolean): Boolean =
    lease.shouldSchedule || forceImmediate

/** Converts WorkManager's asynchronous Operation result into the Boolean scheduling contract. */
internal fun confirmedWorkEnqueue(enqueueAndAwait: () -> Unit): Boolean = try {
    enqueueAndAwait()
    true
} catch (_: Exception) {
    false
}

internal enum class SyncCycleFailure {
    NONE,
    DELIVERY,
    BATCH_LIMIT,
    GOAL_REVIEW,
    ADVICE_PULL,
    ADVICE_POLL_SCHEDULE,
}

internal data class SyncCycleOutcome(
    val attempted: Int,
    val succeeded: Int,
    val failed: Int,
    val failure: SyncCycleFailure,
    val goalPullFailed: Boolean = false,
)

/** Runs one demand generation in the safe order: upload, restore goals, review, then advice. */
internal suspend fun executeSyncCycle(
    batchSize: Int,
    maxBatches: Int,
    sync: suspend (Int) -> SyncReport,
    pullGoals: suspend () -> SyncReport = { SyncReport(0, 0, 0) },
    runGoalReview: suspend () -> Boolean,
    pullAdvice: suspend (Int) -> SyncReport,
    scheduleQueuedAdvicePull: suspend () -> Boolean = { true },
): SyncCycleOutcome {
    var attempted = 0
    var succeeded = 0
    var failed = 0
    var drained = false
    var deliveryFailed = false
    var queuedAdvicePullRequired = false
    for (batch in 0 until maxBatches) {
        val report = sync(batchSize)
        attempted += report.totalAttempted()
        succeeded += report.totalSucceeded()
        failed += report.totalFailed()
        deliveryFailed = deliveryFailed || hasSyncFailures(report)
        queuedAdvicePullRequired = queuedAdvicePullRequired || report.advicePullRequired
        if (isSyncBatchDrained(report, batchSize)) {
            drained = true
            break
        }
    }
    val goals = pullGoals()
    attempted += goals.totalAttempted()
    succeeded += goals.totalSucceeded()
    failed += goals.totalFailed()

    attempted += 1
    val reviewSucceeded = runGoalReview()
    if (!reviewSucceeded) {
        failed += 1
    } else {
        succeeded += 1
    }

    val advice = pullAdvice(batchSize)
    attempted += advice.totalAttempted()
    succeeded += advice.totalSucceeded()
    failed += advice.totalFailed()
    val queuedAdvicePollScheduled = !queuedAdvicePullRequired || scheduleQueuedAdvicePull()
    if (!queuedAdvicePollScheduled) {
        attempted += 1
        failed += 1
    }
    return SyncCycleOutcome(
        attempted,
        succeeded,
        failed,
        when {
            !drained -> SyncCycleFailure.BATCH_LIMIT
            deliveryFailed -> SyncCycleFailure.DELIVERY
            !reviewSucceeded -> SyncCycleFailure.GOAL_REVIEW
            !queuedAdvicePollScheduled -> SyncCycleFailure.ADVICE_POLL_SCHEDULE
            hasSyncFailures(advice) -> SyncCycleFailure.ADVICE_PULL
            else -> SyncCycleFailure.NONE
        },
        goalPullFailed = hasSyncFailures(goals),
    )
}

internal fun auxiliaryPullStatus(advicePullFailed: Boolean, goalPullFailed: Boolean): String = when {
    advicePullFailed && goalPullFailed -> "advice_and_goal_pull_failed_after_delivery"
    advicePullFailed -> "advice_pull_failed_after_delivery"
    goalPullFailed -> "goal_pull_failed_after_delivery"
    else -> ""
}
