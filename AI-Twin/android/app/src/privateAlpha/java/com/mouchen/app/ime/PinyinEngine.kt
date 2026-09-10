package com.mouchen.app.ime

import java.io.InputStream
import java.util.Locale
import java.util.TreeMap
import kotlin.math.ln
import kotlin.math.pow

enum class ImeInputMode(val label: String) {
    FULL_PINYIN("中·全拼"),
    NATURAL_CODE("中·自然码"),
    ENGLISH("EN"),
    ;

    fun next(): ImeInputMode = when (this) {
        FULL_PINYIN -> NATURAL_CODE
        NATURAL_CODE -> ENGLISH
        ENGLISH -> FULL_PINYIN
    }
}

data class ImeCandidate(
    val text: String,
    val segmentedPinyin: String,
    val frequency: Int,
) {
    val learningKey: String
        get() = "$segmentedPinyin\t$text"
}

data class ImeLearningStat(
    val count: Int,
    val lastCommittedAt: Long,
)

/** Public Natural Code (自然码) double-pinyin key mapping. */
object NaturalCodeScheme {
    private val zeroInitial = mapOf(
        "a" to "aa",
        "ai" to "al",
        "an" to "aj",
        "ang" to "ah",
        "ao" to "ak",
        "e" to "ee",
        "ei" to "ez",
        "en" to "ef",
        "eng" to "eg",
        "er" to "er",
        "o" to "oo",
        "ou" to "ob",
    )

    private val finalCodes = mapOf(
        "iang" to "d",
        "uang" to "d",
        "iong" to "s",
        "uai" to "y",
        "uan" to "r",
        "van" to "r",
        "iao" to "c",
        "ian" to "m",
        "ing" to "y",
        "ong" to "s",
        "ang" to "h",
        "eng" to "g",
        "iu" to "q",
        "ia" to "w",
        "ua" to "w",
        "ue" to "t",
        "ve" to "t",
        "un" to "p",
        "vn" to "p",
        "uo" to "o",
        "en" to "f",
        "an" to "j",
        "ao" to "k",
        "ai" to "l",
        "ei" to "z",
        "ie" to "x",
        "ui" to "v",
        "ou" to "b",
        "in" to "n",
    )

    fun encodeSyllable(syllable: String): Set<String> {
        val normalized = normalizeSyllable(syllable)
        if (normalized.isEmpty()) return emptySet()
        zeroInitial[normalized]?.let { canonical ->
            // Rime's public Natural Code schema also accepts the original
            // spelling for the six two-letter zero-initial syllables.
            return if (normalized.length == 2 && normalized != canonical) {
                linkedSetOf(canonical, normalized)
            } else {
                setOf(canonical)
            }
        }

        val (initial, final) = splitInitialAndFinal(normalized)
        val canonical = encodeInitial(initial) + finalCodes.getOrDefault(final, final)
        // Rime's public Natural Code algebra leaves this interjection unchanged.
        if (canonical == "hng") return setOf(canonical)
        if (canonical.length !in 1..2 || canonical.any { it !in 'a'..'z' }) return emptySet()

        // Natural Code accepts both ju and jv spellings for the four standalone ü finals.
        if (normalized.length == 2 && normalized[0] in "jqxy" && normalized[1] == 'u') {
            return linkedSetOf(canonical, "${normalized[0]}v")
        }
        return setOf(canonical)
    }

    fun encodeSegmented(segmentedPinyin: String): Set<String> {
        val syllables = segmentedPinyin
            .lowercase(Locale.ROOT)
            .split('\'')
            .filter(String::isNotBlank)
        if (syllables.isEmpty()) return emptySet()
        var codes = linkedSetOf("")
        for (syllable in syllables) {
            val variants = encodeSyllable(syllable)
            if (variants.isEmpty()) return emptySet()
            val next = linkedSetOf<String>()
            for (prefix in codes) {
                for (variant in variants) {
                    if (next.size >= MAX_VARIANTS) break
                    next += prefix + variant
                }
            }
            codes = next
        }
        return codes
    }

    private fun normalizeSyllable(value: String): String = value
        .trim()
        .lowercase(Locale.ROOT)
        .replace("u:", "v")
        .replace('ü', 'v')
        .filter { it in 'a'..'z' }

    private fun splitInitialAndFinal(value: String): Pair<String, String> {
        val initial = when {
            value.startsWith("sh") -> "sh"
            value.startsWith("ch") -> "ch"
            value.startsWith("zh") -> "zh"
            value.first() in SIMPLE_INITIALS -> value.first().toString()
            else -> ""
        }
        return initial to value.drop(initial.length)
    }

    private fun encodeInitial(value: String): String = when (value) {
        "sh" -> "u"
        "ch" -> "i"
        "zh" -> "v"
        else -> value
    }

    private const val MAX_VARIANTS = 8
    private const val SIMPLE_INITIALS = "bpmfdtnlgkhjqxrzcsyw"
}

/** Immutable, offline pinyin and Natural Code candidate index. */
class PinyinEngine private constructor(
    private val fullPinyin: TreeMap<String, List<ImeCandidate>>,
    private val naturalCode: TreeMap<String, List<ImeCandidate>>,
    private val associations: Map<String, List<ImeCandidate>>,
) {
    fun candidates(
        rawInput: String,
        mode: ImeInputMode,
        learnedStats: Map<String, ImeLearningStat> = emptyMap(),
        limit: Int = DEFAULT_LIMIT,
        nowMillis: Long = System.currentTimeMillis(),
    ): List<ImeCandidate> {
        if (mode == ImeInputMode.ENGLISH || limit <= 0) return emptyList()
        val code = normalizeCode(rawInput)
        if (code.isEmpty()) return emptyList()
        val index = if (mode == ImeInputMode.NATURAL_CODE) naturalCode else fullPinyin
        val exact = index[code].orEmpty()
        val prefix = mutableListOf<ImeCandidate>()
        if (exact.size < limit) {
            for ((candidateCode, values) in index.tailMap(code, false)) {
                if (!candidateCode.startsWith(code)) break
                // Scan the complete matching range. A lexical cutoff makes
                // frequent shi/shou words disappear behind earlier she* keys.
                prefix += values
            }
        }
        return rank(exact, learnedStats, limit, nowMillis) +
            rank(prefix, learnedStats, limit, nowMillis)
                .filterNot { candidate -> exact.any { it.text == candidate.text } }
                .take((limit - exact.distinctBy(ImeCandidate::text).size).coerceAtLeast(0))
    }

    fun associations(
        previousText: String,
        learnedStats: Map<String, ImeLearningStat> = emptyMap(),
        limit: Int = DEFAULT_LIMIT,
        nowMillis: Long = System.currentTimeMillis(),
    ): List<ImeCandidate> {
        if (limit <= 0 || previousText.isBlank()) return emptyList()
        val keys = buildList {
            val text = previousText.takeLast(MAX_ASSOCIATION_PREFIX)
            for (start in 0 until text.length) add(text.substring(start))
        }.sortedByDescending(String::length)
        val combined = keys.flatMap { associations[it].orEmpty() }
        return rank(combined, learnedStats, limit, nowMillis)
    }

    private fun rank(
        source: Collection<ImeCandidate>,
        learnedStats: Map<String, ImeLearningStat>,
        limit: Int,
        nowMillis: Long,
    ): List<ImeCandidate> = source
        .asSequence()
        .distinctBy(ImeCandidate::text)
        .sortedWith(
            compareByDescending<ImeCandidate> { candidate ->
                ln(candidate.frequency.coerceAtLeast(1).toDouble() + 1.0) * BASE_FREQUENCY_WEIGHT +
                    learningBoost(learnedStats[candidate.learningKey], nowMillis)
            }.thenByDescending(ImeCandidate::frequency).thenBy(ImeCandidate::text),
        )
        .take(limit)
        .toList()

    private fun learningBoost(stat: ImeLearningStat?, nowMillis: Long): Double {
        if (stat == null || stat.count <= 0) return 0.0
        val ageMillis = (nowMillis - stat.lastCommittedAt).coerceAtLeast(0L)
        val ageDays = ageMillis.toDouble() / MILLIS_PER_DAY
        val decayedCount = stat.count.coerceAtMost(MAX_EFFECTIVE_LEARN_COUNT) *
            0.5.pow(ageDays / LEARNING_HALF_LIFE_DAYS)
        return ln(decayedCount + 1.0) * LEARNING_WEIGHT
    }

    companion object {
        fun load(input: InputStream): PinyinEngine {
            val full = HashMap<String, MutableList<ImeCandidate>>()
            val natural = HashMap<String, MutableList<ImeCandidate>>()
            val associationIndex = HashMap<String, MutableList<ImeCandidate>>()
            input.bufferedReader(Charsets.UTF_8).useLines { lines ->
                lines.forEach { line ->
                    if (line.isBlank() || line.startsWith('#')) return@forEach
                    val fields = line.split('\t', limit = 3)
                    if (fields.size != 3) return@forEach
                    val segmented = normalizeSegmented(fields[0])
                    val word = fields[1].trim()
                    val frequency = fields[2].toIntOrNull() ?: return@forEach
                    if (segmented.isEmpty() || word.isEmpty() || frequency < 0) return@forEach
                    val candidate = ImeCandidate(word, segmented, frequency)
                    full.getOrPut(segmented.replace("'", "")) { mutableListOf() } += candidate
                    for (code in NaturalCodeScheme.encodeSegmented(segmented)) {
                        natural.getOrPut(code) { mutableListOf() } += candidate
                    }
                    addAssociations(candidate, associationIndex)
                }
            }
            return PinyinEngine(
                fullPinyin = sortedIndex(full),
                naturalCode = sortedIndex(natural),
                associations = associationIndex.mapValues { (_, values) ->
                    values.distinctBy(ImeCandidate::text)
                        .sortedByDescending(ImeCandidate::frequency)
                        .take(MAX_ASSOCIATIONS_PER_PREFIX)
                },
            )
        }

        private fun addAssociations(
            candidate: ImeCandidate,
            target: MutableMap<String, MutableList<ImeCandidate>>,
        ) {
            val syllables = candidate.segmentedPinyin.split('\'')
            if (candidate.text.length != syllables.size || candidate.text.length < 2) return
            val maxPrefix = minOf(MAX_ASSOCIATION_PREFIX, candidate.text.length - 1)
            for (prefixLength in 1..maxPrefix) {
                val prefix = candidate.text.substring(0, prefixLength)
                val suffix = candidate.text.substring(prefixLength)
                if (suffix.length > MAX_ASSOCIATION_SUFFIX) continue
                target.getOrPut(prefix) { mutableListOf() } += ImeCandidate(
                    text = suffix,
                    segmentedPinyin = syllables.drop(prefixLength).joinToString("'"),
                    frequency = candidate.frequency,
                )
            }
        }

        private fun sortedIndex(source: Map<String, MutableList<ImeCandidate>>): TreeMap<String, List<ImeCandidate>> {
            val result = TreeMap<String, List<ImeCandidate>>()
            source.forEach { (code, values) ->
                result[code] = values.distinctBy(ImeCandidate::text)
                    .sortedByDescending(ImeCandidate::frequency)
            }
            return result
        }

        private fun normalizeCode(value: String): String = value
            .lowercase(Locale.ROOT)
            .filter { it in 'a'..'z' }
            .take(MAX_INPUT_CODE_LENGTH)

        private fun normalizeSegmented(value: String): String = value
            .lowercase(Locale.ROOT)
            .replace("u:", "v")
            .replace('ü', 'v')
            .filter { it in 'a'..'z' || it == '\'' }
            .trim('\'')

        private const val DEFAULT_LIMIT = 9
        private const val MAX_INPUT_CODE_LENGTH = 64
        private const val MAX_ASSOCIATION_PREFIX = 3
        private const val MAX_ASSOCIATION_SUFFIX = 4
        private const val MAX_ASSOCIATIONS_PER_PREFIX = 64
        private const val BASE_FREQUENCY_WEIGHT = 100.0
        private const val LEARNING_WEIGHT = 90.0
        private const val MAX_EFFECTIVE_LEARN_COUNT = 100
        private const val LEARNING_HALF_LIFE_DAYS = 90.0
        private const val MILLIS_PER_DAY = 86_400_000.0
    }
}
