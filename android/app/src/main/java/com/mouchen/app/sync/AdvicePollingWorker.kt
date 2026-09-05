package com.mouchen.app.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.mouchen.app.data.MAX_PENDING_ANALYSIS_ATTEMPTS
import com.mouchen.app.data.MouchenDatabase
import java.util.concurrent.TimeUnit

/**
 * Pulls advice independently so poison uploads cannot starve already-generated advice. Queued
 * event identity and retry state live in Room; WorkManager is deliberately only the wake-up path.
 */
class AdvicePollingWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val fence = captureAuthFence(applicationContext) ?: return Result.success()
        if (!fence.isCurrent(applicationContext)) return Result.success()
        val queuedEventPoll = inputData.getBoolean(INPUT_QUEUED_EVENT_POLL, false)
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse { return Result.retry() }
        val connectionStore = BackendConnectionStore(applicationContext)
        if (!connectionStore.load().enabled) return Result.success()
        val client = EventSyncClient(applicationContext, dao, connectionStore)
        if (!queuedEventPoll) {
            return if (advicePollSucceeded(client.pullAdvice(POLL_LIMIT))) {
                Result.success()
            } else {
                Result.retry()
            }
        }

        val attemptedAt = System.currentTimeMillis()
        val due = dao.duePendingAnalysis(attemptedAt, POLL_LIMIT)
        if (due.isEmpty()) {
            return scheduleNextOrFinish(dao.earliestPendingAnalysisAttemptAt(), attemptedAt)
        }

        // A pull can contain old rows, so its size cannot prove that any specific queued event is
        // finished. Every observed pending event advances one bounded attempt regardless of size.
        client.pullAdvice(POLL_LIMIT)
        if (!fence.isCurrent(applicationContext)) return Result.success()
        val nextAttemptAt = dao.finishPendingAnalysisPoll(due, attemptedAt)
        return scheduleNextOrFinish(nextAttemptAt, System.currentTimeMillis())
    }

    private fun scheduleNextOrFinish(nextAttemptAt: Long?, now: Long): Result {
        if (nextAttemptAt == null) return Result.success()
        val delay = (nextAttemptAt - now).coerceAtLeast(0L)
        return if (
            enqueueQueuedAdviceRequest(
                applicationContext,
                ExistingWorkPolicy.APPEND,
                delay,
            )
        ) {
            Result.success()
        } else {
            Result.retry()
        }
    }

    companion object {
        const val POLL_LIMIT = 200
        private const val INPUT_QUEUED_EVENT_POLL = "queued_event_poll"
        private const val QUEUED_EVENT_WORK = "mouchen-queued-event-advice-poll"
        private val QUEUED_WORK_LOCK = Any()

        /** Called only after an empty -> non-empty Room transition committed with markEventSynced. */
        internal fun enqueueAfterQueuedEvent(context: Context): Boolean =
            enqueueQueuedAdviceRequest(
                context.applicationContext,
                // Preserve an active chain's delay, but replace a terminal cancelled/failed
                // chain so a newly durable Room row cannot inherit that terminal state.
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                QUEUED_EVENT_INITIAL_DELAY_MS,
            )

        /** Restores a durable Room pending row if WorkManager was pruned or the process died. */
        internal suspend fun recoverQueuedEventPolling(context: Context): Boolean {
            val appContext = context.applicationContext
            val earliestResult = runCatching {
                MouchenDatabase.get(appContext).dao().earliestPendingAnalysisAttemptAt()
            }
            if (earliestResult.isFailure) return false
            val earliest = earliestResult.getOrNull() ?: return true
            return enqueueQueuedAdviceRequest(
                appContext,
                ExistingWorkPolicy.KEEP,
                (earliest - System.currentTimeMillis()).coerceAtLeast(0L),
            )
        }

        internal fun cancelQueuedEventPolling(context: Context) {
            synchronized(QUEUED_WORK_LOCK) {
                runCatching {
                    WorkManager.getInstance(context.applicationContext)
                        .cancelUniqueWork(QUEUED_EVENT_WORK)
                        .result
                        .get(WORK_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
                }
            }
        }

        internal fun queuedAdviceWorkName(): String = QUEUED_EVENT_WORK

        private fun enqueueQueuedAdviceRequest(
            context: Context,
            policy: ExistingWorkPolicy,
            delayMs: Long,
        ): Boolean = synchronized(QUEUED_WORK_LOCK) {
            runCatching {
                val request = OneTimeWorkRequestBuilder<AdvicePollingWorker>()
                    .setConstraints(
                        Constraints.Builder()
                            .setRequiredNetworkType(NetworkType.CONNECTED)
                            .build(),
                    )
                    .setInputData(workDataOf(INPUT_QUEUED_EVENT_POLL to true))
                    .setInitialDelay(delayMs.coerceAtMost(MAX_QUEUED_POLL_DELAY_MS), TimeUnit.MILLISECONDS)
                    .setBackoffCriteria(
                        BackoffPolicy.EXPONENTIAL,
                        QUEUED_EVENT_RETRY_DELAY_MS,
                        TimeUnit.MILLISECONDS,
                    )
                    .build()
                WorkManager.getInstance(context.applicationContext)
                    .enqueueUniqueWork(QUEUED_EVENT_WORK, policy, request)
                    .result
                    .get(WORK_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
                true
            }.getOrDefault(false)
        }
    }
}

internal fun advicePollSucceeded(report: SyncReport): Boolean =
    report.adviceAttempted == 1 && report.adviceSynced == 1 && report.adviceFailed == 0

internal enum class QueuedAdvicePollDecision {
    RETRY,
    COMPLETE,
}

internal fun queuedAdvicePollDecision(
    @Suppress("UNUSED_PARAMETER") report: SyncReport,
    runAttemptCount: Int,
): QueuedAdvicePollDecision =
    if (runAttemptCount < MAX_PENDING_ANALYSIS_ATTEMPTS - 1) {
        QueuedAdvicePollDecision.RETRY
    } else {
        QueuedAdvicePollDecision.COMPLETE
    }

internal fun queuedAdvicePollEarliestElapsedMs(runAttemptCount: Int): Long {
    var elapsed = QUEUED_EVENT_INITIAL_DELAY_MS
    repeat(runAttemptCount.coerceIn(0, MAX_PENDING_ANALYSIS_ATTEMPTS - 1)) { retryIndex ->
        elapsed += QUEUED_EVENT_RETRY_DELAY_MS shl retryIndex
    }
    return elapsed
}

private const val QUEUED_EVENT_INITIAL_DELAY_MS = 5_000L
private const val QUEUED_EVENT_RETRY_DELAY_MS = 10_000L
private const val MAX_QUEUED_POLL_DELAY_MS = 6L * 60 * 60 * 1000
private const val WORK_OPERATION_TIMEOUT_SECONDS = 5L
