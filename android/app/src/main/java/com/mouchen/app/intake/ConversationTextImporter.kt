package com.mouchen.app.intake

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

sealed interface ConversationImportResult {
    data class Success(
        val chunks: List<String>,
        val byteCount: Int,
        val mimeType: String?,
    ) : ConversationImportResult

    data object UnsupportedUri : ConversationImportResult
    data object UnsupportedDocument : ConversationImportResult
    data object TooLarge : ConversationImportResult
    data object Empty : ConversationImportResult
    data object InvalidUtf8 : ConversationImportResult
    data object ReadDenied : ConversationImportResult
    data object ReadFailed : ConversationImportResult
}

class ConversationTextImporter(context: Context) {
    private val appContext = context.applicationContext

    suspend fun read(uri: Uri): ConversationImportResult = withContext(Dispatchers.IO) {
        if (uri.scheme != ContentResolver.SCHEME_CONTENT) {
            return@withContext ConversationImportResult.UnsupportedUri
        }
        val resolver = appContext.contentResolver
        val displayName = queryDisplayName(resolver, uri)
        val mimeType = runCatching { resolver.getType(uri) }.getOrNull()
        if (!isSupportedConversationDocument(mimeType, displayName)) {
            return@withContext ConversationImportResult.UnsupportedDocument
        }

        val knownLength = runCatching {
            resolver.openAssetFileDescriptor(uri, "r")?.use { it.length }
        }.getOrNull()
        if (knownLength != null && knownLength > MAX_CONVERSATION_FILE_BYTES) {
            return@withContext ConversationImportResult.TooLarge
        }

        val bounded = try {
            resolver.openInputStream(uri)?.use {
                readBoundedBytes(it, MAX_CONVERSATION_FILE_BYTES)
            } ?: return@withContext ConversationImportResult.ReadFailed
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: SecurityException) {
            return@withContext ConversationImportResult.ReadDenied
        } catch (_: Exception) {
            return@withContext ConversationImportResult.ReadFailed
        }
        when (bounded) {
            BoundedByteRead.TooLarge -> ConversationImportResult.TooLarge
            is BoundedByteRead.Success -> {
                val bytes = bounded.bytes
                try {
                    prepareConversationImport(bytes, mimeType)
                } finally {
                    bytes.fill(0)
                }
            }
        }
    }
}

private fun queryDisplayName(resolver: ContentResolver, uri: Uri): String? = runCatching {
    resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
        if (!cursor.moveToFirst()) return@use null
        cursor.getString(0)?.take(MAX_DISPLAY_NAME_CHARACTERS)
    }
}.getOrNull()

internal fun isSupportedConversationDocument(mimeType: String?, displayName: String?): Boolean {
    val normalizedMime = mimeType?.substringBefore(';')?.trim()?.lowercase()
    val textMime = normalizedMime == "text/plain"
    val txtName = displayName?.trim()?.lowercase()?.endsWith(".txt") == true
    return textMime || txtName
}

internal fun prepareConversationImport(
    bytes: ByteArray,
    mimeType: String? = "text/plain",
): ConversationImportResult {
    if (bytes.isEmpty()) return ConversationImportResult.Empty
    val decoded = try {
        StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes))
            .toString()
    } catch (_: Exception) {
        return ConversationImportResult.InvalidUtf8
    }
    if ('\u0000' in decoded) return ConversationImportResult.InvalidUtf8
    val normalized = decoded
        .removePrefix("\uFEFF")
        .replace("\r\n", "\n")
        .replace('\r', '\n')
        .trim()
    if (normalized.isEmpty()) return ConversationImportResult.Empty
    if (normalized.length > MAX_CONVERSATION_TOTAL_CHARACTERS) {
        return ConversationImportResult.TooLarge
    }
    val chunks = chunkConversationText(normalized)
    if (chunks.isEmpty()) return ConversationImportResult.Empty
    if (chunks.size > MAX_CONVERSATION_CHUNKS) return ConversationImportResult.TooLarge
    return ConversationImportResult.Success(chunks, bytes.size, mimeType)
}

internal fun chunkConversationText(
    text: String,
    maxChunkCharacters: Int = MAX_CONVERSATION_CHUNK_CHARACTERS,
): List<String> {
    require(maxChunkCharacters > 0)
    val chunks = ArrayList<String>()
    var start = 0
    while (start < text.length) {
        var end = minOf(text.length, start + maxChunkCharacters)
        if (end < text.length) {
            val minimumUsefulBreak = start + maxChunkCharacters / 2
            val newline = text.lastIndexOf('\n', end - 1).takeIf { it >= minimumUsefulBreak }
            val whitespace = text.lastIndexOf(' ', end - 1).takeIf { it >= minimumUsefulBreak }
            end = newline ?: whitespace ?: end
        }
        // Never split a UTF-16 surrogate pair (for example an emoji) across events.
        if (end < text.length && end > start &&
            text[end - 1].isHighSurrogate() && text[end].isLowSurrogate()
        ) {
            end -= 1
        }
        val chunk = text.substring(start, end).trim()
        if (chunk.isNotEmpty()) chunks += chunk
        start = end
        while (start < text.length && text[start].isWhitespace()) start++
    }
    return chunks
}

internal const val MAX_CONVERSATION_FILE_BYTES = 512 * 1024
internal const val MAX_CONVERSATION_TOTAL_CHARACTERS = 80_000
internal const val MAX_CONVERSATION_CHUNKS = 24
internal const val MAX_CONVERSATION_CHUNK_CHARACTERS = 4_000
private const val MAX_DISPLAY_NAME_CHARACTERS = 240
