package com.mouchen.app.collectors

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.mouchen.app.sync.AdviceAttentionWorker
import com.mouchen.app.sync.AdvicePollingWorker
import com.mouchen.app.sync.SessionHealthWorker
import com.mouchen.app.sync.legacyMigrationAllowsAuthenticatedWork
import java.util.concurrent.TimeUnit

object CollectionScheduler {
    private const val PERIODIC_WORK = "mouchen-incremental-collection"
    private const val ADVICE_POLL_WORK = "mouchen-advice-poll"
    private const val ADVICE_ATTENTION_WORK = "mouchen-advice-attention-poll"

    fun ensurePeriodic(context: Context) {
        if (!legacyMigrationAllowsAuthenticatedWork(context.applicationContext)) return
        SessionHealthWorker.ensurePeriodic(context)
        val workManager = WorkManager.getInstance(context.applicationContext)
        val collectionRequest = PeriodicWorkRequestBuilder<CollectionWorker>(30, TimeUnit.MINUTES).build()
        workManager.enqueueUniquePeriodicWork(
            PERIODIC_WORK,
            ExistingPeriodicWorkPolicy.UPDATE,
            collectionRequest,
        )

        val advicePollRequest = PeriodicWorkRequestBuilder<AdvicePollingWorker>(15, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build(),
            )
            .build()
        workManager.enqueueUniquePeriodicWork(
            ADVICE_POLL_WORK,
            ExistingPeriodicWorkPolicy.UPDATE,
            advicePollRequest,
        )

        // The backend owns cross-device count, spacing and terminal state. Android only wakes to
        // ask for a lease; a normal advice sync never posts a notification by itself.
        val attentionRequest = PeriodicWorkRequestBuilder<AdviceAttentionWorker>(15, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build(),
            )
            .build()
        workManager.enqueueUniquePeriodicWork(
            ADVICE_ATTENTION_WORK,
            ExistingPeriodicWorkPolicy.UPDATE,
            attentionRequest,
        )
    }
}
