package com.mouchen.app.collectors

import android.app.Activity
import android.app.KeyguardManager
import android.app.Service
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.view.WindowManager
import com.mouchen.app.BuildConfig
import com.mouchen.app.CaptureKind
import com.mouchen.app.CaptureStatusStore
import com.mouchen.app.localization.accountText
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.security.EncryptedBlobStore
import com.mouchen.app.sync.effectiveCollectionConsent
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.roundToInt

class ScreenCaptureService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val stopping = AtomicBoolean(false)
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var captureThread: HandlerThread? = null
    private var lastCaptureAt = 0L
    private var lastGuardCheckAt = 0L
    private var sessionStartedAt = 0L
    private var captureCount = 0
    private var lastFrameAuditAt = 0L
    private val ocrInFlight = AtomicBoolean(false)
    private val activeOcrStartedAt = AtomicLong(0L)
    private val ocrTextGate = ScreenOcrTextGate()
    private val ocrDiagnosticLimiter = ScreenOcrDiagnosticLimiter()
    private lateinit var eventWriter: BoundedEventWriter
    private lateinit var blobStore: EncryptedBlobStore
    private lateinit var privacyGuard: ScreenCapturePrivacyGuard
    private lateinit var captureStatusStore: CaptureStatusStore
    private lateinit var chineseTextRecognizer: TextRecognizer
    private lateinit var latinTextRecognizer: TextRecognizer

    private val projectionCallback = object : MediaProjection.Callback() {
        override fun onStop() {
            captureStatusStore.markScreenAuthorizationRequired("capture_authorization_ended")
            stopCapture(stopProjection = false, clearAuthorizationWarning = false)
        }
    }

    override fun onCreate() {
        super.onCreate()
        captureStatusStore = CaptureStatusStore(this)
        captureStatusStore.setRunning(CaptureKind.SCREEN, false)
        eventWriter = BoundedEventWriter(applicationContext, scope, EVENT_QUEUE_CAPACITY)
        blobStore = EncryptedBlobStore(applicationContext)
        privacyGuard = ScreenCapturePrivacyGuard(applicationContext)
        chineseTextRecognizer = TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
        latinTextRecognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopCapture(stopProjection = true, clearAuthorizationWarning = true)
            ACTION_START -> if (effectiveCollectionConsent(applicationContext)) {
                startCapture(intent)
            } else {
                captureStatusStore.markScreenAuthorizationRequired("account_collection_consent_required")
                stopSelf(startId)
            }
            else -> stopSelf(startId)
        }
        return START_NOT_STICKY
    }

    private fun startCapture(intent: Intent) {
        if (projection != null) return
        if (!BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
            captureStatusStore.markScreenAuthorizationRequired("capture_authorization_missing")
            stopSelf()
            return
        }
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
        val resultData = intent.intentExtra(EXTRA_RESULT_DATA)
        if (resultCode != Activity.RESULT_OK || resultData == null) {
            captureStatusStore.markScreenAuthorizationRequired("capture_authorization_missing")
            stopSelf()
            return
        }

        CaptureNotifications.createChannel(this)
        startForeground(
            NOTIFICATION_ID,
            CaptureNotifications.ongoing(
                this,
                ScreenCaptureService::class.java,
                ACTION_STOP,
                accountText("My AI Twin is reading the screen", "AI替身正在读取屏幕"),
                accountText(
                    "Screenshots are encrypted on the phone and text is recognized on-device; tap Stop to end at any time",
                    "截图在手机加密保存并本地识别文字；点按“停止”可随时结束",
                ),
                NOTIFICATION_ID,
            ),
        )
        stopping.set(false)
        sessionStartedAt = System.currentTimeMillis()
        lastCaptureAt = 0L
        lastGuardCheckAt = 0L
        lastFrameAuditAt = 0L
        captureCount = 0

        try {
            val manager = getSystemService(MediaProjectionManager::class.java)
            val localProjection = manager.getMediaProjection(resultCode, resultData) ?: run {
                captureStatusStore.markScreenAuthorizationRequired("capture_authorization_invalid")
                stopCapture(stopProjection = true, clearAuthorizationWarning = false)
                return
            }
            projection = localProjection
            val thread = HandlerThread("mouchen-screen-capture").apply { start() }
            captureThread = thread
            val handler = Handler(thread.looper)
            localProjection.registerCallback(projectionCallback, handler)

            val dimensions = captureSize()
            val width = dimensions.width
            val height = dimensions.height
            val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
            imageReader = reader
            reader.setOnImageAvailableListener({ available -> captureFrame(available, width, height) }, handler)
            val display = checkNotNull(
                localProjection.createVirtualDisplay(
                    "MouchenExplicitScreenSession",
                    width,
                    height,
                    dimensions.densityDpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    reader.surface,
                    null,
                    handler,
                ),
            ) { "MediaProjection did not create a virtual display" }
            virtualDisplay = display
            if (stopping.get()) {
                display.release()
                virtualDisplay = null
                return
            }
            captureStatusStore.setRunning(CaptureKind.SCREEN, true)
            captureStatusStore.clearScreenAuthorizationWarning()
            if (stopping.get()) captureStatusStore.setRunning(CaptureKind.SCREEN, false)
            recordEvent(
                "screen.capture_started",
                JSONObject()
                    .put("width", width)
                    .put("height", height)
                    .put("source_width", dimensions.sourceWidth)
                    .put("source_height", dimensions.sourceHeight)
                    .put("max_capture_edge", MAX_CAPTURE_EDGE)
                    .put("sample_interval_ms", SAMPLE_INTERVAL_MS)
                    .put("visible_foreground_service", true)
                    .put("authorization_persisted", false),
                sessionStartedAt,
            )
        } catch (_: Exception) {
            captureStatusStore.markScreenAuthorizationRequired("capture_start_failed")
            stopCapture(stopProjection = true, clearAuthorizationWarning = false)
        }
    }

    private fun captureFrame(reader: ImageReader, width: Int, height: Int) {
        if (!effectiveCollectionConsent(applicationContext)) {
            captureStatusStore.markScreenAuthorizationRequired("account_collection_consent_required")
            stopCapture(stopProjection = true, clearAuthorizationWarning = false)
            return
        }
        val image = reader.acquireLatestImage() ?: return
        var diagnosticOccurredAt = System.currentTimeMillis()
        try {
            val now = System.currentTimeMillis()
            diagnosticOccurredAt = now
            if (!isScreenCaptureGuardCheckDue(
                    now = now,
                    lastCaptureAt = lastCaptureAt,
                    lastGuardCheckAt = lastGuardCheckAt,
                    sampleIntervalMs = SAMPLE_INTERVAL_MS,
                    guardCheckIntervalMs = GUARD_CHECK_INTERVAL_MS,
                )
            ) return
            lastGuardCheckAt = now
            // A MediaProjection grant alone never authorizes inspection. Accessibility must be
            // enabled and must have just identified a non-sensitive foreground package.
            if (getSystemService(KeyguardManager::class.java).isDeviceLocked) {
                recordOcrDiagnostic(
                    ScreenOcrDiagnostic(
                        status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                        stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                        reason = "device_locked",
                        framesInSession = captureCount,
                    ),
                    now,
                )
                return
            }
            val captureContext = privacyGuard.safeCaptureContext(now) ?: run {
                recordOcrDiagnostic(
                    ScreenOcrDiagnostic(
                        status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                        stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                        reason = "unsafe_or_stale_foreground",
                        framesInSession = captureCount,
                    ),
                    now,
                )
                return
            }
            // Unsafe frames must not consume the sampling slot. Otherwise the projection's first
            // frame (normally our own Activity) can delay the first safe app frame until after the
            // foreground-package safety signal has expired.
            lastCaptureAt = now
            val plane = image.planes.firstOrNull() ?: run {
                recordOcrDiagnostic(
                    ScreenOcrDiagnostic(
                        status = ScreenOcrDiagnostic.Status.ERROR,
                        stage = ScreenOcrDiagnostic.Stage.FRAME_CONVERSION,
                        reason = "missing_image_plane",
                        framesInSession = captureCount,
                    ),
                    now,
                )
                return
            }
            val pixelStride = plane.pixelStride
            val rowStride = plane.rowStride
            val paddedWidth = width + (rowStride - pixelStride * width) / pixelStride
            val padded = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888)
            try {
                plane.buffer.rewind()
                padded.copyPixelsFromBuffer(plane.buffer)
                // Always use a separate tightly packed bitmap. The padded ImageReader bitmap can
                // then be released immediately while ML Kit owns only one bounded frame.
                val logicalFrame = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also {
                    Canvas(it).drawBitmap(padded, 0f, 0f, null)
                }
                var handedToOcr = false
                try {
                    if (!isSameSafeForeground(captureContext)) {
                        recordOcrDiagnostic(
                            ScreenOcrDiagnostic(
                                status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                                stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                                reason = "foreground_changed_before_encode",
                                framesInSession = captureCount,
                            ),
                            now,
                        )
                        return
                    }
                    val encoded = ByteArrayOutputStream().use { output ->
                        check(logicalFrame.compress(Bitmap.CompressFormat.PNG, 100, output))
                        output.toByteArray()
                    }
                    val blobName = "screen-$now.png.mch"
                    try {
                        // Recheck after pixel conversion and compression. A package switch or a
                        // password signal during either operation invalidates this whole frame.
                        if (getSystemService(KeyguardManager::class.java).isDeviceLocked) {
                            recordOcrDiagnostic(
                                ScreenOcrDiagnostic(
                                    status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                                    stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                                    reason = "device_locked_before_persist",
                                    framesInSession = captureCount,
                                ),
                                now,
                            )
                            return
                        }
                        if (!isSameSafeForeground(captureContext)) {
                            recordOcrDiagnostic(
                                ScreenOcrDiagnostic(
                                    status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                                    stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                                    reason = "foreground_changed_before_persist",
                                    framesInSession = captureCount,
                                ),
                                now,
                            )
                            return
                        }
                        if (!effectiveCollectionConsent(applicationContext)) {
                            captureStatusStore.markScreenAuthorizationRequired(
                                "account_collection_consent_required",
                            )
                            stopCapture(stopProjection = true, clearAuthorizationWarning = false)
                            return
                        }
                        blobStore.write(blobName, encoded)
                    } finally {
                        encoded.fill(0)
                    }
                    captureCount++
                    // A frame audit is useful for diagnostics but does not need to occupy the
                    // event/sync queue every sample. Accepted OCR text carries its own blob ref.
                    if (now - lastFrameAuditAt >= FRAME_AUDIT_INTERVAL_MS) {
                        lastFrameAuditAt = now
                        recordEvent(
                            "screen.frame_captured",
                            JSONObject()
                                .put("blob", blobName)
                                .put("width", width)
                                .put("height", height)
                                .put("package", captureContext.foregroundPackage)
                                .put("frames_in_session", captureCount),
                            now,
                        )
                    }
                    handedToOcr = submitOcr(
                        bitmap = logicalFrame,
                        frame = ValidatedScreenOcrFrame(
                            blobName = blobName,
                            occurredAt = now,
                            captureContext = captureContext,
                        ),
                    )
                } finally {
                    if (!handedToOcr && !logicalFrame.isRecycled) logicalFrame.recycle()
                }
            } finally {
                if (!padded.isRecycled) padded.recycle()
            }
        } catch (error: Exception) {
            // A display resize or projection shutdown can invalidate the current frame.
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.ERROR,
                    stage = ScreenOcrDiagnostic.Stage.FRAME_CONVERSION,
                    exceptionType = error.safeExceptionType(),
                    framesInSession = captureCount,
                ),
                diagnosticOccurredAt,
            )
        } finally {
            image.close()
        }
    }

    /** Transfers bitmap ownership to the ML Kit task when true is returned. */
    private fun submitOcr(
        bitmap: Bitmap,
        frame: ValidatedScreenOcrFrame,
    ): Boolean {
        val submittedAt = System.currentTimeMillis()
        if (!ocrInFlight.compareAndSet(false, true)) {
            val activeStartedAt = activeOcrStartedAt.get()
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                    stage = ScreenOcrDiagnostic.Stage.OCR_SUBMISSION,
                    reason = "ocr_in_flight",
                    frameAgeMs = frame.ageAt(submittedAt),
                    operationAgeMs = elapsedSince(activeStartedAt, submittedAt),
                    framesInSession = captureCount,
                ),
            )
            return false
        }
        activeOcrStartedAt.set(submittedAt)
        val input = try {
            InputImage.fromBitmap(bitmap, 0)
        } catch (error: Exception) {
            activeOcrStartedAt.set(0L)
            ocrInFlight.set(false)
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.ERROR,
                    stage = ScreenOcrDiagnostic.Stage.OCR_SUBMISSION,
                    exceptionType = error.safeExceptionType(),
                    frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                    framesInSession = captureCount,
                ),
            )
            return false
        }
        val completed = AtomicBoolean(false)

        fun finish(rawText: String, engine: String) {
            if (!completed.compareAndSet(false, true)) return
            try {
                publishOcrText(rawText, engine, frame)
            } catch (error: Exception) {
                recordOcrDiagnostic(
                    ScreenOcrDiagnostic(
                        status = ScreenOcrDiagnostic.Status.ERROR,
                        stage = ScreenOcrDiagnostic.Stage.PUBLISH,
                        engine = engine,
                        exceptionType = error.safeExceptionType(),
                        recognizedCharacterCount = rawText.length,
                        frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                        framesInSession = captureCount,
                    ),
                )
            } finally {
                if (!bitmap.isRecycled) bitmap.recycle()
                activeOcrStartedAt.set(0L)
                ocrInFlight.set(false)
            }
        }

        val latinStarted = AtomicBoolean(false)
        fun runLatin(chineseCandidate: String) {
            if (!latinStarted.compareAndSet(false, true)) return
            try {
                latinTextRecognizer.process(input)
                    .addOnSuccessListener { latinResult ->
                        val latinCandidate = latinResult.text
                        val selected = selectBestText(chineseCandidate, latinCandidate)
                        finish(selected.first, selected.second)
                    }
                    .addOnFailureListener { error ->
                        recordOcrDiagnostic(
                            ScreenOcrDiagnostic(
                                status = ScreenOcrDiagnostic.Status.ERROR,
                                stage = ScreenOcrDiagnostic.Stage.LATIN_RECOGNIZER,
                                engine = "mlkit_latin",
                                exceptionType = error.safeExceptionType(),
                                frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                                framesInSession = captureCount,
                            ),
                        )
                        finish(chineseCandidate, "mlkit_chinese")
                    }
                    .addOnCanceledListener {
                        recordOcrDiagnostic(
                            ScreenOcrDiagnostic(
                                status = ScreenOcrDiagnostic.Status.ERROR,
                                stage = ScreenOcrDiagnostic.Stage.LATIN_RECOGNIZER,
                                reason = "task_canceled",
                                engine = "mlkit_latin",
                                exceptionType = "CancellationException",
                                frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                                framesInSession = captureCount,
                            ),
                        )
                        finish(chineseCandidate, "mlkit_chinese")
                    }
            } catch (error: Exception) {
                recordOcrDiagnostic(
                    ScreenOcrDiagnostic(
                        status = ScreenOcrDiagnostic.Status.ERROR,
                        stage = ScreenOcrDiagnostic.Stage.LATIN_RECOGNIZER,
                        engine = "mlkit_latin",
                        exceptionType = error.safeExceptionType(),
                        frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                        framesInSession = captureCount,
                    ),
                )
                finish(chineseCandidate, "mlkit_chinese")
            }
        }

        try {
            chineseTextRecognizer.process(input)
                .addOnSuccessListener { chineseResult ->
                    val candidate = chineseResult.text
                    if (candidate.any(::isCjkCharacter)) {
                        finish(candidate, "mlkit_chinese")
                    } else {
                        // The Latin model is usually more accurate on screens without CJK text.
                        runLatin(candidate)
                    }
                }
                .addOnFailureListener { error ->
                    recordOcrDiagnostic(
                        ScreenOcrDiagnostic(
                            status = ScreenOcrDiagnostic.Status.ERROR,
                            stage = ScreenOcrDiagnostic.Stage.CHINESE_RECOGNIZER,
                            engine = "mlkit_chinese",
                            exceptionType = error.safeExceptionType(),
                            frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                            framesInSession = captureCount,
                        ),
                    )
                    runLatin("")
                }
                .addOnCanceledListener {
                    recordOcrDiagnostic(
                        ScreenOcrDiagnostic(
                            status = ScreenOcrDiagnostic.Status.ERROR,
                            stage = ScreenOcrDiagnostic.Stage.CHINESE_RECOGNIZER,
                            reason = "task_canceled",
                            engine = "mlkit_chinese",
                            exceptionType = "CancellationException",
                            frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                            framesInSession = captureCount,
                        ),
                    )
                    runLatin("")
                }
        } catch (error: Exception) {
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.ERROR,
                    stage = ScreenOcrDiagnostic.Stage.CHINESE_RECOGNIZER,
                    engine = "mlkit_chinese",
                    exceptionType = error.safeExceptionType(),
                    frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                    framesInSession = captureCount,
                ),
            )
            runLatin("")
        }
        return true
    }

    private fun publishOcrText(
        rawText: String,
        engine: String,
        frame: ValidatedScreenOcrFrame,
    ) {
        // OCR completion is asynchronous. A per-account revocation after frame capture must stop
        // publication and tear down the projection rather than leaking a late result.
        if (!effectiveCollectionConsent(applicationContext)) {
            captureStatusStore.markScreenAuthorizationRequired("account_collection_consent_required")
            stopCapture(stopProjection = true, clearAuthorizationWarning = false)
            return
        }
        // This token is created only after the immutable frame passes the final pre-persist gate.
        // Completion checks only for a *new* risk signal. Requiring the original foreground signal
        // to remain inside its 10-second window silently discarded valid OCR on static screens and
        // during first-run model loading.
        if (getSystemService(KeyguardManager::class.java).isDeviceLocked) {
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                    stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                    reason = "device_locked_after_capture",
                    engine = engine,
                    recognizedCharacterCount = rawText.length,
                    frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                    framesInSession = captureCount,
                ),
            )
            return
        }
        if (!privacyGuard.completionStillSafe(frame.captureContext)) {
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = ScreenOcrDiagnostic.Status.GATE_SKIPPED,
                    stage = ScreenOcrDiagnostic.Stage.CAPTURE_GATE,
                    reason = "new_risk_signal_after_capture",
                    engine = engine,
                    recognizedCharacterCount = rawText.length,
                    frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                    framesInSession = captureCount,
                ),
            )
            return
        }
        val gateResult = ocrTextGate.evaluate(rawText, frame.occurredAt)
        if (gateResult is ScreenOcrGateResult.Rejected) {
            recordOcrDiagnostic(
                ScreenOcrDiagnostic(
                    status = if (gateResult.reason == ScreenOcrRejectionReason.EMPTY_TEXT) {
                        ScreenOcrDiagnostic.Status.EMPTY_TEXT
                    } else {
                        ScreenOcrDiagnostic.Status.GATE_SKIPPED
                    },
                    stage = ScreenOcrDiagnostic.Stage.TEXT_GATE,
                    reason = gateResult.reason.wireValue,
                    engine = engine,
                    recognizedCharacterCount = gateResult.recognizedCharacterCount,
                    frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                    framesInSession = captureCount,
                ),
            )
            return
        }
        val accepted = (gateResult as ScreenOcrGateResult.Accepted).text
        val visibleText = org.json.JSONArray()
        accepted.lines.forEach(visibleText::put)
        val queueAccepted = eventWriter.offer(
            LocalEventEntity(
                source = "android.screen_ocr",
                type = "ui.visible_text",
                occurredAt = frame.occurredAt,
                sensitivity = "restricted",
                payloadJson = JSONObject()
                    .put("package", frame.captureContext.foregroundPackage)
                    .put("context", "screen_ocr")
                    .put("analysis_requested", true)
                    .put("visible_text", visibleText)
                    .put("ocr_engine", engine)
                    .put("ocr_local", true)
                    .put("source_frame_blob", frame.blobName)
                    .put("character_count", accepted.characterCount)
                    .put("device_locked_excluded", true)
                    .put("secure_surface_bypass", false)
                    .toString(),
            ),
        )
        recordOcrDiagnostic(
            ScreenOcrDiagnostic(
                status = ScreenOcrDiagnostic.Status.ACCEPTED,
                stage = ScreenOcrDiagnostic.Stage.PUBLISH,
                engine = engine,
                recognizedCharacterCount = rawText.length,
                acceptedCharacterCount = accepted.characterCount,
                lineCount = accepted.lines.size,
                frameAgeMs = frame.ageAt(System.currentTimeMillis()),
                queueAccepted = queueAccepted,
                framesInSession = captureCount,
            ),
        )
    }

    private fun isSameSafeForeground(expected: ScreenCaptureContext): Boolean =
        privacyGuard.safeCaptureContext(System.currentTimeMillis())?.foregroundPackage == expected.foregroundPackage

    private fun selectBestText(chineseCandidate: String, latinCandidate: String): Pair<String, String> {
        val chineseScore = chineseCandidate.count(Char::isLetterOrDigit)
        val latinScore = latinCandidate.count(Char::isLetterOrDigit)
        return if (latinScore >= chineseScore) {
            latinCandidate to "mlkit_latin"
        } else {
            chineseCandidate to "mlkit_chinese"
        }
    }

    private fun isCjkCharacter(value: Char): Boolean =
        value in '\u3400'..'\u4DBF' ||
            value in '\u4E00'..'\u9FFF' ||
            value in '\uF900'..'\uFAFF'

    private fun captureSize(): CaptureDimensions {
        val manager = getSystemService(WindowManager::class.java)
        val sourceSize = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val bounds = manager.maximumWindowMetrics.bounds
            bounds.width().coerceAtLeast(1) to bounds.height().coerceAtLeast(1)
        } else {
            @Suppress("DEPRECATION")
            resources.displayMetrics.widthPixels.coerceAtLeast(1) to
                resources.displayMetrics.heightPixels.coerceAtLeast(1)
        }
        val longestEdge = maxOf(sourceSize.first, sourceSize.second)
        val scale = minOf(1f, MAX_CAPTURE_EDGE.toFloat() / longestEdge)
        return CaptureDimensions(
            width = (sourceSize.first * scale).roundToInt().coerceAtLeast(1),
            height = (sourceSize.second * scale).roundToInt().coerceAtLeast(1),
            sourceWidth = sourceSize.first,
            sourceHeight = sourceSize.second,
            densityDpi = (resources.displayMetrics.densityDpi * scale).roundToInt().coerceAtLeast(1),
        )
    }

    private fun recordEvent(type: String, payload: JSONObject, occurredAt: Long = System.currentTimeMillis()) {
        eventWriter.offer(
            LocalEventEntity(
                source = "android.screen",
                type = type,
                occurredAt = occurredAt,
                sensitivity = "restricted",
                payloadJson = payload.toString(),
            ),
        )
    }

    private fun recordOcrDiagnostic(
        diagnostic: ScreenOcrDiagnostic,
        occurredAt: Long = System.currentTimeMillis(),
    ) {
        val now = System.currentTimeMillis()
        if (!ocrDiagnosticLimiter.shouldEmit(diagnostic, now)) return
        recordEvent("screen.ocr_diagnostic", diagnostic.toMetadataJson(), occurredAt)
    }

    private fun stopCapture(stopProjection: Boolean, clearAuthorizationWarning: Boolean = false) {
        if (!stopping.compareAndSet(false, true)) return
        captureStatusStore.setRunning(CaptureKind.SCREEN, false)
        if (clearAuthorizationWarning) captureStatusStore.clearScreenAuthorizationWarning()
        imageReader?.setOnImageAvailableListener(null, null)
        virtualDisplay?.release()
        virtualDisplay = null
        imageReader?.close()
        imageReader = null
        val localProjection = projection
        projection = null
        runCatching { localProjection?.unregisterCallback(projectionCallback) }
        if (stopProjection) runCatching { localProjection?.stop() }
        captureThread?.quitSafely()
        captureThread = null

        val payload = JSONObject()
            .put("duration_ms", (System.currentTimeMillis() - sessionStartedAt).coerceAtLeast(0))
            .put("frames", captureCount)
        scope.launch {
            try {
                eventWriter.writeNow(
                    LocalEventEntity(
                        source = "android.screen",
                        type = "screen.capture_stopped",
                        sensitivity = "restricted",
                        payloadJson = payload.toString(),
                    ),
                )
            } catch (_: Exception) {
                // The writer already rate-limits error logs; service shutdown must still complete.
            }
            try {
                withContext(NonCancellable + Dispatchers.Main.immediate) {
                    runCatching {
                        stopForeground(STOP_FOREGROUND_REMOVE)
                        stopSelf()
                    }
                }
            } catch (_: Exception) {
                // No lifecycle failure may escape this service root coroutine.
            }
        }
    }

    override fun onDestroy() {
        if (::captureStatusStore.isInitialized) {
            captureStatusStore.setRunning(CaptureKind.SCREEN, false)
        }
        if (!stopping.get()) {
            stopping.set(true)
            imageReader?.setOnImageAvailableListener(null, null)
            virtualDisplay?.release()
            imageReader?.close()
            runCatching { projection?.unregisterCallback(projectionCallback) }
            runCatching { projection?.stop() }
            captureThread?.quitSafely()
        }
        if (::chineseTextRecognizer.isInitialized) chineseTextRecognizer.close()
        if (::latinTextRecognizer.isInitialized) latinTextRecognizer.close()
        if (::eventWriter.isInitialized) eventWriter.close()
        scope.cancel()
        super.onDestroy()
    }

    @Suppress("DEPRECATION")
    private fun Intent.intentExtra(name: String): Intent? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getParcelableExtra(name, Intent::class.java)
        } else {
            getParcelableExtra(name)
        }
    }

    companion object {
        const val ACTION_START = "com.mouchen.app.action.START_SCREEN_CAPTURE"
        const val ACTION_STOP = "com.mouchen.app.action.STOP_SCREEN_CAPTURE"
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        private const val NOTIFICATION_ID = 4102
        private const val SAMPLE_INTERVAL_MS = 15_000L
        private const val GUARD_CHECK_INTERVAL_MS = 500L
        private const val FRAME_AUDIT_INTERVAL_MS = 5L * 60 * 1000
        private const val MAX_CAPTURE_EDGE = 1_280
        private const val EVENT_QUEUE_CAPACITY = 16
    }

    private data class CaptureDimensions(
        val width: Int,
        val height: Int,
        val sourceWidth: Int,
        val sourceHeight: Int,
        val densityDpi: Int,
    )

    /** Exists only after the frame passed the final lock/package/password gate before storage. */
    private data class ValidatedScreenOcrFrame(
        val blobName: String,
        val occurredAt: Long,
        val captureContext: ScreenCaptureContext,
    ) {
        fun ageAt(now: Long): Long = if (now >= occurredAt) now - occurredAt else 0L
    }
}

private fun Throwable.safeExceptionType(): String =
    javaClass.simpleName.takeIf(String::isNotBlank)?.take(80) ?: "UnknownException"

private fun elapsedSince(startedAt: Long, now: Long): Long? =
    startedAt.takeIf { it > 0L && now >= it }?.let { now - it }
