package com.mouchen.app.intake

sealed interface SharedImageIntakeResult {
    data class Success(
        val lines: List<String>,
        val characterCount: Int,
        val byteCount: Int,
        val mimeType: String?,
        val ocrEngine: String,
    ) : SharedImageIntakeResult

    data class Failure(val reason: SharedImageFailure) : SharedImageIntakeResult
}

enum class SharedImageFailure {
    UNAVAILABLE_IN_BUILD,
    BUSY,
    UNSUPPORTED_URI,
    UNSUPPORTED_TYPE,
    READ_DENIED,
    READ_FAILED,
    TOO_LARGE,
    INVALID_IMAGE,
    NO_TEXT,
    OCR_FAILED,
}

internal fun isSupportedImageMime(mimeType: String?): Boolean =
    mimeType?.substringBefore(';')?.trim()?.lowercase()?.startsWith("image/") == true

internal fun calculateImageSampleSize(
    width: Int,
    height: Int,
    maxEdge: Int = MAX_SHARED_IMAGE_EDGE,
    maxPixels: Long = MAX_SHARED_IMAGE_PIXELS,
): Int {
    require(width > 0 && height > 0)
    require(maxEdge > 0 && maxPixels > 0)
    var sample = 1
    while (true) {
        val sampledWidth = (width.toLong() + sample - 1) / sample
        val sampledHeight = (height.toLong() + sample - 1) / sample
        if (sampledWidth <= maxEdge && sampledHeight <= maxEdge && sampledWidth * sampledHeight <= maxPixels) {
            break
        }
        if (sample > Int.MAX_VALUE / 2) return Int.MAX_VALUE
        sample *= 2
    }
    return sample
}

internal const val MAX_SHARED_IMAGE_BYTES = 8 * 1024 * 1024
internal const val MAX_SHARED_IMAGE_EDGE = 2_048
internal const val MAX_SHARED_IMAGE_PIXELS = 4_000_000L
