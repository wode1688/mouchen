package com.mouchen.app.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.mouchen.app.collectors.CollectionWorker
import com.mouchen.app.status.PipelineStatusStore
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SessionHealthWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val storedSession = AuthSessionStore(applicationContext).load()
        if (storedSession != null && LegacyMigrationRuntimeGate.requiresDecision(storedSession)) {
            return@withContext Result.success()
        }
        val status = PipelineStatusStore(applicationContext)
        when (val restored = AuthApiClient(applicationContext).restore()) {
            is AuthResult.Success -> {
                val fence = captureAuthFence(applicationContext)
                if (fence == null || fence.userId != restored.session.userId) return@withContext Result.success()
                status.markSessionHealthy(restored.session.userId)
                CollectionWorker.enqueueStartup(applicationContext, "session_healthy")
                RealtimeSyncWorker.recover(applicationContext, "session_healthy")
                Result.success()
            }
            is AuthResult.Failure -> {
                if (restored.unauthorized) {
                    invalidateAuthenticatedExecution(applicationContext)
                    AuthSessionStore(applicationContext).clear()
                    BackendConnectionStore(applicationContext).clearAuthentication()
                    SyncDemandStore(applicationContext).release()
                    status.markSyncAuthRequired(restored.message)
                    Result.success()
                } else {
                    status.markSessionCheckFailed(restored.message)
                    Result.retry()
                }
            }
        }
    }

    companion object {
        private const val PERIODIC_WORK = "mouchen-session-health-periodic"
        private const val IMMEDIATE_WORK = "mouchen-session-health-now"

        fun ensurePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<SessionHealthWorker>(15, TimeUnit.MINUTES)
                .setConstraints(networkConstraints())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context.applicationContext).enqueueUniquePeriodicWork(
                PERIODIC_WORK,
                ExistingPeriodicWorkPolicy.UPDATE,
                request,
            )
        }

        fun enqueueNow(context: Context, replace: Boolean = false) {
            val request = OneTimeWorkRequestBuilder<SessionHealthWorker>()
                .setConstraints(networkConstraints())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
                IMMEDIATE_WORK,
                if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
                request,
            )
        }

        private fun networkConstraints(): Constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
    }
}

internal fun hasAuthenticationCandidate(context: Context): Boolean {
    val connection = BackendConnectionStore(context).load()
    return connection.enabled && AuthSessionStore(context).credentialCandidate(connection) != null
}
