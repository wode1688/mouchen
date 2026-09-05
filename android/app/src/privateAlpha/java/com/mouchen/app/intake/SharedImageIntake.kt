package com.mouchen.app.intake

import android.content.ContentResolver
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.net.Uri
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import com.mouchen.app.collectors.ScreenOcrTextGate
import java.io.ByteArrayInputStream
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext

/** One-shot private-alpha OCR. The shared image is read immediately and is never persisted. */
class SharedImageIntake(context: Context) {
    private val appContext = context.applicationContext

    suspend fun process(uri: Uri, declaredMimeType: String?): SharedImageIntakeResult {
        if (!processInFlight.compareAndSet(false, true)) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.BUSY)
        }
        return try {
            try {
                withContext(Dispatchers.IO) { processOnce(uri, declaredMimeType) }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                SharedImageIntakeResult.Failure(SharedImageFailure.OCR_FAILED)
            }
        } finally {
            processInFlight.set(false)
        }
    }

    private suspend fun processOnce(uri: Uri, declaredMimeType: String?): SharedImageIntakeResult {
        if (uri.scheme != ContentResolver.SCHEME_CONTENT) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.UNSUPPORTED_URI)
        }
        val resolver = appContext.contentResolver
        val resolvedMime = runCatching { resolver.getType(uri) }.getOrNull()
        val mimeType = resolvedMime ?: declaredMimeType
        if (!isSupportedImageMime(mimeType)) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.UNSUPPORTED_TYPE)
        }
        val knownLength = runCatching {
            resolver.openAssetFileDescriptor(uri, "r")?.use { it.length }
        }.getOrNull()
        if (knownLength != null && knownLength > MAX_SHARED_IMAGE_BYTES) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.TOO_LARGE)
        }

        val bounded = try {
            resolver.openInputStream(uri)?.use { readBoundedBytes(it, MAX_SHARED_IMAGE_BYTES) }
                ?: return SharedImageIntakeResult.Failure(SharedImageFailure.READ_FAILED)
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: SecurityException) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.READ_DENIED)
        } catch (_: Exception) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.READ_FAILED)
        }
        if (bounded is BoundedByteRead.TooLarge) {
            return SharedImageIntakeResult.Failure(SharedImageFailure.TOO_LARGE)
        }
        val bytes = (bounded as BoundedByteRead.Success).bytes
        return try {
            val bitmap = decodeBoundedBitmap(bytes)
                ?: return SharedImageIntakeResult.Failure(SharedImageFailure.INVALID_IMAGE)
            try {
                // Once ML Kit owns the bitmap, wait for its task to finish before recycling even if
                // the Activity is destroyed. The result is still discarded by the cancelled caller.
                val recognized = withContext(NonCancellable) { recognize(bitmap) }
                    ?: return SharedImageIntakeResult.Failure(SharedImageFailure.OCR_FAILED)
                val accepted = ScreenOcrTextGate().accept(recognized.first, System.currentTimeMillis())
                    ?: return SharedImageIntakeResult.Failure(SharedImageFailure.NO_TEXT)
                SharedImageIntakeResult.Success(
                    lines = accepted.lines,
                    characterCount = accepted.characterCount,
                    byteCount = bytes.size,
                    mimeType = mimeType,
                    ocrEngine = recognized.second,
                )
            } finally {
                if (!bitmap.isRecycled) bitmap.recycle()
            }
        } finally {
            bytes.fill(0)
        }
    }

    private fun decodeBoundedBitmap(bytes: ByteArray): Bitmap? {
        val orientation = runCatching {
            ExifInterface(ByteArrayInputStream(bytes)).getAttributeInt(
                ExifInterface.TAG_ORIENTATION,
                ExifInterface.ORIENTATION_NORMAL,
            )
        }.getOrDefault(ExifInterface.ORIENTATION_NORMAL)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        val sample = calculateImageSampleSize(bounds.outWidth, bounds.outHeight)
        val options = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        val decoded = runCatching { BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) }.getOrNull()
            ?: return null
        return applyExifOrientation(decoded, orientation)
    }

    private fun applyExifOrientation(bitmap: Bitmap, orientation: Int): Bitmap? {
        val matrix = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.setScale(-1f, 1f)
            ExifInterface.ORIENTATION_ROTATE_180 -> matrix.setRotate(180f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> {
                matrix.setRotate(180f)
                matrix.postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_TRANSPOSE -> {
                matrix.setRotate(90f)
                matrix.postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_ROTATE_90 -> matrix.setRotate(90f)
            ExifInterface.ORIENTATION_TRANSVERSE -> {
                matrix.setRotate(-90f)
                matrix.postScale(-1f, 1f)
            }
            ExifInterface.ORIENTATION_ROTATE_270 -> matrix.setRotate(-90f)
            else -> return bitmap
        }
        return try {
            Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true).also {
                if (it !== bitmap && !bitmap.isRecycled) bitmap.recycle()
            }
        } catch (_: Exception) {
            if (!bitmap.isRecycled) bitmap.recycle()
            null
        }
    }

    private suspend fun recognize(bitmap: Bitmap): Pair<String, String>? = suspendCancellableCoroutine { continuation ->
        val chinese = TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
        val latin = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        val completed = AtomicBoolean(false)
        val input = try {
            InputImage.fromBitmap(bitmap, 0)
        } catch (_: Exception) {
            chinese.close()
            latin.close()
            continuation.resume(null)
            return@suspendCancellableCoroutine
        }

        fun finish(result: Pair<String, String>?) {
            if (!completed.compareAndSet(false, true)) return
            chinese.close()
            latin.close()
            if (continuation.isActive) continuation.resume(result)
        }

        fun runLatin(chineseCandidate: String) {
            runRecognizer(latin, input, onSuccess = { latinCandidate ->
                finish(selectBestOcrText(chineseCandidate, latinCandidate))
            }, onFailure = {
                if (chineseCandidate.isBlank()) finish(null) else finish(chineseCandidate to "mlkit_chinese")
            })
        }

        continuation.invokeOnCancellation {
            if (completed.compareAndSet(false, true)) {
                chinese.close()
                latin.close()
            }
        }
        runRecognizer(chinese, input, onSuccess = { candidate ->
            if (candidate.any(::isCjkCharacter)) {
                finish(candidate to "mlkit_chinese")
            } else {
                runLatin(candidate)
            }
        }, onFailure = { runLatin("") })
    }

    private fun runRecognizer(
        recognizer: TextRecognizer,
        input: InputImage,
        onSuccess: (String) -> Unit,
        onFailure: () -> Unit,
    ) {
        try {
            recognizer.process(input)
                .addOnSuccessListener { onSuccess(it.text) }
                .addOnFailureListener { onFailure() }
        } catch (_: Exception) {
            onFailure()
        }
    }

    private companion object {
        /** One bounded OCR operation per app process, even across Activity recreation/instances. */
        val processInFlight = AtomicBoolean(false)
    }
}

internal fun selectBestOcrText(chineseCandidate: String, latinCandidate: String): Pair<String, String> {
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
