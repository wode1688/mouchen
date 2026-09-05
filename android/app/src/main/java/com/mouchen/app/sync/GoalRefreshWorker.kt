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
import com.mouchen.app.data.MouchenDatabase
import java.util.concurrent.TimeUnit

/** Retries server goal restoration without ever owning or delaying realtime evidence demand. */
class GoalRefreshWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val fence = workerAuthFence() ?: return Result.success()
        if (!fence.isCurrent(applicationContext)) return Result.success()
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse { return Result.retry() }
        val connectionStore = BackendConnectionStore(applicationContext)
        if (!connectionStore.load().enabled) return Result.success()
        val report = EventSyncClient(applicationContext, dao, connectionStore).pullGoals()
        if (!fence.isCurrent(applicationContext)) return Result.success()
        return if (goalPullSucceeded(report)) Result.success() else Result.retry()
    }

    companion object {
        private const val UNIQUE_WORK = "mouchen-goal-refresh"
        private const val INITIAL_DELAY_SECONDS = 15L
        private const val RETRY_DELAY_SECONDS = 30L
        private const val WORK_OPERATION_TIMEOUT_SECONDS = 5L
        private const val INPUT_AUTH_EPOCH = "auth_epoch"
        private const val INPUT_AUTH_USER = "auth_user"
        private const val INPUT_AUTH_ORIGIN = "auth_origin"

        internal fun enqueue(context: Context): Boolean = runCatching {
            val fence = captureAuthFence(context.applicationContext)
            val request = OneTimeWorkRequestBuilder<GoalRefreshWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .setInitialDelay(INITIAL_DELAY_SECONDS, TimeUnit.SECONDS)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    RETRY_DELAY_SECONDS,
                    TimeUnit.SECONDS,
                )
                .setInputData(
                    workDataOf(
                        INPUT_AUTH_EPOCH to (fence?.epoch ?: -1L),
                        INPUT_AUTH_USER to fence?.userId.orEmpty(),
                        INPUT_AUTH_ORIGIN to fence?.origin.orEmpty(),
                    ),
                )
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(UNIQUE_WORK, ExistingWorkPolicy.KEEP, request)
                .result
                .get(WORK_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            true
        }.getOrDefault(false)
    }

    private fun workerAuthFence(): AuthFence? {
        val epoch = inputData.getLong(INPUT_AUTH_EPOCH, -1L)
        val user = inputData.getString(INPUT_AUTH_USER).orEmpty()
        val origin = inputData.getString(INPUT_AUTH_ORIGIN).orEmpty()
        return if (epoch > 0L && user.isNotBlank() && origin.isNotBlank()) {
            AuthFence(epoch, user, origin)
        } else captureAuthFence(applicationContext)
    }
}

internal fun goalPullSucceeded(report: SyncReport): Boolean =
    report.goalPullAttempted == 1 && report.goalPullSynced == 1 && report.goalPullFailed == 0
