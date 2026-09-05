package com.mouchen.app.collectors

import android.Manifest
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.IBinder
import androidx.core.content.ContextCompat
import com.mouchen.app.BuildConfig
import com.mouchen.app.CaptureKind
import com.mouchen.app.CaptureStatusStore
import com.mouchen.app.localization.accountText
import com.mouchen.app.localization.uiText
import com.mouchen.app.audioCaptureNotificationText
import com.mouchen.app.audioTranscriptionMode
import com.mouchen.app.audioTranscriptionReady
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.security.EncryptedChunkWriter
import com.mouchen.app.security.EncryptedBlobStore
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.authenticatedConnection
import com.mouchen.app.sync.effectiveCollectionConsent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

class AudioCaptureService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val stopping = AtomicBoolean(false)
    private val recorderLock = Any()
    private val microphoneLeaseLock = Any()
    private var captureJob: Job? = null
    @Volatile private var recorder: AudioRecord? = null
    @Volatile private var microphoneLease: ProcessMicrophoneLease.Lease? = null
    private lateinit var eventWriter: BoundedEventWriter
    private lateinit var captureStatusStore: CaptureStatusStore

    override fun onCreate() {
        super.onCreate()
        captureStatusStore = CaptureStatusStore(this)
        captureStatusStore.setRunning(CaptureKind.AUDIO, false)
        eventWriter = BoundedEventWriter(applicationContext, scope, EVENT_QUEUE_CAPACITY)
        // Persisted periodic recovery is installed before capture starts, so a process death after
        // segment close cannot strand a ledger entry forever.
        runCatching { AudioTranscriptionRecoveryWorker.ensurePeriodic(applicationContext) }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopCapture()
            ACTION_START -> if (effectiveCollectionConsent(applicationContext)) {
                startCapture()
            } else {
                captureStatusStore.setRunning(CaptureKind.AUDIO, false)
                stopSelf(startId)
            }
            else -> stopSelf(startId)
        }
        return START_NOT_STICKY
    }

    private fun startCapture() {
        if (captureJob?.isActive == true) return
        if (!BuildConfig.ALLOW_SENSITIVE_CAPTURE ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED
        ) {
            stopSelf()
            return
        }
        val lease = ProcessMicrophoneLease.tryAcquire(MicrophoneUse.CONTINUOUS_CAPTURE)
        if (lease == null) {
            captureStatusStore.setRunning(CaptureKind.AUDIO, false)
            stopSelf()
            return
        }
        synchronized(microphoneLeaseLock) {
            check(microphoneLease == null) { "Microphone lease already active" }
            microphoneLease = lease
        }

        // Freeze the disclosed STT destination for the whole capture session. Changing settings
        // requires stopping and starting capture; queued segments remain bound by fingerprint.
        val transcriptionConnection = currentBackendConnection()
        val foregroundStarted = runCatching {
            CaptureNotifications.createChannel(this)
            startForeground(
                NOTIFICATION_ID,
                CaptureNotifications.ongoing(
                    this,
                    AudioCaptureService::class.java,
                    ACTION_STOP,
                    accountText("My AI Twin is recording audio", "AI替身正在录音"),
                    uiText(audioCaptureNotificationText(transcriptionConnection)),
                    NOTIFICATION_ID,
                ),
            )
        }.isSuccess
        if (!foregroundStarted) {
            releaseMicrophoneLease(lease)
            stopSelf()
            return
        }
        stopping.set(false)
        val job = scope.launch {
            try {
                recordUntilStopped(transcriptionConnection)
            } catch (_: Exception) {
                // recordUntilStopped owns cleanup; no failure may escape this service root.
            }
        }
        captureJob = job
        job.invokeOnCompletion { releaseMicrophoneLease(lease) }
    }

    private suspend fun recordUntilStopped(transcriptionConnection: BackendConnection) {
        val startedAt = System.currentTimeMillis()
        val bufferSize = maxOf(
            AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT),
            SAMPLE_RATE * 2,
        )
        val buffer = ByteArray(bufferSize)
        val blobStore = EncryptedBlobStore(this)
        var writer: EncryptedChunkWriter? = null
        var segmentIndex = 0
        var segmentStartedAt = startedAt
        var segmentBytes = 0L
        var totalBytes = 0L
        var completedSegments = 0
        var blobName = audioSegmentName(startedAt, segmentIndex)
        var unsharedRecorder: AudioRecord? = null

        suspend fun openSegment(now: Long) {
            segmentStartedAt = now
            segmentBytes = 0L
            blobName = audioSegmentName(startedAt, segmentIndex)
            writer = blobStore.openChunked(blobName)
            recordEvent(
                "audio.segment_started",
                JSONObject()
                    .put("blob", blobName)
                    .put("segment_index", segmentIndex)
                    .put("max_duration_ms", AUDIO_SEGMENT_MAX_DURATION_MS)
                    .put("max_plain_bytes", AUDIO_SEGMENT_MAX_PLAIN_BYTES),
                now,
            )
        }

        suspend fun closeSegment(now: Long): Boolean {
            val activeWriter = writer ?: return true
            writer = null
            try {
                activeWriter.close()
            } catch (_: Exception) {
                // A second best-effort close is cleanup only; the segment remains failed and is
                // never counted as completed after any close exception.
                runCatching { activeWriter.close() }
                runCatching {
                    recordEvent(
                        audioSegmentTerminalEventType(closeSucceeded = false),
                        JSONObject()
                            .put("blob", blobName)
                            .put("segment_index", segmentIndex)
                            .put("duration_ms", (now - segmentStartedAt).coerceAtLeast(0L))
                            .put("plain_bytes", segmentBytes)
                            .put("closed", false),
                        now,
                    )
                }
                return false
            }
            val transcriptionReady = effectiveCollectionConsent(applicationContext) &&
                audioTranscriptionReady(transcriptionConnection)
            val transcriptionQueued = if (
                transcriptionReady && segmentBytes >= AUDIO_TRANSCRIPTION_MIN_PLAIN_BYTES
            ) {
                runCatching {
                    registerAudioTranscriptionSegment(
                        this@AudioCaptureService,
                        blobName,
                        segmentStartedAt,
                        now,
                        audioDestinationFingerprint(transcriptionConnection).orEmpty(),
                    )
                }.getOrDefault(false)
            } else {
                false
            }
            recordEvent(
                audioSegmentTerminalEventType(closeSucceeded = true),
                JSONObject()
                    .put("blob", blobName)
                    .put("segment_index", segmentIndex)
                    .put("duration_ms", (now - segmentStartedAt).coerceAtLeast(0L))
                    .put("plain_bytes", segmentBytes)
                    .put("transcription_queued", transcriptionQueued)
                    .put("transcription_ready", transcriptionReady)
                    .put("transcription_location", audioTranscriptionMode(transcriptionConnection)),
                now,
            )
            completedSegments += 1
            return true
        }
        try {
            val localRecorder = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.MIC)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AUDIO_FORMAT)
                        .setSampleRate(SAMPLE_RATE)
                        .setChannelMask(CHANNEL_CONFIG)
                        .build(),
                )
                .setBufferSizeInBytes(bufferSize)
                .build()
            unsharedRecorder = localRecorder
            check(localRecorder.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord initialization failed" }
            synchronized(recorderLock) {
                check(recorder == null) { "AudioRecord already active" }
                recorder = localRecorder
                unsharedRecorder = null
            }
            recordEvent(
                "audio.capture_started",
                JSONObject()
                    .put("first_blob", audioSegmentName(startedAt, 0))
                    .put("sample_rate", SAMPLE_RATE)
                    .put("encoding", "pcm_16bit_mono")
                    .put("segmented", true)
                    .put("segment_max_duration_ms", AUDIO_SEGMENT_MAX_DURATION_MS)
                    .put("segment_max_plain_bytes", AUDIO_SEGMENT_MAX_PLAIN_BYTES)
                    .put("visible_foreground_service", true),
                startedAt,
            )
            openSegment(startedAt)

            localRecorder.startRecording()
            check(localRecorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                "AudioRecord did not enter recording state"
            }
            if (stopping.get()) return
            captureStatusStore.setRunning(CaptureKind.AUDIO, true)
            if (stopping.get()) captureStatusStore.setRunning(CaptureKind.AUDIO, false)
            while (scope.isActive && !stopping.get()) {
                if (!effectiveCollectionConsent(applicationContext)) break
                val count = localRecorder.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                if (count > 0) {
                    // Consent may be revoked while AudioRecord is blocked. Discard that buffer and
                    // stop before it can be persisted to the encrypted segment.
                    if (!effectiveCollectionConsent(applicationContext)) {
                        buffer.fill(0, 0, count)
                        break
                    }
                    val now = System.currentTimeMillis()
                    if (shouldRotateAudioSegment(segmentStartedAt, now, segmentBytes, count.toLong())) {
                        check(closeSegment(now)) { "Encrypted audio segment close failed" }
                        segmentIndex += 1
                        openSegment(now)
                    }
                    writer?.write(buffer, count)
                    segmentBytes += count
                    totalBytes += count
                }
                if (count == AudioRecord.ERROR_DEAD_OBJECT || count == AudioRecord.ERROR_INVALID_OPERATION) break
            }
        } catch (_: SecurityException) {
            // Permission can be revoked while the foreground service is active.
        } catch (_: IllegalStateException) {
            // Device audio hardware can become unavailable during a capture session.
        } finally {
            captureStatusStore.setRunning(CaptureKind.AUDIO, false)
            releaseRecorder(unsharedRecorder)
            unsharedRecorder = null
            releaseActiveRecorder()
            val stoppedAt = System.currentTimeMillis()
            runCatching { closeSegment(stoppedAt) }
            buffer.fill(0)
            try {
                recordEvent(
                    "audio.capture_stopped",
                    JSONObject()
                        .put("last_blob", blobName)
                        .put("duration_ms", stoppedAt - startedAt)
                        .put("segments", completedSegments)
                        .put("plain_bytes", totalBytes)
                        .put("transcription_mode", audioTranscriptionMode(transcriptionConnection)),
                )
            } catch (_: Exception) {
                // Capture shutdown must not leave a foreground notification behind.
            }
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private suspend fun recordEvent(type: String, payload: JSONObject, occurredAt: Long = System.currentTimeMillis()) {
        eventWriter.writeNow(
            LocalEventEntity(
                source = "android.microphone",
                type = type,
                occurredAt = occurredAt,
                sensitivity = "restricted",
                payloadJson = payload.toString(),
            ),
        )
    }

    private fun currentBackendConnection(): BackendConnection = runCatching {
        val stored = BackendConnectionStore(this).load()
        authenticatedConnection(this, stored) ?: stored.copy(
            userId = "",
            bearerToken = null,
            enabled = false,
        )
    }.getOrElse {
        BackendConnection(BuildConfig.DEFAULT_BACKEND_URL, enabled = false)
    }

    private fun stopCapture() {
        if (!stopping.compareAndSet(false, true)) return
        captureStatusStore.setRunning(CaptureKind.AUDIO, false)
        stopActiveRecorder()
        if (captureJob?.isActive != true) {
            if (captureJob == null) releaseCurrentMicrophoneLease()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    override fun onDestroy() {
        stopping.set(true)
        if (::captureStatusStore.isInitialized) {
            captureStatusStore.setRunning(CaptureKind.AUDIO, false)
        }
        stopActiveRecorder()
        if (::eventWriter.isInitialized) eventWriter.close()
        scope.cancel()
        if (captureJob == null) releaseCurrentMicrophoneLease()
        super.onDestroy()
    }

    private fun stopActiveRecorder() {
        synchronized(recorderLock) {
            runCatching {
                recorder?.takeIf { it.recordingState == AudioRecord.RECORDSTATE_RECORDING }?.stop()
            }
        }
    }

    private fun releaseActiveRecorder() {
        synchronized(recorderLock) {
            val activeRecorder = recorder ?: return
            recorder = null
            releaseRecorder(activeRecorder)
        }
    }

    private fun releaseRecorder(activeRecorder: AudioRecord?) {
        if (activeRecorder == null) return
        runCatching {
            if (activeRecorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) activeRecorder.stop()
        }
        runCatching { activeRecorder.release() }
    }

    private fun releaseMicrophoneLease(expected: ProcessMicrophoneLease.Lease) {
        val lease = synchronized(microphoneLeaseLock) {
            if (microphoneLease !== expected) return
            microphoneLease = null
            expected
        }
        lease.close()
    }

    private fun releaseCurrentMicrophoneLease() {
        val lease = synchronized(microphoneLeaseLock) {
            val current = microphoneLease
            microphoneLease = null
            current
        }
        lease?.close()
    }

    companion object {
        const val ACTION_START = "com.mouchen.app.action.START_AUDIO_CAPTURE"
        const val ACTION_STOP = "com.mouchen.app.action.STOP_AUDIO_CAPTURE"
        private const val NOTIFICATION_ID = 4101
        private const val SAMPLE_RATE = 16_000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private const val EVENT_QUEUE_CAPACITY = 8
    }
}

internal fun audioSegmentName(sessionStartedAt: Long, segmentIndex: Int): String =
    "audio-$sessionStartedAt-segment-${segmentIndex.toString().padStart(4, '0')}.pcm.mch"

internal fun shouldRotateAudioSegment(
    segmentStartedAt: Long,
    now: Long,
    currentPlainBytes: Long,
    nextPlainBytes: Long,
    maxDurationMs: Long = AUDIO_SEGMENT_MAX_DURATION_MS,
    maxPlainBytes: Long = AUDIO_SEGMENT_MAX_PLAIN_BYTES,
): Boolean {
    if (currentPlainBytes <= 0L) return false
    val durationReached = now - segmentStartedAt >= maxDurationMs
    val sizeReached = nextPlainBytes > maxPlainBytes - currentPlainBytes
    return durationReached || sizeReached
}

internal fun audioSegmentTerminalEventType(closeSucceeded: Boolean): String =
    if (closeSucceeded) "audio.segment_completed" else "audio.segment_failed"

private const val AUDIO_SEGMENT_MAX_DURATION_MS = 30L * 1000
private const val AUDIO_SEGMENT_MAX_PLAIN_BYTES = 1024L * 1024
private const val AUDIO_TRANSCRIPTION_MIN_PLAIN_BYTES = 16_000L * 2
