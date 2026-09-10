package com.mouchen.app.collectors

internal data class AcceptedScreenOcrText(
    val lines: List<String>,
    val characterCount: Int,
)

internal enum class ScreenOcrRejectionReason(val wireValue: String) {
    EMPTY_TEXT("empty_text"),
    NOT_MEANINGFUL("not_meaningful"),
    EXACT_REPEAT("exact_repeat"),
    NEAR_REPEAT("near_repeat"),
}

internal sealed interface ScreenOcrGateResult {
    data class Accepted(val text: AcceptedScreenOcrText) : ScreenOcrGateResult

    data class Rejected(
        val reason: ScreenOcrRejectionReason,
        val recognizedCharacterCount: Int,
    ) : ScreenOcrGateResult
}

/**
 * Bounds OCR payloads and suppresses unchanged or nearly unchanged screens.
 *
 * The gate only retains a small, in-memory history. It never stores raw bitmaps or OCR text.
 */
internal class ScreenOcrTextGate(
    private val exactRepeatWindowMs: Long = 30L * 60 * 1000,
    private val nearRepeatWindowMs: Long = 2L * 60 * 1000,
    private val maxHistoryEntries: Int = 64,
    private val maxLines: Int = 80,
    private val maxLineCharacters: Int = 500,
    private val maxTotalCharacters: Int = 6_000,
    private val nearDuplicateThreshold: Double = 0.94,
) {
    private val exactHistory = LinkedHashMap<String, Long>()
    private var lastAcceptedCanonical: String? = null
    private var lastAcceptedAt: Long = Long.MIN_VALUE

    init {
        require(exactRepeatWindowMs >= 0)
        require(nearRepeatWindowMs >= 0)
        require(maxHistoryEntries > 0)
        require(maxLines > 0)
        require(maxLineCharacters > 0)
        require(maxTotalCharacters > 0)
        require(nearDuplicateThreshold in 0.0..1.0)
    }

    @Synchronized
    fun evaluate(rawText: String, now: Long): ScreenOcrGateResult {
        val lines = normalizeLines(rawText)
        if (lines.isEmpty()) {
            return ScreenOcrGateResult.Rejected(
                reason = ScreenOcrRejectionReason.EMPTY_TEXT,
                recognizedCharacterCount = rawText.length,
            )
        }
        val canonical = lines.joinToString("\n")
        if (canonical.count(Char::isLetterOrDigit) < MIN_MEANINGFUL_CHARACTERS) {
            return ScreenOcrGateResult.Rejected(
                reason = ScreenOcrRejectionReason.NOT_MEANINGFUL,
                recognizedCharacterCount = canonical.length,
            )
        }

        pruneHistory(now)
        val exactAt = exactHistory[canonical]
        if (exactAt != null && elapsed(now, exactAt) < exactRepeatWindowMs) {
            return ScreenOcrGateResult.Rejected(
                reason = ScreenOcrRejectionReason.EXACT_REPEAT,
                recognizedCharacterCount = canonical.length,
            )
        }

        val previous = lastAcceptedCanonical
        if (
            previous != null &&
            elapsed(now, lastAcceptedAt) < nearRepeatWindowMs &&
            similarity(previous, canonical) >= nearDuplicateThreshold
        ) {
            remember(canonical, now)
            return ScreenOcrGateResult.Rejected(
                reason = ScreenOcrRejectionReason.NEAR_REPEAT,
                recognizedCharacterCount = canonical.length,
            )
        }

        remember(canonical, now)
        lastAcceptedCanonical = canonical
        lastAcceptedAt = now
        return ScreenOcrGateResult.Accepted(AcceptedScreenOcrText(lines, canonical.length))
    }

    fun accept(rawText: String, now: Long): AcceptedScreenOcrText? =
        (evaluate(rawText, now) as? ScreenOcrGateResult.Accepted)?.text

    private fun normalizeLines(rawText: String): List<String> {
        val result = ArrayList<String>(minOf(maxLines, 32))
        val seen = HashSet<String>()
        var usedCharacters = 0
        for (rawLine in rawText.lineSequence()) {
            if (result.size >= maxLines || usedCharacters >= maxTotalCharacters) break
            val normalized = rawLine
                .filterNot { it.isISOControl() }
                .replace(WHITESPACE, " ")
                .trim()
                .take(maxLineCharacters)
            if (normalized.isEmpty() || !seen.add(normalized)) continue
            val remaining = maxTotalCharacters - usedCharacters
            if (remaining <= 0) break
            // Do not append a tiny tail from a later line just to fill the last bytes.
            if (normalized.length > remaining && result.isNotEmpty()) break
            val bounded = normalized.take(remaining)
            if (bounded.isNotEmpty()) {
                result += bounded
                usedCharacters += bounded.length
            }
        }
        return result
    }

    private fun remember(canonical: String, now: Long) {
        exactHistory.remove(canonical)
        exactHistory[canonical] = now
        while (exactHistory.size > maxHistoryEntries) {
            exactHistory.remove(exactHistory.keys.first())
        }
    }

    private fun pruneHistory(now: Long) {
        val iterator = exactHistory.iterator()
        while (iterator.hasNext()) {
            if (elapsed(now, iterator.next().value) >= exactRepeatWindowMs) iterator.remove()
        }
    }

    private fun similarity(left: String, right: String): Double {
        if (left == right) return 1.0
        val leftShingles = shingles(left)
        val rightShingles = shingles(right)
        if (leftShingles.isEmpty() || rightShingles.isEmpty()) return 0.0
        val intersection = leftShingles.count(rightShingles::contains)
        val union = leftShingles.size + rightShingles.size - intersection
        return if (union == 0) 1.0 else intersection.toDouble() / union
    }

    private fun shingles(value: String): Set<String> {
        val compact = value.filterNot(Char::isWhitespace)
        if (compact.length < SHINGLE_SIZE) return setOf(compact)
        return buildSet(compact.length - SHINGLE_SIZE + 1) {
            for (index in 0..compact.length - SHINGLE_SIZE) {
                add(compact.substring(index, index + SHINGLE_SIZE))
            }
        }
    }

    private fun elapsed(now: Long, then: Long): Long = if (now >= then) now - then else Long.MAX_VALUE

    private companion object {
        val WHITESPACE = Regex("\\s+")
        const val MIN_MEANINGFUL_CHARACTERS = 3
        const val SHINGLE_SIZE = 2
    }
}
