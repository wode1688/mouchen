package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.models.SecureSettingsStore
import com.mouchen.app.network.MouchenDns
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

internal sealed interface AdviceAttentionClaimResult {
    data object None : AdviceAttentionClaimResult

    data class Claimed(
        val advice: AdviceEntity,
        val claimToken: String,
        val deliveryNumber: Int,
        val leaseExpiresAt: Long,
    ) : AdviceAttentionClaimResult

    data object RetryableFailure : AdviceAttentionClaimResult
}

internal data class AdviceAttentionCompletion(
    val deliveryCount: Int,
    val nextEligibleAt: Long?,
)

internal sealed interface AdviceAttentionOperationResult {
    data class Completed(val completion: AdviceAttentionCompletion) : AdviceAttentionOperationResult
    data object Released : AdviceAttentionOperationResult
    data object Terminal : AdviceAttentionOperationResult
    data object RetryableFailure : AdviceAttentionOperationResult
}

/**
 * Owns the server-side attention lease protocol. Advice listing and notification delivery remain
 * separate operations: GET /v1/advice only mirrors state, while this client is the sole authority
 * for claiming one cross-device notification slot.
 */
internal class AdviceAttentionClient internal constructor(
    private val connectionProvider: () -> BackendConnection,
    private val deviceId: String,
    private val appVersion: String,
    private val client: OkHttpClient,
) {
    init {
        validateDeviceId(deviceId)
    }

    constructor(
        context: Context,
        connectionStore: BackendConnectionStore = BackendConnectionStore(context),
        client: OkHttpClient = defaultClient(context),
    ) : this(
        connectionProvider = {
            val stored = connectionStore.load()
            authenticatedConnection(context.applicationContext, stored)
                ?: stored.copy(enabled = false, userId = "", bearerToken = null)
        },
        deviceId = AndroidDeviceIdentity(context).deviceId(),
        appVersion = BuildConfig.VERSION_NAME,
        client = client,
    )

    suspend fun claim(): AdviceAttentionClaimResult = withContext(Dispatchers.IO) {
        val connection = connectionProvider()
        if (!connection.enabled) return@withContext AdviceAttentionClaimResult.None
        val body = JSONObject()
            .put("device_id", deviceId)
            .put("platform", PLATFORM)
            .put("app_version", appVersion)
            .toString()
        val request = authorizedRequest(
            connection,
            "${connection.baseUrl}/v1/advice-attention/claim",
            body,
        )
        runCatching {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use AdviceAttentionClaimResult.RetryableFailure
                parseAdviceAttentionClaim(response.body?.string().orEmpty())
            }
        }.getOrDefault(AdviceAttentionClaimResult.RetryableFailure)
    }

    suspend fun complete(
        adviceId: String,
        claimToken: String,
    ): AdviceAttentionOperationResult = postOperation(
        adviceId = adviceId,
        operation = "complete",
        body = JSONObject()
            .put("device_id", deviceId)
            .put("claim_token", claimToken)
            .toString(),
        parseSuccess = ::parseAdviceAttentionCompletion,
    )

    suspend fun fail(
        adviceId: String,
        claimToken: String,
        reason: String,
    ): AdviceAttentionOperationResult = postOperation(
        adviceId = adviceId,
        operation = "fail",
        body = JSONObject()
            .put("device_id", deviceId)
            .put("claim_token", claimToken)
            .put("reason", reason.take(MAX_FAILURE_REASON_LENGTH))
            .toString(),
        parseSuccess = { AdviceAttentionOperationResult.Released },
    )

    private suspend fun postOperation(
        adviceId: String,
        operation: String,
        body: String,
        parseSuccess: (String) -> AdviceAttentionOperationResult,
    ): AdviceAttentionOperationResult = withContext(Dispatchers.IO) {
        val connection = connectionProvider()
        if (!connection.enabled) return@withContext AdviceAttentionOperationResult.RetryableFailure
        val normalizedAdviceId = runCatching { UUID.fromString(adviceId).toString() }.getOrNull()
            ?: return@withContext AdviceAttentionOperationResult.Terminal
        val request = authorizedRequest(
            connection,
            "${connection.baseUrl}/v1/advice-attention/$normalizedAdviceId/$operation",
            body,
        )
        runCatching {
            client.newCall(request).execute().use { response ->
                when {
                    response.isSuccessful -> parseSuccess(response.body?.string().orEmpty())
                    response.code == HTTP_CONFLICT || response.code == HTTP_NOT_FOUND ->
                        AdviceAttentionOperationResult.Terminal
                    else -> AdviceAttentionOperationResult.RetryableFailure
                }
            }
        }.getOrDefault(AdviceAttentionOperationResult.RetryableFailure)
    }

    private fun authorizedRequest(
        connection: BackendConnection,
        url: String,
        body: String,
    ): Request = Request.Builder()
        .url(url)
        .header("X-User-Id", connection.userId)
        .apply {
            connection.bearerToken?.let { header("Authorization", "Bearer $it") }
        }
        .post(body.toRequestBody(JSON))
        .build()

    private companion object {
        const val PLATFORM = "android"
        const val HTTP_CONFLICT = 409
        const val HTTP_NOT_FOUND = 404
        const val MAX_FAILURE_REASON_LENGTH = 160
        val JSON = "application/json; charset=utf-8".toMediaType()

        fun defaultClient(context: Context? = null): OkHttpClient {
            val builder = OkHttpClient.Builder()
            .dns(MouchenDns)
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            if (context != null) builder.enforceSessionAuthorization(context.applicationContext)
            return builder.build()
        }
    }
}

internal fun parseAdviceAttentionClaim(responseBody: String): AdviceAttentionClaimResult = runCatching {
    val root = JSONObject(responseBody)
    when (root.getString("status")) {
        "none" -> AdviceAttentionClaimResult.None
        "claimed" -> {
            val deliveryNumber = root.getInt("delivery_number")
            require(deliveryNumber in 1..2) { "Unsupported delivery number" }
            val claimToken = UUID.fromString(root.getString("claim_token")).toString()
            AdviceAttentionClaimResult.Claimed(
                advice = parseAdviceRecord(root.getJSONObject("advice")),
                claimToken = claimToken,
                deliveryNumber = deliveryNumber,
                leaseExpiresAt = Instant.parse(root.getString("lease_expires_at")).toEpochMilli(),
            )
        }
        else -> error("Unsupported attention status")
    }
}.getOrDefault(AdviceAttentionClaimResult.RetryableFailure)

internal fun parseAdviceAttentionCompletion(responseBody: String): AdviceAttentionOperationResult = runCatching {
    val root = JSONObject(responseBody)
    require(root.getString("status") == "delivered") { "Unexpected completion status" }
    val deliveryCount = root.getInt("delivery_count")
    require(deliveryCount in 1..2) { "Unsupported delivery count" }
    AdviceAttentionOperationResult.Completed(
        AdviceAttentionCompletion(
            deliveryCount = deliveryCount,
            nextEligibleAt = if (root.isNull("next_eligible_at")) {
                null
            } else {
                Instant.parse(root.getString("next_eligible_at")).toEpochMilli()
            },
        ),
    )
}.getOrDefault(AdviceAttentionOperationResult.RetryableFailure)

/** Random install identity; it contains no hardware identifier and remains stable across restarts. */
internal class AndroidDeviceIdentity(context: Context) {
    private val secureStore = SecureSettingsStore(context, PREFERENCES)

    fun deviceId(): String = synchronized(LOCK) {
        normalizeDeviceId(secureStore.get(KEY))?.let { return@synchronized it }
        val generated = UUID.randomUUID().toString()
        check(secureStore.putDurably(KEY, generated)) { "Unable to persist Android device identity" }
        generated
    }

    private companion object {
        const val PREFERENCES = "mouchen_device_identity"
        const val KEY = "device_id"
        val LOCK = Any()
    }
}

internal fun normalizeDeviceId(value: String?): String? =
    value?.trim()?.takeIf(String::isNotEmpty)?.let { stored ->
        runCatching { UUID.fromString(stored).toString() }.getOrNull()
    }

private fun validateDeviceId(value: String): String =
    requireNotNull(normalizeDeviceId(value)) { "deviceId must be a UUID" }
