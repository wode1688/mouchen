package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.network.MouchenDns
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

enum class AdviceFeedbackKind(val apiValue: String, val localStatus: String?) {
    Adopted("adopted", "adopted"),
    Later("later", null),
    Acknowledged("acknowledged", null),
    StopTopic("stop_topic", "dismissed"),
    FactError("fact_error", "dismissed"),
    PredictionError("prediction_error", "dismissed"),
    Irrelevant("irrelevant", "dismissed"),
    TimingError("timing_error", "dismissed"),
}

enum class AdviceOutcomeKind(val apiValue: String, val actualResult: String) {
    Correct("correct", "用户在 Android 端确认：结果达成"),
    Incorrect("incorrect", "用户在 Android 端确认：结果未达成"),
}

data class AdviceFeedbackResult(
    val accepted: Boolean,
    val message: String,
)

class AdviceFeedbackClient private constructor(
    private val updateAdviceStatus: suspend (String, String) -> Int,
    private val loadAdvice: suspend (String) -> AdviceEntity?,
    private val loadConnection: () -> BackendConnection,
    private val scheduleFollowUp: (AdviceEntity) -> Unit,
    private val cancelFollowUp: (String) -> Unit,
    private val cancelAttention: (String) -> Unit,
    private val dismissAttention: (String) -> Unit,
    private val newFeedbackId: () -> String = { UUID.randomUUID().toString() },
    private val client: OkHttpClient = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build(),
) {
    constructor(
        context: Context,
        dao: MouchenDao,
        connectionStore: BackendConnectionStore = BackendConnectionStore(context),
        client: OkHttpClient = defaultClient(context),
    ) : this(
        updateAdviceStatus = dao::updateAdviceStatus,
        loadAdvice = dao::advice,
        loadConnection = {
            val stored = connectionStore.load()
            authenticatedConnection(context.applicationContext, stored)
                ?: stored.copy(enabled = false, userId = "", bearerToken = null)
        },
        scheduleFollowUp = { advice -> AdviceFollowUpScheduler.schedule(context, advice) },
        cancelFollowUp = { adviceId -> AdviceFollowUpScheduler.cancel(context, adviceId) },
        cancelAttention = { adviceId -> AdviceAttentionScheduler.cancel(context, adviceId) },
        dismissAttention = { adviceId -> AdviceNotifier(context).dismissAdvice(adviceId) },
        client = client,
    )

    internal constructor(
        connectionProvider: () -> BackendConnection,
        client: OkHttpClient,
        statusUpdater: suspend (String, String) -> Int,
        adviceLoader: suspend (String) -> AdviceEntity? = { null },
        followUpScheduler: (AdviceEntity) -> Unit = {},
        followUpCanceller: (String) -> Unit = {},
        attentionCanceller: (String) -> Unit = {},
        attentionDismisser: (String) -> Unit = {},
        feedbackIdFactory: () -> String = { UUID.randomUUID().toString() },
    ) : this(
        updateAdviceStatus = statusUpdater,
        loadAdvice = adviceLoader,
        loadConnection = connectionProvider,
        scheduleFollowUp = followUpScheduler,
        cancelFollowUp = followUpCanceller,
        cancelAttention = attentionCanceller,
        dismissAttention = attentionDismisser,
        newFeedbackId = feedbackIdFactory,
        client = client,
    )

    suspend fun submit(adviceId: String, kind: AdviceFeedbackKind): AdviceFeedbackResult =
        withContext(Dispatchers.IO) {
            val normalizedId = runCatching { UUID.fromString(adviceId).toString() }.getOrNull()
                ?: return@withContext AdviceFeedbackResult(false, "谏言编号无效")
            val connection = loadConnection()
            if (!connection.enabled) {
                return@withContext AdviceFeedbackResult(false, "Backend 未启用，反馈尚未记录")
            }
            runCatching {
                // Generate once per user action. OkHttp reuses this immutable request body for
                // connection retries, so a retry cannot create a second feedback record.
                val feedbackId = UUID.fromString(newFeedbackId()).toString()
                val body = JSONObject()
                    .put("kind", kind.apiValue)
                    .put("feedback_id", feedbackId)
                    .toString()
                val request = Request.Builder()
                    .url("${connection.baseUrl}/v1/advice/$normalizedId/feedback")
                    .header("X-User-Id", connection.userId)
                    .apply {
                        connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                    }
                    .post(body.toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        return@use AdviceFeedbackResult(false, "反馈失败：HTTP ${response.code}")
                    }
                    kind.localStatus?.let { localStatus ->
                        if (updateAdviceStatus(normalizedId, localStatus) != 1) {
                            return@use AdviceFeedbackResult(false, "反馈已送达，但本地状态更新失败")
                        }
                    }
                    if (kind == AdviceFeedbackKind.Adopted) {
                        loadAdvice(normalizedId)?.let(scheduleFollowUp)
                    } else {
                        cancelFollowUp(normalizedId)
                    }
                    if (kind != AdviceFeedbackKind.Later) {
                        cancelAttention(normalizedId)
                    }
                    dismissAttention(normalizedId)
                    AdviceFeedbackResult(
                        accepted = true,
                        message = when (kind) {
                            AdviceFeedbackKind.Adopted -> "已记录采纳"
                            AdviceFeedbackKind.Later -> "已稍后处理；AI替身会按新的时点再提醒"
                            AdviceFeedbackKind.Acknowledged -> "已阅；本条不再提醒"
                            AdviceFeedbackKind.StopTopic -> "言尽于此，已停止此条谏言"
                            AdviceFeedbackKind.FactError -> "已记录事实错误并撤回谏言"
                            AdviceFeedbackKind.PredictionError -> "已记录预测错误并撤回谏言"
                            AdviceFeedbackKind.Irrelevant -> "已记录与目标无关并撤回谏言"
                            AdviceFeedbackKind.TimingError -> "已记录时机错误并撤回谏言"
                        },
                    )
                }
            }.getOrElse {
                AdviceFeedbackResult(false, "反馈未送达，请检查 Backend 连接")
            }
        }

    /** Records owner-supplied direction as learning input and closes only this advice's reminders. */
    suspend fun submitGuidance(adviceId: String, note: String): AdviceFeedbackResult =
        withContext(Dispatchers.IO) {
            val normalizedId = runCatching { UUID.fromString(adviceId).toString() }.getOrNull()
                ?: return@withContext AdviceFeedbackResult(false, "谏言编号无效")
            val normalizedNote = note.trim()
            if (normalizedNote.isEmpty()) {
                return@withContext AdviceFeedbackResult(false, "请先写下希望AI替身调整的方向")
            }
            if (normalizedNote.length > MAX_GUIDANCE_NOTE_LENGTH) {
                return@withContext AdviceFeedbackResult(
                    false,
                    "方向说明不能超过 $MAX_GUIDANCE_NOTE_LENGTH 个字",
                )
            }
            val connection = loadConnection()
            if (!connection.enabled) {
                return@withContext AdviceFeedbackResult(false, "Backend 未启用，方向尚未记录")
            }
            runCatching {
                // Keep one idempotency key for every transport attempt of this submission.
                val feedbackId = UUID.fromString(newFeedbackId()).toString()
                val body = JSONObject()
                    .put("kind", GUIDANCE_KIND)
                    .put("note", normalizedNote)
                    .put("feedback_id", feedbackId)
                    .toString()
                val request = Request.Builder()
                    .url("${connection.baseUrl}/v1/advice/$normalizedId/feedback")
                    .header("X-User-Id", connection.userId)
                    .apply {
                        connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                    }
                    .post(body.toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        return@use AdviceFeedbackResult(false, "方向提交失败：HTTP ${response.code}")
                    }
                    // Guidance is learning input, not a rejection or adoption. Keep the advice
                    // record/status intact; the backend resolves its central attention row.
                    cancelFollowUp(normalizedId)
                    cancelAttention(normalizedId)
                    dismissAttention(normalizedId)
                    AdviceFeedbackResult(true, "已记住你的方向，后续建言会据此调整")
                }
            }.getOrElse {
                AdviceFeedbackResult(false, "方向尚未送达，请检查 Backend 连接")
            }
        }

    suspend fun submitOutcome(adviceId: String, kind: AdviceOutcomeKind): AdviceFeedbackResult =
        withContext(Dispatchers.IO) {
            val normalizedId = runCatching { UUID.fromString(adviceId).toString() }.getOrNull()
                ?: return@withContext AdviceFeedbackResult(false, "谏言编号无效")
            val connection = loadConnection()
            if (!connection.enabled) {
                return@withContext AdviceFeedbackResult(false, "Backend 未启用，结果尚未记录")
            }
            runCatching {
                val body = JSONObject()
                    .put("status", kind.apiValue)
                    .put("actual_result", kind.actualResult)
                    .toString()
                val request = Request.Builder()
                    .url("${connection.baseUrl}/v1/advice/$normalizedId/outcome")
                    .header("X-User-Id", connection.userId)
                    .apply {
                        connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                    }
                    .post(body.toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful && response.code != HTTP_CONFLICT) {
                        return@use AdviceFeedbackResult(false, "结果提交失败：HTTP ${response.code}")
                    }
                    val updated = updateAdviceStatus(normalizedId, VERIFIED_STATUS)
                    cancelFollowUp(normalizedId)
                    cancelAttention(normalizedId)
                    dismissAttention(normalizedId)
                    if (updated != 1) {
                        return@use AdviceFeedbackResult(false, "结果已送达，但本地状态更新失败")
                    }
                    AdviceFeedbackResult(
                        accepted = true,
                        message = if (kind == AdviceOutcomeKind.Correct) {
                            "已核验：结果达成"
                        } else {
                            "已核验：结果未达成"
                        },
                    )
                }
            }.getOrElse {
                AdviceFeedbackResult(false, "结果未送达，请检查 Backend 连接")
            }
        }

    companion object {
        private const val HTTP_CONFLICT = 409
        private const val VERIFIED_STATUS = "verified"
        private const val GUIDANCE_KIND = "guidance"
        internal const val MAX_GUIDANCE_NOTE_LENGTH = 2_000
        val JSON = "application/json; charset=utf-8".toMediaType()

        private fun defaultClient(context: Context) = OkHttpClient.Builder()
            .dns(MouchenDns)
            .enforceSessionAuthorization(context.applicationContext)
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build()
    }
}
