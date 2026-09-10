package com.mouchen.app.intake

import java.nio.charset.StandardCharsets
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConversationTextImporterTest {
    @Test
    fun acceptsPlainMimeOrTxtNameButNotArbitraryBinary() {
        assertTrue(isSupportedConversationDocument("text/plain; charset=utf-8", null))
        assertTrue(isSupportedConversationDocument("application/octet-stream", "chat.TXT"))
        assertFalse(isSupportedConversationDocument("application/pdf", "chat.pdf"))
    }

    @Test
    fun validUtf8IsNormalizedAndChunkedWithinBounds() {
        val text = buildString {
            repeat(10) { index ->
                append("第")
                append(index)
                append("段")
                append("内容".repeat(700))
                append("\r\n")
            }
        }
        val result = prepareConversationImport(text.toByteArray(StandardCharsets.UTF_8))
            as ConversationImportResult.Success

        assertTrue(result.chunks.isNotEmpty())
        assertTrue(result.chunks.size <= MAX_CONVERSATION_CHUNKS)
        assertTrue(result.chunks.all { it.length <= MAX_CONVERSATION_CHUNK_CHARACTERS })
        assertTrue(result.chunks.all { '\r' !in it })
    }

    @Test
    fun malformedUtf8IsRejected() {
        val result = prepareConversationImport(byteArrayOf(0xC3.toByte(), 0x28))

        assertEquals(ConversationImportResult.InvalidUtf8, result)
    }

    @Test
    fun totalCharacterLimitRejectsIncompleteImport() {
        val oversized = "字".repeat(MAX_CONVERSATION_TOTAL_CHARACTERS + 1)

        assertEquals(
            ConversationImportResult.TooLarge,
            prepareConversationImport(oversized.toByteArray(StandardCharsets.UTF_8)),
        )
    }

    @Test
    fun chunkerDoesNotEmitEmptyOrOversizedChunks() {
        val chunks = chunkConversationText("甲".repeat(9_500), maxChunkCharacters = 4_000)

        assertEquals(listOf(4_000, 4_000, 1_500), chunks.map(String::length))
    }

    @Test
    fun chunkerNeverSplitsASurrogatePair() {
        val original = "abc\uD83D\uDE03def"
        val chunks = chunkConversationText(original, maxChunkCharacters = 4)

        assertEquals(original, chunks.joinToString(""))
        assertTrue(chunks.all { it.length <= 4 })
        assertTrue(chunks.none { it.last().isHighSurrogate() || it.first().isLowSurrogate() })
    }
}
