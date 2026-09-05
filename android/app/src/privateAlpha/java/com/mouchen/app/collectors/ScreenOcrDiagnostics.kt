package com.mouchen.app.collectors

import org.json.JSONObject

/** Metadata-only outcome for screen OCR. Raw OCR text is deliberately not representable here. */
internal data class ScreenOcrDiagnostic(
    val status: Status,
    val stage: Stage,
    val reason: String? = null,
    val engine: String? = null,
    val exceptionType: String? = null,
    val recognizedCharacterCount: Int? = null,
    val acceptedCharacterCount: Int? = null,
    val lineCount: Int? = null,
    val frameAgeMs: Long? = null,
    val operationAgeMs: Long? = null,
    val queueAccepted: Boolean? = null,
    val framesInSession: Int? = null,
) {
    enum class Status(val wireValue: String) {
        ACCEPTED("accepted"),
        EMPTY_TEXT("empty_text"),
        GATE_SKIPPED("gate_skipped"),
        ERROR("error"),
    }

    enum class Stage(val wireValue: String) {
        CAPTURE_GATE("capture_gate"),
        FRAME_CONVERSION("frame_conversion"),
        OCR_SUBMISSION("ocr_submission"),
        CHINESE_RECOGNIZER("chinese_recognizer"),
        LATIN_RECOGNIZER("latin_recognizer"),
        TEXT_GATE("text_gate"),
        PUBLISH("publish"),
    }
}

internal fun ScreenOcrDiagnostic.toMetadataJson(): JSONObject = JSONObject()
    .put("status", status.wireValue)
    .put("stage", stage.wireValue)
    .putOptionalString("reason", reason)
    .putOptionalString("engine", engine)
    .putOptionalString("exception_type", exceptionType)
    .putOptionalNumber("recognized_character_count", recognizedCharacterCount)
    .putOptionalNumber("accepted_character_count", acceptedCharacterCount)
    .putOptionalNumber("line_count", lineCount)
    .putOptionalNumber("frame_age_ms", frameAgeMs)
    .putOptionalNumber("operation_age_ms", operationAgeMs)
    .putOptionalBoolean("queue_accepted", queueAccepted)
    .putOptionalNumber("frames_in_session", framesInSession)

/** Prevents a failed guard at display refresh rate from flooding the local event queue. */
internal class ScreenOcrDiagnosticLimiter(
    private val repeatWindowMs: Long = 60_000L,
    private val maxEntries: Int = 32,
) {
    private val emittedAt = LinkedHashMap<String, Long>()

    init {
        require(repeatWindowMs >= 0)
        require(maxEntries > 0)
    }

    @Synchronized
    fun shouldEmit(diagnostic: ScreenOcrDiagnostic, now: Long): Boolean {
        val key = listOf(
            diagnostic.status.wireValue,
            diagnostic.stage.wireValue,
            diagnostic.reason.orEmpty(),
            diagnostic.engine.orEmpty(),
            diagnostic.exceptionType.orEmpty(),
        ).joinToString("|")
        val previous = emittedAt[key]
        if (previous != null && elapsed(now, previous) < repeatWindowMs) return false
        emittedAt.remove(key)
        emittedAt[key] = now
        while (emittedAt.size > maxEntries) emittedAt.remove(emittedAt.keys.first())
        return true
    }

    private fun elapsed(now: Long, then: Long): Long = if (now >= then) now - then else Long.MAX_VALUE
}

private fun JSONObject.putOptionalString(key: String, value: String?): JSONObject = apply {
    value?.takeIf(String::isNotBlank)?.let { put(key, it.take(80)) }
}

private fun JSONObject.putOptionalNumber(key: String, value: Number?): JSONObject = apply {
    value?.let { put(key, it) }
}

private fun JSONObject.putOptionalBoolean(key: String, value: Boolean?): JSONObject = apply {
    value?.let { put(key, it) }
}
