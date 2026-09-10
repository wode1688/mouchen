package com.mouchen.app.collectors

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.content.ContextCompat
import com.mouchen.app.network.MouchenDns
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.authenticatedConnection
import com.mouchen.app.sync.enforceSessionAuthorization
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.sqrt
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okio.BufferedSink

internal data class VoiceSessionKey(
    val sessionId: Long,
    val editorGeneration: Long,
)

internal data class VoiceCandidate(
    val text: String,
    val confidence: Float? = null,
)

internal data class VoiceRecognitionMetadata(
    val engine: String,
    val durationMs: Long,
    val processingLocation: String,
    val rawAudioLeftDevice: Boolean,
)

internal data class VoiceOutboundAudit(
    val phase: String,
    val engine: String,
    val processingLocation: String,
    val rawAudioLeftDevice: Boolean,
    val rawAudioReachedCloud: Boolean?,
    val payloadBytes: Int,
    val destinationFingerprint: String?,
    val reason: String? = null,
)

internal sealed interface VoiceUpdate {
    data class Starting(val engineLabel: String) : VoiceUpdate
    data class Listening(val partialText: String = "") : VoiceUpdate
    data object Processing : VoiceUpdate
    data class Results(
        val candidates: List<VoiceCandidate>,
        val metadata: VoiceRecognitionMetadata,
    ) : VoiceUpdate
    data class Error(val message: String) : VoiceUpdate
}

internal sealed interface VoiceStartResult {
    data class Started(val key: VoiceSessionKey) : VoiceStartResult
    data class Rejected(val message: String, val permissionMissing: Boolean = false) : VoiceStartResult
}

internal class VoiceSessionGate {
    private val stopping = AtomicBoolean(false)
    private val terminal = AtomicBoolean(false)

    fun requestStop(): Boolean = stopping.compareAndSet(false, true)

    fun markSpeechEnded() {
        stopping.set(true)
    }

    fun markTerminal(): Boolean = terminal.compareAndSet(false, true)

    fun canPublishProgress(): Boolean = !stopping.get() && !terminal.get()
}

/** Keeps the backend recorder's one-shot Listening transition ordered with stop/cancel. */
internal class BackendVoiceStateGate {
    private val sessionGate = VoiceSessionGate()
    private val listeningPublished = AtomicBoolean(false)

    fun markRecordingStarted(): Boolean {
        if (!sessionGate.canPublishProgress()) return false
        if (!listeningPublished.compareAndSet(false, true)) return false
        return sessionGate.canPublishProgress()
    }

    fun requestStop(): Boolean = sessionGate.requestStop()

    fun markTerminal(): Boolean = sessionGate.markTerminal()

    fun canPublishProgress(): Boolean = sessionGate.canPublishProgress()
}

internal data class VoiceUploadAuditSnapshot(
    val attempted: Boolean,
    val uploadStarted: Boolean,
    val terminal: Boolean,
    val payloadBytes: Int,
)

/** Serializes upload audit transitions and emits each transition from one immutable snapshot. */
internal class VoiceUploadAuditGate(
    private val emit: (phase: String, snapshot: VoiceUploadAuditSnapshot, reason: String?) -> Unit,
) {
    private val lock = Any()
    private var attempted = false
    private var uploadStarted = false
    private var terminal = false
    private var payloadBytes = 0

    fun markAttempted(bytes: Int): Boolean = synchronized(lock) {
        if (attempted || terminal) return@synchronized false
        attempted = true
        payloadBytes = bytes.coerceAtLeast(0)
        emit("attempted", snapshot(), null)
        true
    }

    fun markUploadStarted(): Boolean = synchronized(lock) {
        if (!attempted || uploadStarted || terminal) return@synchronized false
        uploadStarted = true
        emit("request_body_started", snapshot(), null)
        true
    }

    fun markTerminal(phase: String, reason: String? = null): Boolean = synchronized(lock) {
        if (terminal) return@synchronized false
        terminal = true
        if (attempted) emit(phase, snapshot(), reason)
        true
    }

    private fun snapshot() = VoiceUploadAuditSnapshot(
        attempted = attempted,
        uploadStarted = uploadStarted,
        terminal = terminal,
        payloadBytes = payloadBytes,
    )
}

internal fun normalizeVoiceCandidates(
    rawResults: List<String>?,
    confidenceScores: FloatArray? = null,
    limit: Int = MAX_VOICE_RESULTS,
): List<VoiceCandidate> {
    if (limit <= 0) return emptyList()
    val seen = LinkedHashSet<String>()
    val normalized = ArrayList<VoiceCandidate>(limit)
    rawResults.orEmpty().forEachIndexed { index, raw ->
        val text = raw.trim().replace(VOICE_WHITESPACE, " ").take(MAX_VOICE_TEXT_CHARS)
        if (text.isEmpty() || !seen.add(text)) return@forEachIndexed
        val confidence = confidenceScores
            ?.getOrNull(index)
            ?.takeIf { it.isFinite() && it in 0f..1f }
        normalized += VoiceCandidate(text, confidence)
        if (normalized.size >= limit) return normalized
    }
    return normalized
}

internal fun voiceBackendErrorMessage(reason: String): String = when (reason) {
    "backend_disabled" -> "AI替身连接已停用，请先在连接页启用"
    "authentication_required" -> "请先在AI替身连接页配置授权"
    "speech_to_text_not_configured" -> "请先在AI替身连接页配置语音识别地址"
    "secure_speech_to_text_url_required" -> "语音识别地址必须使用 HTTPS"
    "trusted_lan_private_address_required" -> "可信局域网语音地址必须是私有地址"
    "on_device_stt_not_implemented" -> "当前设备未安装可用的端侧普通话模型"
    "voice_stt_http_429" -> "语音识别服务正忙，请稍后重试"
    "voice_stt_unavailable_503" -> "服务器语音识别尚未启用"
    "voice_stt_http_401", "voice_stt_http_403" -> "语音识别授权无效，请检查连接"
    "voice_stt_unreachable" -> "无法连接语音识别服务，请检查网络"
    "voice_stt_cancelled" -> "语音识别已取消"
    "voice_stt_empty" -> "没有听清，请重说一次"
    "voice_stt_invalid_response" -> "语音识别结果异常，请重试"
    else -> "语音识别失败，请重试"
}

internal fun runVoiceCandidateCommit(
    commit: () -> Boolean,
    clearResults: () -> Unit,
): Boolean {
    if (!commit()) return false
    clearResults()
    return true
}

internal fun systemSpeechErrorMessage(error: Int): String = when (error) {
    SpeechRecognizer.ERROR_AUDIO -> "麦克风暂时不可用，请重试"
    SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "请先授予AI替身麦克风权限"
    SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "语音识别网络不可用"
    SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "没有听清，请重说一次"
    SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "语音识别正忙，请稍后重试"
    SpeechRecognizer.ERROR_SERVER, SpeechRecognizer.ERROR_SERVER_DISCONNECTED -> "语音识别服务暂时不可用"
    SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED, SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE ->
        "当前语音服务不支持普通话"
    else -> "语音识别失败，请重试"
}

/**
 * One short Mandarin dictation session. The explicitly configured Mouchen STT endpoint is used
 * first; an Android recognizer is eligible only when the OS confirms a fully on-device service.
 * Every callback carries the editor generation that created it.
 */
internal class ImeVoiceController(
    private val context: Context,
    private val scope: CoroutineScope,
    private val onUpdate: (VoiceSessionKey, VoiceUpdate) -> Unit,
    private val onOutboundAudit: (VoiceSessionKey, VoiceOutboundAudit) -> Unit = { _, _ -> },
) {
    private var nextSessionId = 0L
    private var activeSession: ActiveSession? = null

    fun start(editorGeneration: Long, biasingStrings: List<String> = emptyList()): VoiceStartResult {
        cancel()
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return VoiceStartResult.Rejected("请先授予AI替身麦克风权限", permissionMissing = true)
        }

        val connection = runCatching {
            authenticatedConnection(context, BackendConnectionStore(context).load())
        }.getOrNull()
        val backendFailure = if (connection == null) {
            "connection_store_unavailable"
        } else {
            localSttConfigurationFailure(connection)
        }
        val useBackend = connection != null && backendFailure == null
        val useSystem = !useBackend && onDeviceRecognizerAvailable()
        if (!useBackend && !useSystem) {
            val message = if (backendFailure == "connection_store_unavailable") {
                "无法读取AI替身语音连接配置"
            } else {
                voiceBackendErrorMessage(requireNotNull(backendFailure))
            }
            return VoiceStartResult.Rejected(message)
        }
        val lease = ProcessMicrophoneLease.tryAcquire(MicrophoneUse.IME_DICTATION)
            ?: return VoiceStartResult.Rejected(
                "${ProcessMicrophoneLease.currentUse()?.label ?: "其他功能"}正在占用麦克风，请停止后重试",
            )
        val key = VoiceSessionKey(++nextSessionId, editorGeneration)
        return if (useBackend) {
            startBackendRecognizer(key, requireNotNull(connection), lease)
        } else {
            startSystemRecognizer(key, biasingStrings, lease)
        }
    }

    fun stop(): Boolean {
        val current = activeSession ?: return false
        if (!current.requestStop()) return false
        dispatch(current.key, VoiceUpdate.Processing)
        if (current is SystemSession && activeSession === current) {
            current.scheduleFinalTimeout(scope) {
                failSystemSession(current.key, "语音服务没有返回结果，请重试", "final_timeout")
            }
        }
        return true
    }

    fun cancel() {
        val current = activeSession ?: return
        activeSession = null
        nextSessionId += 1
        current.cancel()
    }

    fun isActive(): Boolean = activeSession != null

    private fun startSystemRecognizer(
        key: VoiceSessionKey,
        biasingStrings: List<String>,
        microphoneLease: ProcessMicrophoneLease.Lease,
    ): VoiceStartResult {
        val recognizer = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            runCatching { SpeechRecognizer.createOnDeviceSpeechRecognizer(context) }.getOrNull()
        } else {
            null
        }
            ?: run {
                microphoneLease.close()
                return VoiceStartResult.Rejected("当前设备没有可用的端侧普通话语音服务")
            }
        val session = SystemSession(key, recognizer, microphoneLease)
        activeSession = session
        val listenerInstalled = runCatching {
            recognizer.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                if (session.markListening()) dispatch(key, VoiceUpdate.Listening())
            }

            override fun onBeginningOfSpeech() {
                if (session.markListening()) dispatch(key, VoiceUpdate.Listening())
            }

            override fun onRmsChanged(rmsdB: Float) = Unit

            override fun onBufferReceived(buffer: ByteArray?) = Unit

            override fun onEndOfSpeech() {
                session.markSpeechEnded()
                dispatch(key, VoiceUpdate.Processing)
                session.scheduleFinalTimeout(scope) {
                    failSystemSession(key, "语音服务没有返回结果，请重试", "final_timeout")
                }
            }

            override fun onError(error: Int) {
                if (!session.markTerminal()) return
                complete(key, VoiceUpdate.Error(systemSpeechErrorMessage(error)))
            }

            override fun onResults(results: Bundle?) {
                val candidates = voiceCandidatesFromBundle(results)
                if (candidates.isEmpty()) {
                    if (!session.markTerminal()) return
                    complete(key, VoiceUpdate.Error("没有听清，请重说一次"))
                    return
                }
                if (!session.markTerminal()) return
                complete(
                    key,
                    VoiceUpdate.Results(
                        candidates = candidates,
                        metadata = VoiceRecognitionMetadata(
                            engine = "android-on-device-speech-recognizer",
                            durationMs = 0L,
                            processingLocation = "on_device",
                            rawAudioLeftDevice = false,
                        ),
                    ),
                )
            }

            override fun onPartialResults(partialResults: Bundle?) {
                val partial = voiceCandidatesFromBundle(partialResults).firstOrNull()?.text.orEmpty()
                if (partial.isNotEmpty() && session.canPublishProgress()) {
                    dispatch(key, VoiceUpdate.Listening(partial))
                }
            }

            override fun onEvent(eventType: Int, params: Bundle?) = Unit
            })
        }.isSuccess
        if (!listenerInstalled) {
            if (activeSession === session) activeSession = null
            session.cancel()
            return VoiceStartResult.Rejected("无法初始化端侧普通话识别，请重试")
        }

        // Starting must precede startListening: some vendor recognizers invoke ready/partial
        // callbacks synchronously and must never regress the UI back to Starting.
        dispatch(key, VoiceUpdate.Starting("端侧普通话"))
        val started = runCatching {
            recognizer.startListening(mandarinRecognizerIntent(biasingStrings))
        }.isSuccess
        if (!started) {
            if (activeSession === session) activeSession = null
            session.cancel()
            return VoiceStartResult.Rejected("无法启动端侧普通话识别，请重试")
        }
        if (activeSession === session) {
            session.scheduleReadyTimeout(scope) {
                failSystemSession(key, "语音服务启动超时，请重试", "ready_timeout")
            }
            session.scheduleMaximumDuration(scope) {
                failSystemSession(key, "单次语音最长 20 秒，请重试", "maximum_duration")
            }
        }
        return VoiceStartResult.Started(key)
    }

    private fun startBackendRecognizer(
        key: VoiceSessionKey,
        connection: BackendConnection,
        microphoneLease: ProcessMicrophoneLease.Lease,
    ): VoiceStartResult {
        val recorder = ShortMandarinRecorder(microphoneLease)
        val sttClient = ImeVoiceSttClient(connection, buildImeVoiceHttpClient(context))
        val session = BackendSession(key, recorder, sttClient, connection, onOutboundAudit)
        activeSession = session
        dispatch(key, VoiceUpdate.Starting("AI替身普通话"))
        session.job = scope.launch(Dispatchers.IO) {
            try {
                when (
                    val recording = recorder.capture {
                        if (session.markRecordingStarted()) {
                            dispatchOnMain(key, VoiceUpdate.Listening())
                        }
                    }
                ) {
                is VoiceCaptureResult.Completed -> {
                    val pcm = recording.pcm
                    try {
                        dispatchOnMain(key, VoiceUpdate.Processing)
                        if (!session.markOutboundAttempted(pcm.size)) return@launch
                        when (val result = sttClient.transcribe(pcm, session::markUploadStarted)) {
                            is VoiceSttResult.Completed -> {
                                session.markTerminal("completed")
                                val candidates = normalizeVoiceCandidates(listOf(result.transcript.text))
                                if (candidates.isEmpty()) {
                                    completeOnMain(key, VoiceUpdate.Error(voiceBackendErrorMessage("voice_stt_empty")))
                                } else {
                                    completeOnMain(
                                        key,
                                        VoiceUpdate.Results(
                                            candidates = candidates,
                                            metadata = VoiceRecognitionMetadata(
                                                engine = result.transcript.engine,
                                                durationMs = result.transcript.durationMs,
                                                processingLocation = connection.sttProcessingLocation.wireValue,
                                                rawAudioLeftDevice = true,
                                            ),
                                        ),
                                    )
                                }
                            }
                            is VoiceSttResult.Failed -> {
                                session.markTerminal("failed", result.reason)
                                completeOnMain(key, VoiceUpdate.Error(voiceBackendErrorMessage(result.reason)))
                            }
                            VoiceSttResult.Cancelled -> Unit
                        }
                    } finally {
                        pcm.fill(0)
                    }
                }
                VoiceCaptureResult.NoSpeech -> completeOnMain(
                    key,
                    VoiceUpdate.Error("没有检测到普通话，请靠近麦克风重试"),
                )
                is VoiceCaptureResult.Failed -> completeOnMain(
                    key,
                    VoiceUpdate.Error(recording.message),
                )
                VoiceCaptureResult.Cancelled -> Unit
                }
            } catch (_: CancellationException) {
                // Lifecycle cancellation is terminal and is audited by BackendSession.cancel().
            } catch (_: Throwable) {
                session.markTerminal("failed", "voice_pipeline_error")
                completeOnMain(key, VoiceUpdate.Error("语音识别失败，请重试"))
            }
        }
        return VoiceStartResult.Started(key)
    }

    private fun onDeviceRecognizerAvailable(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && runCatching {
        SpeechRecognizer.isOnDeviceRecognitionAvailable(context)
    }.getOrDefault(false)

    private fun dispatch(key: VoiceSessionKey, update: VoiceUpdate) {
        val session = activeSession ?: return
        if (session.key != key || !session.allows(update)) return
        onUpdate(key, update)
    }

    private suspend fun dispatchOnMain(key: VoiceSessionKey, update: VoiceUpdate) {
        withContext(Dispatchers.Main.immediate) { dispatch(key, update) }
    }

    private fun complete(key: VoiceSessionKey, update: VoiceUpdate) {
        val session = activeSession ?: return
        if (session.key != key) return
        activeSession = null
        session.finish()
        onUpdate(key, update)
    }

    private suspend fun completeOnMain(key: VoiceSessionKey, update: VoiceUpdate) {
        withContext(Dispatchers.Main.immediate) { complete(key, update) }
    }

    private fun failSystemSession(key: VoiceSessionKey, message: String, reason: String) {
        val session = activeSession as? SystemSession ?: return
        if (session.key != key) return
        if (!session.markTerminal()) return
        complete(key, VoiceUpdate.Error(message))
    }

    private sealed interface ActiveSession {
        val key: VoiceSessionKey
        fun allows(update: VoiceUpdate): Boolean = true
        fun requestStop(): Boolean
        fun cancel()
        fun finish()
    }

    private class SystemSession(
        override val key: VoiceSessionKey,
        private val recognizer: SpeechRecognizer,
        private val microphoneLease: ProcessMicrophoneLease.Lease,
    ) : ActiveSession {
        private val gate = VoiceSessionGate()
        private val readySeen = AtomicBoolean(false)
        private var readyTimeout: Job? = null
        private var maximumDurationTimeout: Job? = null
        private var finalTimeout: Job? = null

        override fun allows(update: VoiceUpdate): Boolean = when (update) {
            is VoiceUpdate.Starting, is VoiceUpdate.Listening -> gate.canPublishProgress()
            else -> true
        }

        fun canPublishProgress(): Boolean = gate.canPublishProgress()

        fun markListening(): Boolean {
            readySeen.set(true)
            readyTimeout?.cancel()
            return canPublishProgress()
        }

        fun markSpeechEnded() {
            gate.markSpeechEnded()
        }

        override fun requestStop(): Boolean {
            if (!gate.requestStop()) return false
            readyTimeout?.cancel()
            runCatching { recognizer.stopListening() }
            return true
        }

        fun scheduleReadyTimeout(scope: CoroutineScope, onTimeout: () -> Unit) {
            if (readySeen.get()) return
            val timeout = scope.launch {
                delay(SYSTEM_READY_TIMEOUT_MS)
                onTimeout()
            }
            readyTimeout = timeout
            if (readySeen.get()) timeout.cancel()
        }

        fun scheduleMaximumDuration(scope: CoroutineScope, onTimeout: () -> Unit) {
            maximumDurationTimeout = scope.launch {
                delay(SYSTEM_MAXIMUM_DURATION_MS)
                onTimeout()
            }
        }

        fun scheduleFinalTimeout(scope: CoroutineScope, onTimeout: () -> Unit) {
            finalTimeout?.cancel()
            finalTimeout = scope.launch {
                delay(SYSTEM_FINAL_TIMEOUT_MS)
                onTimeout()
            }
        }

        fun markTerminal(): Boolean = gate.markTerminal()

        override fun cancel() {
            gate.markTerminal()
            cancelTimeouts()
            runCatching { recognizer.cancel() }
            runCatching { recognizer.destroy() }
            microphoneLease.close()
        }

        override fun finish() {
            cancelTimeouts()
            runCatching { recognizer.destroy() }
            microphoneLease.close()
        }

        private fun cancelTimeouts() {
            readyTimeout?.cancel()
            maximumDurationTimeout?.cancel()
            finalTimeout?.cancel()
        }

    }

    private class BackendSession(
        override val key: VoiceSessionKey,
        private val recorder: ShortMandarinRecorder,
        private val sttClient: ImeVoiceSttClient,
        private val connection: BackendConnection,
        private val onOutboundAudit: (VoiceSessionKey, VoiceOutboundAudit) -> Unit,
    ) : ActiveSession {
        var job: Job? = null
        private val stateGate = BackendVoiceStateGate()
        private val auditGate = VoiceUploadAuditGate { phase, snapshot, reason ->
            onOutboundAudit(key, outboundAudit(phase, snapshot, reason))
        }

        override fun allows(update: VoiceUpdate): Boolean = when (update) {
            is VoiceUpdate.Starting, is VoiceUpdate.Listening -> stateGate.canPublishProgress()
            else -> true
        }

        override fun requestStop(): Boolean {
            if (!stateGate.requestStop()) return false
            recorder.requestStop()
            return true
        }

        fun markRecordingStarted(): Boolean = stateGate.markRecordingStarted()

        fun markOutboundAttempted(bytes: Int): Boolean = auditGate.markAttempted(bytes)

        fun markUploadStarted(): Boolean = auditGate.markUploadStarted()

        fun markTerminal(phase: String, reason: String? = null) {
            stateGate.markTerminal()
            auditGate.markTerminal(phase, reason)
        }

        override fun cancel() {
            stateGate.markTerminal()
            recorder.cancel()
            sttClient.cancel()
            job?.cancel()
            markTerminal("cancelled", "user_or_lifecycle_cancelled")
        }

        override fun finish() {
            recorder.finish()
        }

        private fun outboundAudit(
            phase: String,
            snapshot: VoiceUploadAuditSnapshot,
            reason: String? = null,
        ) = VoiceOutboundAudit(
            phase = phase,
            engine = "mouchen-whisper",
            processingLocation = connection.sttProcessingLocation.wireValue,
            rawAudioLeftDevice = snapshot.uploadStarted,
            rawAudioReachedCloud = snapshot.uploadStarted && rawAudioReachedCloud(connection.sttProcessingLocation),
            payloadBytes = snapshot.payloadBytes,
            destinationFingerprint = audioDestinationFingerprint(connection),
            reason = reason,
        )
    }
}

private fun systemSpeechReason(error: Int): String = when (error) {
    SpeechRecognizer.ERROR_AUDIO -> "audio_error"
    SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "permission_denied"
    SpeechRecognizer.ERROR_NETWORK -> "network_error"
    SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "network_timeout"
    SpeechRecognizer.ERROR_NO_MATCH -> "no_match"
    SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "speech_timeout"
    SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "recognizer_busy"
    SpeechRecognizer.ERROR_SERVER -> "server_error"
    SpeechRecognizer.ERROR_SERVER_DISCONNECTED -> "server_disconnected"
    SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED -> "language_not_supported"
    SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> "language_unavailable"
    else -> "recognizer_error_$error"
}

private fun voiceCandidatesFromBundle(bundle: Bundle?): List<VoiceCandidate> = normalizeVoiceCandidates(
    bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION),
    bundle?.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES),
)

private fun mandarinRecognizerIntent(biasingStrings: List<String>): Intent =
    Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, MANDARIN_LANGUAGE_TAG)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, MANDARIN_LANGUAGE_TAG)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, MAX_VOICE_RESULTS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val hints = biasingStrings.map(String::trim).filter(String::isNotEmpty).distinct().take(MAX_BIASING_STRINGS)
            if (hints.isNotEmpty()) putStringArrayListExtra(RecognizerIntent.EXTRA_BIASING_STRINGS, ArrayList(hints))
            putExtra(RecognizerIntent.EXTRA_ENABLE_BIASING_DEVICE_CONTEXT, true)
            putExtra(RecognizerIntent.EXTRA_ENABLE_FORMATTING, RecognizerIntent.FORMATTING_OPTIMIZE_QUALITY)
        }
    }

internal sealed interface VoiceCaptureResult {
    data class Completed(val pcm: ByteArray, val durationMs: Long) : VoiceCaptureResult
    data object NoSpeech : VoiceCaptureResult
    data class Failed(val message: String) : VoiceCaptureResult
    data object Cancelled : VoiceCaptureResult
}

internal class MandarinSilenceDetector(
    private val sampleRate: Int = VOICE_SAMPLE_RATE,
    private val speechRmsThreshold: Int = 350,
    private val speechPeakThreshold: Int = 1_200,
    private val minimumSpeechMs: Long = 220,
    private val endingSilenceMs: Long = 1_100,
    private val noSpeechTimeoutMs: Long = 7_000,
) {
    private var totalMs = 0L
    private var voicedMs = 0L
    private var silenceAfterVoiceMs = 0L

    fun accept(buffer: ByteArray, count: Int): VoiceFrameDecision {
        if (count < 2) return VoiceFrameDecision.CONTINUE
        val completeBytes = count - (count % 2)
        var squareSum = 0.0
        var peak = 0
        var index = 0
        while (index < completeBytes) {
            val sample = ((buffer[index].toInt() and 0xff) or (buffer[index + 1].toInt() shl 8)).toShort().toInt()
            val magnitude = kotlin.math.abs(sample)
            peak = maxOf(peak, magnitude)
            squareSum += sample.toDouble() * sample.toDouble()
            index += 2
        }
        val sampleCount = completeBytes / 2
        val frameMs = (sampleCount.toLong() * 1_000L / sampleRate).coerceAtLeast(1L)
        totalMs += frameMs
        val rms = sqrt(squareSum / sampleCount).toInt()
        val voiced = rms >= speechRmsThreshold || peak >= speechPeakThreshold
        if (voiced) {
            voicedMs += frameMs
            silenceAfterVoiceMs = 0L
        } else if (voicedMs >= minimumSpeechMs) {
            silenceAfterVoiceMs += frameMs
        }
        return when {
            voicedMs >= minimumSpeechMs && silenceAfterVoiceMs >= endingSilenceMs -> VoiceFrameDecision.STOP_WITH_SPEECH
            voicedMs < minimumSpeechMs && totalMs >= noSpeechTimeoutMs -> VoiceFrameDecision.STOP_NO_SPEECH
            else -> VoiceFrameDecision.CONTINUE
        }
    }

    fun hasSpeech(): Boolean = voicedMs >= minimumSpeechMs
}

internal enum class VoiceFrameDecision {
    CONTINUE,
    STOP_WITH_SPEECH,
    STOP_NO_SPEECH,
}

internal class ShortMandarinRecorder(
    private val microphoneLease: ProcessMicrophoneLease.Lease,
) {
    private val stopRequested = AtomicBoolean(false)
    private val cancelled = AtomicBoolean(false)
    private val captureStarted = AtomicBoolean(false)
    private val recorder = AtomicReference<AudioRecord?>(null)
    private val stateLock = Any()

    @SuppressLint("MissingPermission")
    suspend fun capture(onRecordingStarted: suspend () -> Unit = {}): VoiceCaptureResult {
        captureStarted.set(true)
        return try {
            withContext(Dispatchers.IO) { captureOnIoThread(onRecordingStarted) }
        } finally {
            microphoneLease.close()
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun captureOnIoThread(onRecordingStarted: suspend () -> Unit): VoiceCaptureResult {
        if (cancelled.get()) return VoiceCaptureResult.Cancelled
        if (stopRequested.get()) return VoiceCaptureResult.NoSpeech
        currentCoroutineContext().ensureActive()
        val localRecorder = createAudioRecord()
            ?: return VoiceCaptureResult.Failed("无法启动麦克风，请检查是否被其他应用占用")
        val buffer = ByteArray(VOICE_READ_BUFFER_BYTES)
        val accumulator = PcmAccumulator(VOICE_MAX_PCM_BYTES)
        val detector = MandarinSilenceDetector()
        val startedAt = SystemClock.elapsedRealtime()
        val deadlineAt = startedAt + VOICE_MAX_DURATION_MS
        var noSpeech = false
        var failure: String? = null
        var noiseSuppressor: NoiseSuppressor? = null
        var gainControl: AutomaticGainControl? = null
        try {
            synchronized(stateLock) {
                if (cancelled.get() || stopRequested.get()) return@synchronized
                recorder.set(localRecorder)
                if (NoiseSuppressor.isAvailable()) {
                    noiseSuppressor = runCatching { NoiseSuppressor.create(localRecorder.audioSessionId) }.getOrNull()
                }
                if (AutomaticGainControl.isAvailable()) {
                    gainControl = runCatching { AutomaticGainControl.create(localRecorder.audioSessionId) }.getOrNull()
                }
                localRecorder.startRecording()
            }
            if (cancelled.get()) return VoiceCaptureResult.Cancelled
            if (stopRequested.get()) return VoiceCaptureResult.NoSpeech
            if (localRecorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                if (cancelled.get()) return VoiceCaptureResult.Cancelled
                if (stopRequested.get()) return VoiceCaptureResult.NoSpeech
                return VoiceCaptureResult.Failed("麦克风没有开始录音，请重试")
            }
            onRecordingStarted()
            while (!cancelled.get() && !stopRequested.get() && accumulator.size < VOICE_MAX_PCM_BYTES) {
                currentCoroutineContext().ensureActive()
                val now = SystemClock.elapsedRealtime()
                if (now >= deadlineAt) break
                if (!detector.hasSpeech() && now - startedAt >= VOICE_NO_SPEECH_WALL_TIMEOUT_MS) {
                    noSpeech = true
                    break
                }
                val count = localRecorder.read(buffer, 0, buffer.size, AudioRecord.READ_NON_BLOCKING)
                when {
                    count > 0 -> {
                        accumulator.append(buffer, count)
                        when (detector.accept(buffer, count)) {
                            VoiceFrameDecision.STOP_WITH_SPEECH -> break
                            VoiceFrameDecision.STOP_NO_SPEECH -> {
                                noSpeech = true
                                break
                            }
                            VoiceFrameDecision.CONTINUE -> Unit
                        }
                    }
                    count == 0 -> delay(VOICE_EMPTY_READ_DELAY_MS)
                    else -> {
                        if (!stopRequested.get() && !cancelled.get()) failure = "麦克风录音中断，请重试"
                        break
                    }
                }
            }
        } catch (cancelledError: CancellationException) {
            accumulator.clear()
            throw cancelledError
        } catch (_: SecurityException) {
            failure = "麦克风权限已被撤销"
        } catch (_: IllegalStateException) {
            failure = "麦克风暂时不可用，请重试"
        } catch (_: Throwable) {
            failure = "麦克风录音失败，请重试"
        } finally {
            synchronized(stateLock) {
                runCatching {
                    if (localRecorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) localRecorder.stop()
                }
                recorder.compareAndSet(localRecorder, null)
            }
            runCatching { noiseSuppressor?.release() }
            runCatching { gainControl?.release() }
            runCatching { localRecorder.release() }
            buffer.fill(0)
        }

        return when {
            cancelled.get() -> {
                accumulator.clear()
                VoiceCaptureResult.Cancelled
            }
            failure != null -> {
                accumulator.clear()
                VoiceCaptureResult.Failed(failure)
            }
            noSpeech || !detector.hasSpeech() -> {
                accumulator.clear()
                VoiceCaptureResult.NoSpeech
            }
            else -> {
                val pcm = accumulator.takeBytes()
                VoiceCaptureResult.Completed(pcm, SystemClock.elapsedRealtime() - startedAt)
            }
        }
    }

    fun requestStop() {
        stopRequested.set(true)
        stopActiveRecorder()
    }

    fun cancel() {
        cancelled.set(true)
        stopRequested.set(true)
        stopActiveRecorder()
        if (!captureStarted.get()) microphoneLease.close()
    }

    fun finish() {
        if (!captureStarted.get()) microphoneLease.close()
    }

    private fun stopActiveRecorder() {
        synchronized(stateLock) {
            runCatching {
                recorder.get()?.takeIf { it.recordingState == AudioRecord.RECORDSTATE_RECORDING }?.stop()
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun createAudioRecord(): AudioRecord? {
        val minimum = AudioRecord.getMinBufferSize(
            VOICE_SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minimum <= 0) return null
        val bufferSize = maxOf(minimum * 2, VOICE_READ_BUFFER_BYTES * 2)
        for (source in intArrayOf(MediaRecorder.AudioSource.VOICE_RECOGNITION, MediaRecorder.AudioSource.MIC)) {
            val candidate = runCatching {
                AudioRecord.Builder()
                    .setAudioSource(source)
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(VOICE_SAMPLE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .build(),
                    )
                    .setBufferSizeInBytes(bufferSize)
                    .build()
            }.getOrNull() ?: continue
            if (candidate.state == AudioRecord.STATE_INITIALIZED) return candidate
            runCatching { candidate.release() }
        }
        return null
    }
}

private class PcmAccumulator(private val capacity: Int) {
    private val bytes = ByteArray(capacity)
    var size: Int = 0
        private set

    fun append(source: ByteArray, count: Int) {
        val accepted = minOf(count.coerceAtLeast(0), capacity - size)
        if (accepted <= 0) return
        source.copyInto(bytes, destinationOffset = size, startIndex = 0, endIndex = accepted)
        size += accepted
    }

    fun takeBytes(): ByteArray = bytes.copyOf(size).also { clear() }

    fun clear() {
        bytes.fill(0)
        size = 0
    }
}

private sealed interface VoiceSttResult {
    data class Completed(val transcript: LocalTranscript) : VoiceSttResult
    data class Failed(val reason: String) : VoiceSttResult
    data object Cancelled : VoiceSttResult
}

private class ImeVoiceSttClient(
    private val connection: BackendConnection,
    private val client: OkHttpClient,
) {
    private val activeCall = AtomicReference<Call?>(null)
    private val cancelled = AtomicBoolean(false)

    fun transcribe(pcm: ByteArray, onUploadStarted: () -> Boolean): VoiceSttResult {
        if (cancelled.get()) return VoiceSttResult.Cancelled
        if (pcm.isEmpty()) return VoiceSttResult.Failed("voice_stt_empty")
        val configurationFailure = localSttConfigurationFailure(connection)
        if (configurationFailure != null) return VoiceSttResult.Failed(configurationFailure)
        val url = connection.sttBaseUrl!!.trimEnd('/').toHttpUrlOrNull()
            ?.newBuilder()
            ?.addPathSegments("v1/audio/transcribe")
            ?.setQueryParameter("sample_rate", VOICE_SAMPLE_RATE.toString())
            ?.setQueryParameter("language", "zh")
            ?.build()
            ?: return VoiceSttResult.Failed("secure_speech_to_text_url_required")
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer ${connection.bearerToken!!.trim()}")
            .header("X-User-Id", connection.userId)
            .header("X-Segment-Id", "ime-${UUID.randomUUID()}")
            .header("X-Local-STT-Only", "true")
            .header("X-Voice-Input", "true")
            .post(PcmRequestBody(pcm, onUploadStarted))
            .build()
        val call = client.newCall(request)
        if (!activeCall.compareAndSet(null, call)) {
            call.cancel()
            return VoiceSttResult.Cancelled
        }
        if (cancelled.get()) {
            activeCall.compareAndSet(call, null)
            call.cancel()
            return VoiceSttResult.Cancelled
        }
        return try {
            call.execute().use { response ->
                val body = readBoundedResponseBody(response.body)
                    ?: return VoiceSttResult.Failed("voice_stt_invalid_response")
                if (response.isSuccessful) {
                    val transcript = runCatching { parseLocalTranscript(body) }.getOrNull()
                        ?: return VoiceSttResult.Failed("voice_stt_invalid_response")
                    VoiceSttResult.Completed(transcript)
                } else {
                    val reason = when (response.code) {
                        429 -> "voice_stt_http_429"
                        503 -> "voice_stt_unavailable_503"
                        else -> "voice_stt_http_${response.code}"
                    }
                    VoiceSttResult.Failed(reason)
                }
            }
        } catch (_: java.io.IOException) {
            if (call.isCanceled()) VoiceSttResult.Cancelled else VoiceSttResult.Failed("voice_stt_unreachable")
        } catch (_: Exception) {
            VoiceSttResult.Failed("voice_stt_client_error")
        } finally {
            activeCall.compareAndSet(call, null)
        }
    }

    fun cancel() {
        cancelled.set(true)
        activeCall.getAndSet(null)?.cancel()
    }

}

internal class PcmRequestBody(
    private val pcm: ByteArray,
    private val onUploadStarted: () -> Boolean,
) : RequestBody() {
    override fun contentType() = VOICE_PCM_MEDIA_TYPE

    override fun contentLength(): Long = pcm.size.toLong()

    override fun writeTo(sink: BufferedSink) {
        if (!onUploadStarted()) throw java.io.IOException("Voice upload cancelled")
        sink.write(pcm)
    }
}

private fun readBoundedResponseBody(body: okhttp3.ResponseBody?): String? {
    if (body == null || body.contentLength() > MAX_STT_RESPONSE_CHARS) return null
    return body.charStream().use { reader ->
        val result = StringBuilder()
        val chars = CharArray(4_096)
        while (true) {
            val count = reader.read(chars)
            if (count < 0) break
            if (result.length + count > MAX_STT_RESPONSE_CHARS) return null
            result.append(chars, 0, count)
        }
        result.toString()
    }
}

internal fun buildImeVoiceHttpClient(context: Context? = null): OkHttpClient {
    val builder = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(8, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .callTimeout(20, TimeUnit.SECONDS)
    if (context != null) builder.enforceSessionAuthorization(context.applicationContext)
    return builder.build()
}

private const val MANDARIN_LANGUAGE_TAG = "zh-CN"
private const val VOICE_SAMPLE_RATE = 16_000
private const val VOICE_MAX_SECONDS = 20
private const val VOICE_MAX_DURATION_MS = VOICE_MAX_SECONDS * 1_000L
private const val VOICE_NO_SPEECH_WALL_TIMEOUT_MS = 7_000L
private const val VOICE_EMPTY_READ_DELAY_MS = 10L
private const val VOICE_MAX_PCM_BYTES = VOICE_SAMPLE_RATE * 2 * VOICE_MAX_SECONDS
private const val VOICE_READ_BUFFER_BYTES = 3_200
private const val MAX_VOICE_RESULTS = 5
private const val MAX_BIASING_STRINGS = 20
private const val MAX_VOICE_TEXT_CHARS = 1_000
private const val MAX_STT_RESPONSE_CHARS = 64 * 1_024
private const val SYSTEM_READY_TIMEOUT_MS = 8_000L
private const val SYSTEM_MAXIMUM_DURATION_MS = 22_000L
private const val SYSTEM_FINAL_TIMEOUT_MS = 6_000L
private val VOICE_WHITESPACE = Regex("\\s+")
private val VOICE_PCM_MEDIA_TYPE = "audio/L16; rate=16000; channels=1".toMediaType()
