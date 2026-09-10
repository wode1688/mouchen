package com.mouchen.app.relay

import android.content.Context
import androidx.work.*
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException

class RelaySyncWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        if (RelaySettingsStore(applicationContext).load()?.enabled != true) return Result.success()
        return try {
            RelaySync(applicationContext).sync()
            Result.success()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            Result.retry()
        }
    }
    companion object {
        private const val IMMEDIATE = "mouchen-relay-immediate"
        private const val PERIODIC = "mouchen-relay-periodic"
        private fun constraints() = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        fun enqueue(context: Context) {
            if (RelaySettingsStore(context).load()?.enabled != true) return
            WorkManager.getInstance(context).enqueueUniqueWork(IMMEDIATE, ExistingWorkPolicy.KEEP,
                OneTimeWorkRequestBuilder<RelaySyncWorker>().setConstraints(constraints()).build())
        }
        fun configure(context: Context) {
            val manager = WorkManager.getInstance(context)
            if (RelaySettingsStore(context).load()?.enabled == true) {
                manager.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP,
                    PeriodicWorkRequestBuilder<RelaySyncWorker>(15, TimeUnit.MINUTES).setConstraints(constraints()).build())
                enqueue(context)
            } else {
                manager.cancelUniqueWork(IMMEDIATE)
                manager.cancelUniqueWork(PERIODIC)
            }
        }
    }
}
