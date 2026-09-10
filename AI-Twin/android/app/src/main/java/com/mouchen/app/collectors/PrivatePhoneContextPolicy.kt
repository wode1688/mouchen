package com.mouchen.app.collectors

import java.util.Locale
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Provider-free privacy and paging rules shared by both distribution flavors. */
internal enum class ParticipantKind(val wireName: String) {
    PHONE("phone"),
    EMAIL("email"),
    OTHER("other"),
}

internal data class PhoneContextParticipant(
    val kind: ParticipantKind,
    val identifierHash: String,
    val masked: String,
)

internal data class TimelineCursor(val timestampMs: Long, val rowId: Long)

internal fun phoneContextParticipant(secret: ByteArray, value: String): PhoneContextParticipant {
    val kind = participantKind(value)
    return PhoneContextParticipant(
        kind = kind,
        identifierHash = phoneContextIdentifierHash(secret, kind, value),
        masked = maskParticipant(value, kind),
    )
}

internal fun phoneContextIdentifierHash(secret: ByteArray, kind: ParticipantKind, value: String): String {
    val normalized = normalizeParticipant(value, kind)
    if (normalized.isEmpty()) return ""
    return keyedDigestHex(secret, "${kind.wireName}:$normalized")
}

internal fun participantKind(value: String): ParticipantKind {
    val trimmed = value.trim()
    return when {
        trimmed.contains('@') -> ParticipantKind.EMAIL
        trimmed.count(Char::isDigit) >= 5 -> ParticipantKind.PHONE
        else -> ParticipantKind.OTHER
    }
}

internal fun normalizeParticipant(value: String, kind: ParticipantKind): String = when (kind) {
    ParticipantKind.PHONE -> value.filter(Char::isDigit)
    ParticipantKind.EMAIL -> value.trim().lowercase(Locale.ROOT)
    ParticipantKind.OTHER -> value.trim().uppercase(Locale.ROOT)
}

internal fun maskParticipant(value: String, kind: ParticipantKind): String {
    val normalized = normalizeParticipant(value, kind)
    if (normalized.isEmpty()) return "unknown"
    return when (kind) {
        ParticipantKind.PHONE -> "***${normalized.takeLast(minOf(4, normalized.length))}"
        ParticipantKind.EMAIL -> {
            val local = normalized.substringBefore('@')
            val domain = normalized.substringAfter('@', "")
            val domainName = domain.substringBefore('.')
            val suffix = domain.substringAfter('.', "")
            buildString {
                append(local.take(1).ifEmpty { "*" })
                append("***@")
                append(domainName.take(1).ifEmpty { "*" })
                append("***")
                if (suffix.isNotEmpty()) append('.').append(suffix.takeLast(8))
            }
        }
        ParticipantKind.OTHER -> when (normalized.length) {
            1 -> "*"
            2 -> "${normalized.first()}*"
            else -> "${normalized.first()}***${normalized.last()}"
        }
    }
}

internal fun stablePhoneContextEventId(
    prefix: String,
    secret: ByteArray,
    providerRowId: Long,
    timestampMs: Long,
): String {
    val digest = keyedDigestHex(secret, "$prefix:$providerRowId:$timestampMs")
    return "$prefix-$digest"
}

internal fun keyedDigestHex(secret: ByteArray, value: String): String {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(secret, "HmacSHA256"))
    return mac.doFinal(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
}

// Values are the stable Android provider wire values; keeping provider APIs out of common code
// ensures the Play flavor contains privacy rules but no SMS/call-log query implementation.
internal fun smsDirection(type: Int): String = when (type) {
    1 -> "received"
    2 -> "sent"
    3 -> "draft"
    4, 5, 6 -> "outgoing_pending"
    else -> "unknown"
}

internal fun callDirection(type: Int): String = when (type) {
    1 -> "incoming"
    2 -> "outgoing"
    3 -> "missed"
    4 -> "voicemail"
    5 -> "rejected"
    6 -> "blocked"
    7 -> "answered_elsewhere"
    else -> "unknown"
}

internal fun initialTimelineCursor(nowMs: Long): TimelineCursor =
    TimelineCursor((nowMs - PHONE_CONTEXT_LOOKBACK_MS).coerceAtLeast(0L), -1L)

internal fun clampTimelineCursor(cursor: TimelineCursor, nowMs: Long): TimelineCursor {
    val minimum = (nowMs - PHONE_CONTEXT_LOOKBACK_MS).coerceAtLeast(0L)
    if (cursor.timestampMs !in minimum..nowMs) return TimelineCursor(minimum, -1L)
    return cursor
}

internal fun nextTimelineCursor(
    nowMs: Long,
    seenRows: Int,
    last: TimelineCursor,
): TimelineCursor = if (seenRows >= PHONE_CONTEXT_PAGE_SIZE) {
    last
} else {
    TimelineCursor((nowMs - PHONE_CONTEXT_OVERLAP_MS).coerceAtLeast(0L), -1L)
}

internal const val PHONE_CONTEXT_PAGE_SIZE = 500
internal const val PHONE_CONTEXT_LOOKBACK_MS = 7L * 24 * 60 * 60 * 1000
private const val PHONE_CONTEXT_OVERLAP_MS = 5L * 60 * 1000
