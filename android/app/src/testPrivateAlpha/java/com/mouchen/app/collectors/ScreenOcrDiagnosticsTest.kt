package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ScreenOcrDiagnosticsTest {
    @Test
    fun acceptedDiagnosticContainsOnlyBoundedMetadata() {
        val payload = ScreenOcrDiagnostic(
            status = ScreenOcrDiagnostic.Status.ACCEPTED,
            stage = ScreenOcrDiagnostic.Stage.PUBLISH,
            engine = "mlkit_latin",
            recognizedCharacterCount = 120,
            acceptedCharacterCount = 98,
            lineCount = 4,
            frameAgeMs = 12_345,
            operationAgeMs = 11_000,
            queueAccepted = true,
            framesInSession = 3,
        ).toMetadataJson()

        assertEquals("accepted", payload.getString("status"))
        assertEquals("publish", payload.getString("stage"))
        assertEquals(120, payload.getInt("recognized_character_count"))
        assertEquals(98, payload.getInt("accepted_character_count"))
        assertEquals(4, payload.getInt("line_count"))
        assertEquals(11_000, payload.getInt("operation_age_ms"))
        assertTrue(payload.getBoolean("queue_accepted"))
        listOf("text", "visible_text", "raw_text", "package", "blob", "exception_message")
            .forEach { assertFalse(payload.has(it)) }
    }

    @Test
    fun exceptionDiagnosticExposesTypeButNeverMessage() {
        val payload = ScreenOcrDiagnostic(
            status = ScreenOcrDiagnostic.Status.ERROR,
            stage = ScreenOcrDiagnostic.Stage.CHINESE_RECOGNIZER,
            exceptionType = "MlKitException",
        ).toMetadataJson()

        assertEquals("error", payload.getString("status"))
        assertEquals("MlKitException", payload.getString("exception_type"))
        assertFalse(payload.has("exception_message"))
    }

    @Test
    fun limiterSuppressesOnlyRepeatedEquivalentOutcomesInsideWindow() {
        val limiter = ScreenOcrDiagnosticLimiter(repeatWindowMs = 1_000)
        val empty = ScreenOcrDiagnostic(
            status = ScreenOcrDiagnostic.Status.EMPTY_TEXT,
            stage = ScreenOcrDiagnostic.Stage.TEXT_GATE,
            reason = "empty_text",
        )
        val error = ScreenOcrDiagnostic(
            status = ScreenOcrDiagnostic.Status.ERROR,
            stage = ScreenOcrDiagnostic.Stage.LATIN_RECOGNIZER,
            exceptionType = "MlKitException",
        )

        assertTrue(limiter.shouldEmit(empty, 1_000))
        assertFalse(limiter.shouldEmit(empty.copy(recognizedCharacterCount = 0), 1_999))
        assertTrue(limiter.shouldEmit(error, 1_999))
        assertTrue(limiter.shouldEmit(empty, 2_000))
    }
}
