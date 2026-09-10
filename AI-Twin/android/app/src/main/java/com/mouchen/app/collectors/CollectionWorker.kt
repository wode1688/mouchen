package com.mouchen.app.collectors

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkerParameters
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.security.EncryptedBlobStore
import com.mouchen.app.status.PipelineStatusStore
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.AuthFence
import com.mouchen.app.sync.captureAuthFence
import com.mouchen.app.sync.effectiveCollectionConsent
import com.mouchen.app.sync.hasAuthenticationCandidate
import kotlinx.coroutines.CancellationException

class CollectionWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val trigger = inputData.getString(INPUT_TRIGGER).orEmpty().ifBlank { "scheduled" }
        val status = PipelineStatusStore(applicationContext)
        val fence = inputAuthFence()
        if (fence == null || !fence.isCurrent(applicationContext)) return Result.success()
        if (!hasAuthenticationCandidate(applicationContext)) {
            status.markSyncAuthRequired()
            return Result.success()
        }
        if (!effectiveCollectionConsent(applicationContext)) {
            status.markCollectionDisabled("account_collection_consent_required")
            return Result.success()
        }
        status.markCollectionRunning(trigger)
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse {
                status.markCollectionFailed("database_unavailable")
                return Result.retry()
            }
        val failures = mutableListOf<String>()
        var attempted = 0
        var succeeded = 0

        suspend fun isolate(name: String, block: suspend () -> Unit) {
            if (!fence.isCurrent(applicationContext)) return
            attempted += 1
            try {
                block()
                if (!fence.isCurrent(applicationContext)) return
                succeeded += 1
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                failures += name
            }
        }

        isolate("usage") { UsageStatsCollector(applicationContext, dao).collect() }
        isolate("calendar") { CalendarCollector(applicationContext, dao).collect() }
        isolate("location") { LocationCollector(applicationContext, dao).collect() }
        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
            isolate("contacts") { ContactContextCollector(applicationContext, dao).collect() }
            isolate("sms") { SmsContextCollector(applicationContext, dao).collect() }
            isolate("call_log") { CallLogContextCollector(applicationContext, dao).collect() }
        }
        isolate("rss") { RssIntelCollector(dao, RssFeedStore(applicationContext)).collect() }
        isolate("imap") { ImapMailCollector(applicationContext, dao, MailConnectionStore(applicationContext)).collect() }
        isolate("private_phone_retention") {
            dao.deleteEventsByTypesBefore(
                PRIVATE_PHONE_EVENT_TYPES,
                System.currentTimeMillis() - PHONE_CONTEXT_LOOKBACK_MS,
            )
        }
        isolate("event_retention") { dao.deleteEventsBefore(System.currentTimeMillis() - RETENTION_MS) }
        isolate("blob_retention") {
            val cutoff = System.currentTimeMillis() - BLOB_RETENTION_MS
            EncryptedBlobStore(applicationContext).prune(
                cutoff = cutoff,
                maxBytes = BLOB_MAX_BYTES,
            )
            // Ledger rows live at least as long as their encrypted source, but do not accumulate
            // forever after the bounded encrypted-blob retention window closes.
            dao.deleteAudioTranscriptionJobsBefore(cutoff)
        }

        if (!fence.isCurrent(applicationContext)) return Result.success()

        status.markCollectionFinished(attempted, succeeded, failures)
        if (trigger == "manual_scan") {
            RealtimeSyncWorker.enqueueImmediate(applicationContext, "collection:$trigger")
        } else {
            RealtimeSyncWorker.enqueue(applicationContext, "collection:$trigger")
        }

        return if (failures.isNotEmpty()) Result.retry() else Result.success()
    }

    companion object {
        private const val RETENTION_MS = 90L * 24 * 60 * 60 * 1000
        private const val BLOB_RETENTION_MS = 7L * 24 * 60 * 60 * 1000
        private const val BLOB_MAX_BYTES = 512L * 1024 * 1024
        private const val IMMEDIATE_WORK = "mouchen-immediate-collection"
        private const val INPUT_TRIGGER = "trigger"
        private const val INPUT_AUTH_EPOCH = "auth_epoch"
        private const val INPUT_AUTH_USER = "auth_user"
        private const val INPUT_AUTH_ORIGIN = "auth_origin"
        private val PRIVATE_PHONE_EVENT_TYPES = listOf(
            "message.sms",
            "call.observed",
            "contact.identifier",
        )

        fun enqueueImmediate(context: Context, trigger: String = "event") {
            enqueue(context, trigger, ExistingWorkPolicy.APPEND_OR_REPLACE)
        }

        fun enqueueStartup(context: Context, trigger: String) {
            enqueue(context, trigger, ExistingWorkPolicy.KEEP)
        }

        private fun enqueue(context: Context, trigger: String, policy: ExistingWorkPolicy) {
            val appContext = context.applicationContext
            val fence = captureAuthFence(appContext)
            PipelineStatusStore(appContext).markCollectionQueued(trigger)
            WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
                IMMEDIATE_WORK,
                policy,
                OneTimeWorkRequestBuilder<CollectionWorker>()
                    .setInputData(
                        workDataOf(
                            INPUT_TRIGGER to trigger,
                            INPUT_AUTH_EPOCH to (fence?.epoch ?: -1L),
                            INPUT_AUTH_USER to fence?.userId.orEmpty(),
                            INPUT_AUTH_ORIGIN to fence?.origin.orEmpty(),
                        ),
                    )
                    .setBackoffCriteria(androidx.work.BackoffPolicy.EXPONENTIAL, 30, java.util.concurrent.TimeUnit.SECONDS)
                    .build(),
            )
        }
    }

    private fun inputAuthFence(): AuthFence? {
        val epoch = inputData.getLong(INPUT_AUTH_EPOCH, -1L)
        val user = inputData.getString(INPUT_AUTH_USER).orEmpty()
        val origin = inputData.getString(INPUT_AUTH_ORIGIN).orEmpty()
        return if (epoch > 0L && user.isNotBlank() && origin.isNotBlank()) {
            AuthFence(epoch, user, origin)
        } else {
            captureAuthFence(applicationContext)
        }
    }
}
