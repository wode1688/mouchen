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
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.data.UrgentEventQueueEntity
import com.mouchen.app.network.MouchenDns
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import okhttp3.OkHttpClient

/**
 * Drains a durable priority queue with one running WorkManager chain, independent of backlog size.
 * Each poison row receives two bounded priority retries and then falls back to the ordinary queue.
 */
class UrgentEventSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val queuedFence = inputAuthFence()
        if (queuedFence == null || !queuedFence.isCurrent(applicationContext)) return Result.success()
        val demandStore = urgentDemandStore(applicationContext)
        var demand = demandStore.snapshot()
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse { return Result.retry() }
        val connectionStore = BackendConnectionStore(applicationContext)
        val storedConnection = connectionStore.load()
        if (!storedConnection.enabled) {
            demandStore.release()
            return Result.success()
        }
        val connection = authenticatedConnection(applicationContext, storedConnection)
        if (connection == null || captureAuthFence(applicationContext) != queuedFence) return Result.success()
        if (!AdvicePollingWorker.recoverQueuedEventPolling(applicationContext)) {
            return Result.retry()
        }
        val client = EventSyncClient(
            applicationContext,
            dao,
            connectionStore,
            client = buildUrgentEventHttpClient(applicationContext),
        )
        return try {
            var demandCycles = 0
            while (true) {
                if (!queuedFence.isCurrent(applicationContext)) return Result.success()
                var handled = 0
                while (handled < MAX_EVENTS_PER_RUN) {
                    val entry = dao.nextDueUrgentEvent(System.currentTimeMillis()) ?: break
                    val outcome = client.syncSingleEvent(entry.eventId, semanticFastLane = true)
                    if (!queuedFence.isCurrent(applicationContext)) return Result.success()
                    when (outcome.result) {
                        SingleEventSyncResult.DELIVERED,
                        SingleEventSyncResult.ALREADY_SYNCED,
                        SingleEventSyncResult.NOT_FOUND,
                        -> {
                            dao.deleteUrgentEvent(entry.eventId)
                            if (outcome.advicePullRequired) {
                                if (!AdvicePollingWorker.enqueueAfterQueuedEvent(applicationContext)) {
                                    return Result.retry()
                                }
                            }
                        }

                        SingleEventSyncResult.FAILED -> {
                            if (!handleFailure(entry, queuedFence)) return Result.retry()
                        }
                        SingleEventSyncResult.DISABLED -> {
                            demandStore.release()
                            return Result.success()
                        }
                    }
                    handled += 1
                }

                if (dao.urgentEventCount() > 0) {
                    val nextAttemptAt = dao.earliestUrgentAttemptAt() ?: System.currentTimeMillis()
                    val delay = (nextAttemptAt - System.currentTimeMillis())
                        .coerceIn(0L, MAX_CONTINUATION_DELAY_MS)
                    return if (enqueueContinuation(applicationContext, delay)) {
                        Result.success()
                    } else {
                        Result.retry()
                    }
                }

                if (demandStore.completeIfUnchanged(demand.generation)) return Result.success()
                demandCycles += 1
                if (demandCycles >= MAX_DEMAND_CYCLES) {
                    return if (enqueueContinuation(applicationContext, 0L)) {
                        Result.success()
                    } else {
                        Result.retry()
                    }
                }
                demand = demandStore.snapshot()
            }
            @Suppress("UNREACHABLE_CODE")
            Result.success()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            Result.retry()
        }
    }

    private suspend fun handleFailure(entry: UrgentEventQueueEntity, fence: AuthFence): Boolean {
        if (!fence.isCurrent(applicationContext)) return true
        val now = System.currentTimeMillis()
        return when (val plan = urgentFailurePlan(entry.attempts, now)) {
            is UrgentFailurePlan.RetryAt -> {
                MouchenDatabase.get(applicationContext).dao().markUrgentEventFailed(
                    eventId = entry.eventId,
                    expectedAttempts = entry.attempts,
                    nextAttemptAt = plan.at,
                    updatedAt = now,
                    reason = "semantic_fast_lane_failed",
                )
                true
            }
            UrgentFailurePlan.FallbackToNormal -> {
                val dao = MouchenDatabase.get(applicationContext).dao()
                scheduleUrgentFallback(
                    // The urgent attempts moved the ordinary due time into the future. Reset only
                    // the due time while preserving attempt data.
                    makeOrdinaryDeliveryDue = { dao.makeEventDeliveryDueNow(entry.eventId) },
                    enqueueOrdinaryWorker = {
                        RealtimeSyncWorker.enqueue(applicationContext, "urgent_retry_exhausted")
                    },
                    // Retain this durable urgent row unless ordinary WorkManager enqueue is
                    // confirmed. A false result makes doWork return Result.retry().
                    deleteUrgentRow = { dao.deleteUrgentEvent(entry.eventId) },
                )
            }
        }
    }

    companion object {
        private const val UNIQUE_WORK = "mouchen-urgent-event-drain"
        private const val MAX_EVENTS_PER_RUN = 40
        private const val MAX_DEMAND_CYCLES = 3
        private const val MAX_CONTINUATION_DELAY_MS = 5L * 60 * 1000
        private const val INPUT_AUTH_EPOCH = "auth_epoch"
        private const val INPUT_AUTH_USER = "auth_user"
        private const val INPUT_AUTH_ORIGIN = "auth_origin"

        internal suspend fun enqueue(
            context: Context,
            eventId: String,
            category: UrgencyCategory,
        ): Boolean {
            if (eventId.isBlank() || eventId.length > MAX_EVENT_ID_CHARACTERS) return false
            val appContext = context.applicationContext
            val inserted = runCatching {
                MouchenDatabase.get(appContext).dao().insertUrgentEventIfAbsent(
                    UrgentEventQueueEntity(eventId = eventId, category = category.wireValue),
                )
            }.isSuccess
            if (!inserted) return false
            val demandStore = urgentDemandStore(appContext)
            val lease = demandStore.request("${category.wireValue}:$eventId")
            if (!lease.shouldSchedule) return true
            return if (enqueueInitial(appContext)) {
                true
            } else {
                demandStore.rollbackScheduledRequest(lease.generation)
                false
            }
        }

        fun recover(context: Context): Boolean {
            val appContext = context.applicationContext
            val demandStore = urgentDemandStore(appContext)
            val lease = demandStore.recover("process_or_settings_recovery")
            return if (enqueueInitial(appContext)) {
                true
            } else {
                demandStore.rollbackScheduledRequest(lease.generation)
                false
            }
        }

        internal fun urgentWorkName(): String = UNIQUE_WORK

        private fun enqueueInitial(context: Context): Boolean =
            enqueueRequest(context, ExistingWorkPolicy.KEEP, 0L)

        private fun enqueueContinuation(context: Context, delayMs: Long): Boolean =
            enqueueRequest(context, ExistingWorkPolicy.APPEND_OR_REPLACE, delayMs)

        private fun enqueueRequest(
            context: Context,
            policy: ExistingWorkPolicy,
            delayMs: Long,
        ): Boolean = confirmedWorkEnqueue {
            val fence = captureAuthFence(context.applicationContext)
            val request = OneTimeWorkRequestBuilder<UrgentEventSyncWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build(),
                )
                .apply {
                    if (delayMs > 0L) {
                        setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
                    } else {
                        setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                    }
                }
                .setInputData(
                    workDataOf(
                        INPUT_AUTH_EPOCH to (fence?.epoch ?: -1L),
                        INPUT_AUTH_USER to fence?.userId.orEmpty(),
                        INPUT_AUTH_ORIGIN to fence?.origin.orEmpty(),
                    ),
                )
                .build()
            WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
                UNIQUE_WORK,
                policy,
                request,
            ).result.get(WORK_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        }

        private const val WORK_OPERATION_TIMEOUT_SECONDS = 5L
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

internal suspend fun scheduleUrgentFallback(
    makeOrdinaryDeliveryDue: suspend () -> Unit,
    enqueueOrdinaryWorker: () -> Boolean,
    deleteUrgentRow: suspend () -> Unit,
): Boolean {
    makeOrdinaryDeliveryDue()
    if (!enqueueOrdinaryWorker()) return false
    deleteUrgentRow()
    return true
}

internal sealed interface UrgentFailurePlan {
    data class RetryAt(val at: Long) : UrgentFailurePlan
    data object FallbackToNormal : UrgentFailurePlan
}

internal fun urgentFailurePlan(attemptsBeforeFailure: Int, now: Long): UrgentFailurePlan {
    val attemptsAfterFailure = attemptsBeforeFailure.coerceAtLeast(0) + 1
    if (attemptsAfterFailure >= MAX_PRIORITY_ATTEMPTS) return UrgentFailurePlan.FallbackToNormal
    val delay = if (attemptsAfterFailure == 1) 10_000L else 30_000L
    return UrgentFailurePlan.RetryAt(if (now > Long.MAX_VALUE - delay) Long.MAX_VALUE else now + delay)
}

internal fun buildUrgentEventHttpClient(context: Context? = null): OkHttpClient {
    val builder = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(8, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
    if (context != null) builder.enforceSessionAuthorization(context.applicationContext)
    return builder.build()
}

private fun urgentDemandStore(context: Context): SyncDemandStore =
    SyncDemandStore(context, URGENT_DEMAND_PREFERENCES)

private const val URGENT_DEMAND_PREFERENCES = "urgent_event_sync_demand"
private const val MAX_EVENT_ID_CHARACTERS = 200
private const val MAX_PRIORITY_ATTEMPTS = 3
