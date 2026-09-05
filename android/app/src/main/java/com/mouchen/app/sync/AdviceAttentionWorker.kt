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
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.models.SecureSettingsStore
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject

internal data class PendingAdviceAttention(
    val adviceId: String,
    val claimToken: String,
    val deliveryNumber: Int,
    val notificationPosted: Boolean,
)

internal interface AdviceAttentionPendingStore {
    fun load(): PendingAdviceAttention?
    fun save(value: PendingAdviceAttention): Boolean
    fun clear(): Boolean
}

internal class SecureAdviceAttentionPendingStore(context: Context) : AdviceAttentionPendingStore {
    private val secureStore = SecureSettingsStore(context, PREFERENCES)

    override fun load(): PendingAdviceAttention? = decodePendingAdviceAttention(secureStore.get(KEY))

    override fun save(value: PendingAdviceAttention): Boolean =
        secureStore.putDurably(KEY, encodePendingAdviceAttention(value))

    override fun clear(): Boolean = secureStore.removeDurably(KEY)

    private companion object {
        const val PREFERENCES = "mouchen_advice_attention"
        const val KEY = "pending_claim"
    }
}

internal fun encodePendingAdviceAttention(value: PendingAdviceAttention): String = JSONObject()
    .put("advice_id", value.adviceId)
    .put("claim_token", value.claimToken)
    .put("delivery_number", value.deliveryNumber)
    .put("notification_posted", value.notificationPosted)
    .toString()

internal fun decodePendingAdviceAttention(value: String?): PendingAdviceAttention? {
    if (value == null) return null
    return runCatching {
        val root = JSONObject(value)
        val deliveryNumber = root.getInt("delivery_number")
        require(deliveryNumber in 1..2)
        PendingAdviceAttention(
            adviceId = UUID.fromString(root.getString("advice_id")).toString(),
            claimToken = UUID.fromString(root.getString("claim_token")).toString(),
            deliveryNumber = deliveryNumber,
            notificationPosted = root.getBoolean("notification_posted"),
        )
    }.getOrNull()
}

internal sealed interface AdviceAttentionStepResult {
    data object None : AdviceAttentionStepResult
    data object Released : AdviceAttentionStepResult
    data object Retry : AdviceAttentionStepResult
    data class Delivered(
        val adviceId: String,
        val nextEligibleAt: Long?,
    ) : AdviceAttentionStepResult
}

/**
 * Testable crash-recovery boundary for one server lease. An unconfirmed notification never calls
 * complete; a posted notification is durably remembered before completion is attempted.
 */
internal class AdviceAttentionCoordinator(
    private val notificationAvailable: () -> Boolean,
    private val claim: suspend () -> AdviceAttentionClaimResult,
    private val persistAdvice: suspend (AdviceEntity) -> Unit,
    private val postNotification: (AdviceEntity, Int) -> Boolean,
    private val complete: suspend (String, String) -> AdviceAttentionOperationResult,
    private val fail: suspend (String, String, String) -> AdviceAttentionOperationResult,
    private val pendingStore: AdviceAttentionPendingStore,
) {
    suspend fun recoverPending(): AdviceAttentionStepResult? {
        val pending = pendingStore.load() ?: return null
        return if (pending.notificationPosted) {
            finishCompletion(pending)
        } else {
            finishRelease(pending, "notification_not_confirmed_after_restart")
        }
    }

    suspend fun deliverOne(): AdviceAttentionStepResult {
        // A successful claim irreversibly consumes one of the two global deliveries. Permission
        // and channel checks therefore happen before any request reaches /claim.
        if (!runCatching(notificationAvailable).getOrDefault(false)) {
            return AdviceAttentionStepResult.None
        }
        val claimed = when (val result = claim()) {
            AdviceAttentionClaimResult.None -> return AdviceAttentionStepResult.None
            AdviceAttentionClaimResult.RetryableFailure -> return AdviceAttentionStepResult.Retry
            is AdviceAttentionClaimResult.Claimed -> result
        }
        var pending = PendingAdviceAttention(
            adviceId = claimed.advice.id,
            claimToken = claimed.claimToken,
            deliveryNumber = claimed.deliveryNumber,
            notificationPosted = false,
        )
        if (!pendingStore.save(pending)) {
            return releaseUnpersistedClaim(claimed, "local_claim_persistence_failed")
        }
        val persisted = runCatching { persistAdvice(claimed.advice) }.isSuccess
        if (!persisted) return finishRelease(pending, "local_advice_persistence_failed")
        if (!isAdviceEligibleForAttention(claimed.advice)) {
            return finishRelease(pending, "ineligible_advice")
        }

        val posted = runCatching {
            postNotification(claimed.advice, claimed.deliveryNumber)
        }.getOrDefault(false)
        if (!posted) return finishRelease(pending, "notification_unavailable")

        pending = pending.copy(notificationPosted = true)
        val postedStateSaved = pendingStore.save(pending)
        val result = finishCompletion(pending)
        // If completion was transiently unavailable, the durable posted marker is the only thing
        // preventing a later retry from releasing and then re-notifying this same delivery.
        return if (!postedStateSaved && result == AdviceAttentionStepResult.Retry) {
            AdviceAttentionStepResult.Retry
        } else {
            result
        }
    }

    private suspend fun releaseUnpersistedClaim(
        claimed: AdviceAttentionClaimResult.Claimed,
        reason: String,
    ): AdviceAttentionStepResult = when (fail(claimed.advice.id, claimed.claimToken, reason)) {
        AdviceAttentionOperationResult.Released,
        AdviceAttentionOperationResult.Terminal,
        -> AdviceAttentionStepResult.Released

        is AdviceAttentionOperationResult.Completed,
        AdviceAttentionOperationResult.RetryableFailure,
        -> AdviceAttentionStepResult.Retry
    }

    private suspend fun finishRelease(
        pending: PendingAdviceAttention,
        reason: String,
    ): AdviceAttentionStepResult = when (fail(pending.adviceId, pending.claimToken, reason)) {
        AdviceAttentionOperationResult.Released,
        AdviceAttentionOperationResult.Terminal,
        -> if (pendingStore.clear()) AdviceAttentionStepResult.Released else AdviceAttentionStepResult.Retry

        is AdviceAttentionOperationResult.Completed,
        AdviceAttentionOperationResult.RetryableFailure,
        -> AdviceAttentionStepResult.Retry
    }

    private suspend fun finishCompletion(
        pending: PendingAdviceAttention,
    ): AdviceAttentionStepResult {
        return when (val result = complete(pending.adviceId, pending.claimToken)) {
            is AdviceAttentionOperationResult.Completed -> {
                if (!pendingStore.clear()) return AdviceAttentionStepResult.Retry
                AdviceAttentionStepResult.Delivered(
                    adviceId = pending.adviceId,
                    nextEligibleAt = result.completion.nextEligibleAt,
                )
            }
            AdviceAttentionOperationResult.Terminal ->
                if (pendingStore.clear()) AdviceAttentionStepResult.Released else AdviceAttentionStepResult.Retry

            AdviceAttentionOperationResult.Released,
            AdviceAttentionOperationResult.RetryableFailure,
            -> AdviceAttentionStepResult.Retry
        }
    }
}

internal fun isAdviceEligibleForAttention(advice: AdviceEntity): Boolean =
    advice.status.equals("active", ignoreCase = true) &&
        advice.delivery.equals("immediate", ignoreCase = true)

class AdviceAttentionWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = RUN_MUTEX.withLock { runAttentionCycle() }

    private suspend fun runAttentionCycle(): Result {
        val fence = captureAuthFence(applicationContext) ?: return Result.success()
        if (!fence.isCurrent(applicationContext)) return Result.success()
        val connectionStore = BackendConnectionStore(applicationContext)
        if (!connectionStore.load().enabled) return Result.success()
        val dao = runCatching { MouchenDatabase.get(applicationContext).dao() }
            .getOrElse { return Result.retry() }
        val client = runCatching { AdviceAttentionClient(applicationContext, connectionStore) }
            .getOrElse { return Result.retry() }
        val syncClient = EventSyncClient(applicationContext, dao, connectionStore)
        val notifier = AdviceNotifier(applicationContext)
        val coordinator = AdviceAttentionCoordinator(
            notificationAvailable = notifier::canNotifyAdvice,
            claim = client::claim,
            persistAdvice = syncClient::persistClaimedAdvice,
            postNotification = notifier::notify,
            complete = client::complete,
            fail = client::fail,
            pendingStore = SecureAdviceAttentionPendingStore(applicationContext),
        )

        coordinator.recoverPending()?.let { recovered ->
            if (!fence.isCurrent(applicationContext)) return Result.success()
            when (recovered) {
                AdviceAttentionStepResult.Retry -> return Result.retry()
                AdviceAttentionStepResult.Released,
                AdviceAttentionStepResult.None,
                -> Unit
                is AdviceAttentionStepResult.Delivered -> scheduleNext(recovered)
            }
        }

        repeat(MAX_DELIVERIES_PER_RUN) {
            if (!fence.isCurrent(applicationContext)) return Result.success()
            when (val result = coordinator.deliverOne()) {
                AdviceAttentionStepResult.None -> return Result.success()
                AdviceAttentionStepResult.Released -> return Result.success()
                AdviceAttentionStepResult.Retry -> return Result.retry()
                is AdviceAttentionStepResult.Delivered -> scheduleNext(result)
            }
        }
        return Result.retry()
    }

    private fun scheduleNext(result: AdviceAttentionStepResult.Delivered) {
        result.nextEligibleAt?.let { eligibleAt ->
            AdviceAttentionScheduler.enqueueAt(applicationContext, result.adviceId, eligibleAt)
        }
    }

    private companion object {
        const val MAX_DELIVERIES_PER_RUN = 20
        val RUN_MUTEX = Mutex()
    }
}

internal object AdviceAttentionScheduler {
    private const val IMMEDIATE_WORK = "mouchen-advice-attention-now"
    private const val DUE_WORK_PREFIX = "mouchen-advice-attention-due-"
    private const val RETRY_DELAY_SECONDS = 30L

    fun enqueueNow(context: Context): Boolean = enqueue(
        context = context,
        uniqueName = IMMEDIATE_WORK,
        delayMs = 0L,
        policy = ExistingWorkPolicy.KEEP,
    )

    fun enqueueAt(context: Context, adviceId: String, eligibleAt: Long): Boolean = enqueue(
        context = context,
        uniqueName = dueWorkName(adviceId),
        delayMs = (eligibleAt - System.currentTimeMillis()).coerceAtLeast(0L),
        policy = ExistingWorkPolicy.REPLACE,
    )

    fun cancel(context: Context, adviceId: String) {
        WorkManager.getInstance(context.applicationContext).cancelUniqueWork(dueWorkName(adviceId))
    }

    internal fun dueWorkName(adviceId: String): String = "$DUE_WORK_PREFIX$adviceId"

    private fun enqueue(
        context: Context,
        uniqueName: String,
        delayMs: Long,
        policy: ExistingWorkPolicy,
    ): Boolean = runCatching {
        val request = OneTimeWorkRequestBuilder<AdviceAttentionWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build(),
            )
            .setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
            .setBackoffCriteria(
                BackoffPolicy.EXPONENTIAL,
                RETRY_DELAY_SECONDS,
                TimeUnit.SECONDS,
            )
            .build()
        WorkManager.getInstance(context.applicationContext)
            .enqueueUniqueWork(uniqueName, policy, request)
        true
    }.getOrDefault(false)
}
