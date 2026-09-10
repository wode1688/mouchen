package com.mouchen.app.ime

import android.content.Context
import com.mouchen.app.models.SecureSettingsStore
import org.json.JSONObject

/** Stores only candidate-specific selection stats; no composing text or surrounding context is retained. */
class ImeLearningStore(context: Context) {
    private val secureStore = SecureSettingsStore(context.applicationContext, PREFERENCES)

    @Synchronized
    fun load(): Map<String, ImeLearningStat> {
        val encoded = secureStore.get(KEY) ?: return emptyMap()
        return runCatching {
            val root = JSONObject(encoded)
            if (root.optInt("version", 0) != SCHEMA_VERSION) return@runCatching emptyMap()
            val entries = root.optJSONObject("entries") ?: return@runCatching emptyMap()
            buildMap {
                val keys = entries.keys()
                while (keys.hasNext() && size < MAX_ENTRIES) {
                    val key = keys.next()
                    val value = entries.optJSONObject(key) ?: continue
                    val count = value.optInt("count", 0).coerceAtMost(MAX_COUNT)
                    val lastCommittedAt = value.optLong("last_committed_at", 0L)
                    if (isLearnableKey(key) && count > 0 && lastCommittedAt > 0L) {
                        put(key, ImeLearningStat(count, lastCommittedAt))
                    }
                }
            }
        }.getOrDefault(emptyMap())
    }

    @Synchronized
    fun save(stats: Map<String, ImeLearningStat>): Boolean = runCatching {
        val compact = stats.entries
            .asSequence()
            .filter { (key, stat) -> isLearnableKey(key) && stat.count > 0 && stat.lastCommittedAt > 0L }
            .sortedWith(
                compareByDescending<Map.Entry<String, ImeLearningStat>> { it.value.lastCommittedAt }
                    .thenByDescending { it.value.count },
            )
            .take(MAX_ENTRIES)
            .toList()
        val encodedEntries = JSONObject()
        compact.forEach { (key, stat) ->
            encodedEntries.put(
                key,
                JSONObject()
                    .put("count", stat.count.coerceAtMost(MAX_COUNT))
                    .put("last_committed_at", stat.lastCommittedAt),
            )
        }
        secureStore.putDurably(
            KEY,
            JSONObject()
                .put("version", SCHEMA_VERSION)
                .put("entries", encodedEntries)
                .toString(),
        )
    }.getOrDefault(false)

    @Synchronized
    fun clear(): Boolean = runCatching {
        secureStore.removeDurably(KEY)
    }.getOrDefault(false)

    private fun isLearnableKey(key: String): Boolean {
        val separator = key.lastIndexOf('\t')
        if (separator !in 1 until key.lastIndex) return false
        val pinyin = key.substring(0, separator)
        val text = key.substring(separator + 1)
        return pinyin.length <= MAX_PINYIN_LENGTH &&
            pinyin.all { it in 'a'..'z' || it == '\'' } &&
            text.isNotBlank() &&
            text.length <= MAX_TEXT_LENGTH &&
            text.none { it.isISOControl() }
    }

    private companion object {
        const val PREFERENCES = "mouchen_ime_learning"
        const val KEY = "candidate_counts"
        const val SCHEMA_VERSION = 2
        const val MAX_ENTRIES = 500
        const val MAX_COUNT = 1_000
        const val MAX_PINYIN_LENGTH = 64
        const val MAX_TEXT_LENGTH = 16
    }
}

class ImeModeStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun load(): ImeInputMode = runCatching {
        ImeInputMode.valueOf(preferences.getString(KEY, null).orEmpty())
    }.getOrDefault(ImeInputMode.FULL_PINYIN)

    fun save(mode: ImeInputMode): Boolean = runCatching {
        preferences.edit().putString(KEY, mode.name).apply()
        true
    }.getOrDefault(false)

    private companion object {
        const val PREFERENCES = "mouchen_ime_preferences"
        const val KEY = "input_mode"
    }
}
