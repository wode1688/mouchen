package com.mouchen.app.collectors

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
import com.mouchen.app.data.AudioTranscriptionJobEntity
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.network.MouchenDns
import com.mouchen.app.security.EncryptedBlobStore
import com.mouchen.app.sync.AudioProcessingLocation
import com.mouchen.app.sync.AuthFence
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.authenticatedConnection
import com.mouchen.app.sync.captureAuthFence
import com.mouchen.app.sync.enforceSessionAuthorization
import com.mouchen.app.sync.effectiveCollectionConsent
import com.mouchen.app.sync.isPrivateNodeUrl
import com.mouchen.app.sync.sameHttpsOrigin
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

internal data class LocalTranscript(
    val text: String,
    val language: String,
    val engine: String,
    val durationMs: Long,
)

internal sealed interface LocalSttAttempt {
    data class Completed(
        val transcript: LocalTranscript,
        val processingLocation: AudioProcessingLocation,
    ) : LocalSttAttempt
    data class Retryable(val reason: String) : LocalSttAttempt
    data class PermanentFailure(val reason: String) : LocalSttAttempt
}

internal class LocalSttClient(
    private val connectionProvider: () -> BackendConnection,
    private val client: OkHttpClient,
) {
    constructor(context: Context) : this(
        connectionProvider = {
            authenticatedConnection(
                context.applicationContext,
                BackendConnectionStore(context.applicationContext).load(),
            ) ?: BackendConnection("https://invalid.invalid", enabled = false)
        },
        client = buildLocalSttHttpClient(context.applicationContext),
    )

    suspend fun transcribe(
        pcm: ByteArray,
        segmentId: String,
        sampleRate: Int = AUDIO_TRANSCRIPTION_SAMPLE_RATE,
        language: String = "zh",
    ): LocalSttAttempt = withContext(Dispatchers.IO) {
        val connection = connectionProvider()
        localSttConfigurationFailure(connection)?.let {
            return@withContext LocalSttAttempt.PermanentFailure(it)
        }
        val token = connection.bearerToken!!.trim()
        val url = "${connection.sttBaseUrl!!.trimEnd('/')}/v1/audio/transcribe"
            .toHttpUrlOrNull()
            ?.newBuilder()
            ?.setQueryParameter("sample_rate", sampleRate.toString())
            ?.setQueryParameter("language", language)
            ?.build()
            ?: return@withContext LocalSttAttempt.PermanentFailure("invalid_backend_url")
        try {
            val request = Request.Builder()
                .url(url)
                .header("Authorization", "Bearer $token")
                .header("X-User-Id", connection.userId)
                .header("X-Segment-Id", segmentId.take(120))
                .header("X-Local-STT-Only", "true")
                .post(pcm.toRequestBody(PCM_MEDIA_TYPE))
                .build()
            client.newCall(request).execute().use { response ->
                val body = response.body?.string().orEmpty()
                if (response.isSuccessful) {
                    return@withContext runCatching { parseLocalTranscript(body) }
                        .fold(
                            onSuccess = {
                                LocalSttAttempt.Completed(it, connection.sttProcessingLocation)
                            },
                            onFailure = { LocalSttAttempt.PermanentFailure("invalid_stt_response") },
                        )
                }
                return@withContext when (response.code) {
                    429 -> LocalSttAttempt.Retryable("local_stt_http_429")
                    in 500..599 -> if (response.code == 503) {
                        LocalSttAttempt.PermanentFailure("local_stt_unavailable_503")
                    } else {
                        LocalSttAttempt.Retryable("local_stt_http_${response.code}")
                    }
                    else -> LocalSttAttempt.PermanentFailure("local_stt_http_${response.code}")
                }
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: IOException) {
            LocalSttAttempt.Retryable("local_stt_unreachable")
        } catch (_: Exception) {
            LocalSttAttempt.PermanentFailure("local_stt_client_error")
        }
    }

    private companion object {
        val PCM_MEDIA_TYPE = "audio/L16; rate=16000; channels=1".toMediaType()
    }
}

/** Raw PCM and its bearer token must never be replayed to a redirect target. */
internal fun buildLocalSttHttpClient(context: Context? = null): OkHttpClient {
    val builder = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(45, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.MINUTES)
    if (context != null) builder.enforceSessionAuthorization(context.applicationContext)
    return builder.build()
}

class AudioTranscriptionWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private var authFence: AuthFence? = null

    override suspend fun doWork(): Result {
        val blobName = inputData.getString(INPUT_BLOB).orEmpty()
        val segmentStartedAt = inputData.getLong(INPUT_STARTED_AT, 0L)
        val segmentEndedAt = inputData.getLong(INPUT_ENDED_AT, 0L)
        val requestedDestinationFingerprint = inputData.getString(INPUT_DESTINATION_FINGERPRINT).orEmpty()
        if (!validAudioTranscriptionMetadata(blobName, segmentStartedAt, segmentEndedAt) ||
            !validAudioDestinationFingerprint(requestedDestinationFingerprint)
        ) {
            return terminalFailure("invalid_segment_metadata")
        }
        val queuedFence = inputAuthFence() ?: return Result.success()
        if (!queuedFence.isCurrent(applicationContext)) return Result.success()
        authFence = queuedFence
        runCatching { AudioTranscriptionRecoveryWorker.ensurePeriodic(applicationContext) }
        val dao = MouchenDatabase.get(applicationContext).dao()
        val now = System.currentTimeMillis()
        dao.insertAudioTranscriptionJobIfAbsent(
            AudioTranscriptionJobEntity(
                blobName = blobName,
                segmentStartedAt = segmentStartedAt,
                segmentEndedAt = segmentEndedAt,
                destinationFingerprint = requestedDestinationFingerprint,
                status = AUDIO_JOB_ENQUEUED,
                updatedAt = now,
            ),
        )
        val recordedJob = dao.audioTranscriptionJob(blobName)
        if (recordedJob?.status == AUDIO_JOB_COMPLETED || recordedJob?.status == AUDIO_JOB_ABANDONED) {
            return Result.success()
        }
        // The microphone/system permission is not an application-layer grant. If the signed-in
        // account disables collection, queued PCM must not leave the device even if this work was
        // enqueued while consent was still enabled.
        if (!effectiveCollectionConsent(applicationContext)) {
            return finishFailure(
                blobName,
                segmentStartedAt,
                segmentEndedAt,
                "account_collection_consent_revoked",
                recoverable = false,
            )
        }
        val destinationFingerprint = recordedJob?.destinationFingerprint.orEmpty()
        if (!validAudioDestinationFingerprint(destinationFingerprint)) {
            return finishFailure(
                blobName,
                segmentStartedAt,
                segmentEndedAt,
                "destination_binding_missing",
                recoverable = false,
            )
        }
        val boundConnection = runCatching {
            authenticatedConnection(applicationContext, BackendConnectionStore(applicationContext).load())
        }.getOrNull()
        if (boundConnection == null || audioDestinationFingerprint(boundConnection) != destinationFingerprint) {
            return finishFailure(
                blobName,
                segmentStartedAt,
                segmentEndedAt,
                "stt_destination_changed",
                recoverable = false,
            )
        }
        if (runAttemptCount >= MAX_TOTAL_ATTEMPTS) {
            return finishFailure(
                blobName,
                segmentStartedAt,
                segmentEndedAt,
                "retry_budget_exhausted",
                recoverable = true,
            )
        }

        val pcm = try {
            EncryptedBlobStore(applicationContext).readChunked(blobName, AUDIO_TRANSCRIPTION_MAX_PLAIN_BYTES)
        } catch (_: Exception) {
            return finishFailure(
                blobName,
                segmentStartedAt,
                segmentEndedAt,
                "encrypted_segment_unreadable",
                recoverable = false,
            )
        }
        return try {
            val client = LocalSttClient(
                connectionProvider = { boundConnection },
                client = buildLocalSttHttpClient(applicationContext),
            )
            // Recheck after decrypting because consent can change while this worker is running.
            // This is the final application-controlled gate before LocalSttClient performs I/O.
            if (!effectiveCollectionConsent(applicationContext)) {
                return finishFailure(
                    blobName,
                    segmentStartedAt,
                    segmentEndedAt,
                    "account_collection_consent_revoked",
                    recoverable = false,
                )
            }
            when (val attempt = client.transcribe(pcm, blobName)) {
                is LocalSttAttempt.Completed -> {
                    if (!queuedFence.isCurrent(applicationContext)) return Result.success()
                    persistTranscript(
                        blobName,
                        segmentStartedAt,
                        segmentEndedAt,
                        attempt.transcript,
                        attempt.processingLocation,
                    )
                    if (!queuedFence.isCurrent(applicationContext)) return Result.success()
                    dao.markAudioTranscriptionCompleted(blobName, System.currentTimeMillis())
                    runCatching { AudioTranscriptionRecoveryWorker.enqueueImmediate(applicationContext) }
                    Result.success()
                }
                is LocalSttAttempt.Retryable -> if (canRetryLocalStt(runAttemptCount)) {
                    Result.retry()
                } else {
                    finishFailure(
                        blobName,
                        segmentStartedAt,
                        segmentEndedAt,
                        "retry_budget_exhausted:${attempt.reason}",
                        recoverable = true,
                    )
                }
                is LocalSttAttempt.PermanentFailure -> finishFailure(
                    blobName,
                    segmentStartedAt,
                    segmentEndedAt,
                    attempt.reason,
                    recoverable = shouldRecoverAudioFailure(attempt.reason),
                )
            }
        } finally {
            pcm.fill(0)
        }
    }

    private fun terminalFailure(reason: String): Result = Result.failure(
        workDataOf(OUTPUT_FAILURE_REASON to reason.take(MAX_FAILURE_REASON_CHARACTERS)),
    )

    private suspend fun finishFailure(
        blobName: String,
        segmentStartedAt: Long,
        segmentEndedAt: Long,
        reason: String,
        recoverable: Boolean,
    ): Result {
        if (authFence?.isCurrent(applicationContext) != true) return Result.success()
        val dao = MouchenDatabase.get(applicationContext).dao()
        val now = System.currentTimeMillis()
        dao.insertAudioTranscriptionJobIfAbsent(
            AudioTranscriptionJobEntity(
                blobName = blobName,
                segmentStartedAt = segmentStartedAt,
                segmentEndedAt = segmentEndedAt,
                status = AUDIO_JOB_ENQUEUED,
                updatedAt = now,
            ),
        )
        val current = dao.audioTranscriptionJob(blobName)
        if (current != null && current.status != AUDIO_JOB_COMPLETED) {
            val next = nextAudioFailureRecoveryState(
                currentRecoveryCycles = current.recoveryCycles,
                now = now,
                recoverable = recoverable,
            )
            dao.transitionAudioTranscriptionFailure(
                blobName = blobName,
                expectedRecoveryCycles = current.recoveryCycles,
                newStatus = next.status,
                newRecoveryCycles = next.recoveryCycles,
                nextAttemptAt = next.nextAttemptAt,
                reason = reason.take(AUDIO_FAILURE_REASON_LIMIT),
                updatedAt = now,
            )
        }
        // This request is still RUNNING, so refill through a separate coalesced worker after it
        // reaches a finished state. The periodic worker remains the reboot/process-death fallback.
        runCatching { AudioTranscriptionRecoveryWorker.enqueueImmediate(applicationContext) }
        return terminalFailure(reason)
    }

    private suspend fun persistTranscript(
        blobName: String,
        segmentStartedAt: Long,
        segmentEndedAt: Long,
        transcript: LocalTranscript,
        processingLocation: AudioProcessingLocation,
    ) {
        val fence = authFence ?: return
        if (!fence.isCurrent(applicationContext)) return
        val text = transcript.text.trim().take(MAX_TRANSCRIPT_CHARACTERS)
        if (text.isEmpty()) return
        val eventId = UUID.nameUUIDFromBytes(
            "speech.transcript:$blobName".toByteArray(StandardCharsets.UTF_8),
        ).toString()
        val event = LocalEventEntity(
            id = eventId,
            source = "android.microphone.transcript",
            type = "speech.transcript",
            occurredAt = segmentEndedAt,
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("transcript", text)
                .put("language", transcript.language)
                .put("context", "audio_transcript")
                .put("analysis_requested", true)
                .put("segment", blobName)
                .put("segment_started_at", segmentStartedAt)
                .put("segment_ended_at", segmentEndedAt)
                .put("duration_ms", transcript.durationMs)
                .put("engine", transcript.engine)
                // This worker always uploads PCM over HTTPS. On-device STT is intentionally not
                // selectable until an actual on-device engine exists.
                .put("raw_audio_left_device", true)
                .put("processing_location", processingLocation.wireValue)
                .put("raw_audio_cloud", rawAudioReachedCloud(processingLocation))
                .toString(),
        )
        val dao = MouchenDatabase.get(applicationContext).dao()
        dao.insertEventIfAbsent(event)
        if (!fence.isCurrent(applicationContext)) {
            dao.deleteEvent(event.id)
            return
        }
        RealtimeSyncWorker.enqueueForEvent(applicationContext, event)
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

    companion object {
        internal const val INPUT_BLOB = "blob"
        internal const val INPUT_STARTED_AT = "started_at"
        internal const val INPUT_ENDED_AT = "ended_at"
        internal const val INPUT_DESTINATION_FINGERPRINT = "destination_fingerprint"
        internal const val INPUT_AUTH_EPOCH = "auth_epoch"
        internal const val INPUT_AUTH_USER = "auth_user"
        internal const val INPUT_AUTH_ORIGIN = "auth_origin"
        const val OUTPUT_FAILURE_REASON = "failure_reason"
        internal const val UNIQUE_PREFIX = "mouchen-audio-transcription-"
        internal const val TRANSCRIPTION_TAG = "mouchen-audio-transcription"
        internal const val MAX_PENDING_TRANSCRIPTIONS = 12
    }
}

internal sealed interface AudioQueueEnqueueResult {
    data object Accepted : AudioQueueEnqueueResult
    data object CapacityFull : AudioQueueEnqueueResult
    data class ConfigurationBlocked(val reason: String) : AudioQueueEnqueueResult
    data class ManagerUnavailable(val reason: String) : AudioQueueEnqueueResult
    data class Invalid(val reason: String) : AudioQueueEnqueueResult
}

internal fun enqueueAudioTranscriptionRequest(
    context: Context,
    blobName: String,
    startedAt: Long,
    endedAt: Long,
    destinationFingerprint: String,
): AudioQueueEnqueueResult {
    if (!validAudioTranscriptionMetadata(blobName, startedAt, endedAt) ||
        !validAudioDestinationFingerprint(destinationFingerprint)
    ) {
        return AudioQueueEnqueueResult.Invalid("invalid_segment_metadata")
    }
    val appContext = context.applicationContext
    val fence = captureAuthFence(appContext)
        ?: return AudioQueueEnqueueResult.ConfigurationBlocked("authentication_required")
    val connection = runCatching {
        authenticatedConnection(appContext, BackendConnectionStore(appContext).load())
    }.getOrNull()
        ?: return AudioQueueEnqueueResult.ConfigurationBlocked("connection_store_unavailable")
    localSttConfigurationFailure(connection)?.let {
        return AudioQueueEnqueueResult.ConfigurationBlocked(it)
    }
    val workManager = runCatching { WorkManager.getInstance(appContext) }.getOrNull()
        ?: return AudioQueueEnqueueResult.ManagerUnavailable("work_manager_unavailable")
    val unfinishedCount = runCatching {
        workManager.getWorkInfosByTag(AudioTranscriptionWorker.TRANSCRIPTION_TAG)
            .get(WORK_MANAGER_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .count { !it.state.isFinished }
    }.getOrNull() ?: return AudioQueueEnqueueResult.ManagerUnavailable("queue_state_unavailable")
    if (!canQueueLocalStt(unfinishedCount, AudioTranscriptionWorker.MAX_PENDING_TRANSCRIPTIONS)) {
        return AudioQueueEnqueueResult.CapacityFull
    }

    val request = OneTimeWorkRequestBuilder<AudioTranscriptionWorker>()
        .setConstraints(
            Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build(),
        )
        .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
        .setInputData(
            workDataOf(
                AudioTranscriptionWorker.INPUT_BLOB to blobName,
                AudioTranscriptionWorker.INPUT_STARTED_AT to startedAt,
                AudioTranscriptionWorker.INPUT_ENDED_AT to endedAt,
                AudioTranscriptionWorker.INPUT_DESTINATION_FINGERPRINT to destinationFingerprint,
                AudioTranscriptionWorker.INPUT_AUTH_EPOCH to fence.epoch,
                AudioTranscriptionWorker.INPUT_AUTH_USER to fence.userId,
                AudioTranscriptionWorker.INPUT_AUTH_ORIGIN to fence.origin,
            ),
        )
        .addTag(AudioTranscriptionWorker.TRANSCRIPTION_TAG)
        .build()
    return runCatching {
        workManager.enqueueUniqueWork(
            "${AudioTranscriptionWorker.UNIQUE_PREFIX}${stableWorkSuffix(blobName)}",
            ExistingWorkPolicy.KEEP,
            request,
        ).result.get(WORK_MANAGER_OPERATION_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        AudioQueueEnqueueResult.Accepted
    }.getOrElse { AudioQueueEnqueueResult.ManagerUnavailable("enqueue_operation_failed") }
}

internal const val MAX_TOTAL_LOCAL_STT_ATTEMPTS = 5

/** runAttemptCount is zero-based, so count 4 is the fifth and final network attempt. */
internal fun canRetryLocalStt(runAttemptCount: Int, maxTotalAttempts: Int = MAX_TOTAL_LOCAL_STT_ATTEMPTS): Boolean =
    maxTotalAttempts > 0 && runAttemptCount >= 0 && runAttemptCount + 1 < maxTotalAttempts

internal fun canQueueLocalStt(unfinishedCount: Int, maxPending: Int): Boolean =
    unfinishedCount >= 0 && maxPending > 0 && unfinishedCount < maxPending

internal fun localSttConfigurationFailure(connection: BackendConnection): String? = when {
    !connection.enabled -> "backend_disabled"
    connection.bearerToken.isNullOrBlank() -> "authentication_required"
    connection.sttBaseUrl.isNullOrBlank() -> "speech_to_text_not_configured"
    connection.sttProcessingLocation == AudioProcessingLocation.ON_DEVICE -> "on_device_stt_not_implemented"
    connection.sttBaseUrl.toHttpUrlOrNull()?.scheme != "https" -> "secure_speech_to_text_url_required"
    !sameHttpsOrigin(connection.baseUrl, connection.sttBaseUrl.orEmpty()) -> "speech_to_text_origin_mismatch"
    connection.sttProcessingLocation == AudioProcessingLocation.TRUSTED_LAN &&
        !isPrivateNodeUrl(connection.sttBaseUrl.orEmpty()) -> "trusted_lan_private_address_required"
    else -> null
}

internal fun rawAudioReachedCloud(location: AudioProcessingLocation): Boolean = when (location) {
    AudioProcessingLocation.ON_DEVICE, AudioProcessingLocation.TRUSTED_LAN -> false
    AudioProcessingLocation.PRIVATE_VPS, AudioProcessingLocation.PUBLIC_CLOUD -> true
}

/** Binds encrypted audio to one non-secret destination identity without persisting the token. */
internal fun audioDestinationFingerprint(connection: BackendConnection): String? {
    val url = connection.sttBaseUrl?.trim()?.trimEnd('/')?.toHttpUrlOrNull() ?: return null
    val identity = listOf(
        url.toString().trimEnd('/'),
        connection.sttProcessingLocation.wireValue,
        connection.userId.trim(),
    ).joinToString("\n")
    return MessageDigest.getInstance("SHA-256")
        .digest(identity.toByteArray(StandardCharsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
}

internal fun validAudioDestinationFingerprint(value: String): Boolean =
    value.length == 64 && value.all { it in '0'..'9' || it in 'a'..'f' }

internal fun parseLocalTranscript(body: String): LocalTranscript {
    val json = JSONObject(body)
    return LocalTranscript(
        text = json.getString("transcript"),
        language = json.optString("language", "unknown").take(24),
        engine = json.optString("engine", "local-whisper.cpp").take(80),
        durationMs = json.optLong("duration_ms", 0L).coerceAtLeast(0L),
    )
}

internal fun stableWorkSuffix(value: String): String =
    UUID.nameUUIDFromBytes(value.toByteArray(StandardCharsets.UTF_8)).toString()

internal const val AUDIO_TRANSCRIPTION_SAMPLE_RATE = 16_000
internal const val AUDIO_TRANSCRIPTION_MAX_PLAIN_BYTES = AUDIO_TRANSCRIPTION_SAMPLE_RATE * 2 * 45
private const val MAX_TOTAL_ATTEMPTS = MAX_TOTAL_LOCAL_STT_ATTEMPTS
private const val WORK_MANAGER_OPERATION_TIMEOUT_SECONDS = 5L
private const val MAX_FAILURE_REASON_CHARACTERS = 160
private const val MAX_TRANSCRIPT_CHARACTERS = 12_000
