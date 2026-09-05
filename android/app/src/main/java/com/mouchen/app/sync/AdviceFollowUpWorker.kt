package com.mouchen.app.sync

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.MouchenDatabase
import java.time.Instant
import java.util.concurrent.TimeUnit
import org.json.JSONObject

/** Reminds the owner to verify an adopted recommendation at its promised checkpoint. */
class AdviceFollowUpWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val adviceId = inputData.getString(INPUT_ADVICE_ID).orEmpty()
        if (adviceId.isBlank()) return Result.failure()
        val advice = runCatching { MouchenDatabase.get(applicationContext).dao().advice(adviceId) }
            .getOrElse { return Result.retry() }
            ?: return Result.success()
        if (advice.status != ADOPTED_STATUS) return Result.success()
        if (advice.followUpNotifiedAt != null) return Result.success()
        if (!AdviceNotifier(applicationContext).notifyFollowUp(advice)) {
            // Notification permission may be granted later. Preserve the checkpoint instead of
            // silently losing it while the recommendation is still awaiting verification.
            return Result.retry()
        }
        return runCatching {
            MouchenDatabase.get(applicationContext).dao().markAdviceFollowUpNotified(
                advice.id,
                System.currentTimeMillis(),
            )
            Result.success()
        }.getOrElse { Result.retry() }
    }

    companion object {
        internal const val INPUT_ADVICE_ID = "advice_id"
        internal const val ADOPTED_STATUS = "adopted"
    }
}

internal object AdviceFollowUpScheduler {
    fun schedule(context: Context, advice: AdviceEntity, nowMillis: Long = System.currentTimeMillis()): Boolean {
        if (advice.status != AdviceFollowUpWorker.ADOPTED_STATUS || advice.followUpNotifiedAt != null) {
            return false
        }
        val delay = adviceFollowUpDelayMillis(advice.predictionJson, nowMillis) ?: return false
        val request = OneTimeWorkRequestBuilder<AdviceFollowUpWorker>()
            .setInitialDelay(delay, TimeUnit.MILLISECONDS)
            .setInputData(workDataOf(AdviceFollowUpWorker.INPUT_ADVICE_ID to advice.id))
            .build()
        WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
            workName(advice.id),
            ExistingWorkPolicy.KEEP,
            request,
        )
        return true
    }

    fun reconcile(context: Context, advice: AdviceEntity) {
        when (adviceFollowUpReconcileAction(advice)) {
            AdviceFollowUpReconcileAction.Schedule -> schedule(context, advice)
            AdviceFollowUpReconcileAction.Cancel -> cancel(context, advice.id)
            AdviceFollowUpReconcileAction.None -> Unit
        }
    }

    fun cancel(context: Context, adviceId: String) {
        WorkManager.getInstance(context.applicationContext).cancelUniqueWork(workName(adviceId))
    }

    internal fun workName(adviceId: String) = "mouchen-advice-follow-up-$adviceId"
}

internal enum class AdviceFollowUpReconcileAction {
    Schedule,
    Cancel,
    None,
}

internal fun adviceFollowUpReconcileAction(advice: AdviceEntity): AdviceFollowUpReconcileAction =
    when (advice.status.lowercase()) {
        AdviceFollowUpWorker.ADOPTED_STATUS -> if (advice.followUpNotifiedAt == null) {
            AdviceFollowUpReconcileAction.Schedule
        } else {
            AdviceFollowUpReconcileAction.None
        }
        "verified", "withdrawn" -> AdviceFollowUpReconcileAction.Cancel
        else -> AdviceFollowUpReconcileAction.None
    }

internal fun adviceFollowUpDelayMillis(predictionJson: String, nowMillis: Long): Long? {
    val deadline = runCatching {
        JSONObject(predictionJson).optString("deadline").takeIf(String::isNotBlank)?.let {
            Instant.parse(it).toEpochMilli()
        }
    }.getOrNull() ?: return null
    return (deadline - nowMillis).coerceIn(0L, MAX_FOLLOW_UP_DELAY_MS)
}

private const val MAX_FOLLOW_UP_DELAY_MS = 365L * 24 * 60 * 60 * 1000
