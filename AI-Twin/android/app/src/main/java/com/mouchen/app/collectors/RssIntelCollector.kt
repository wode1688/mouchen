package com.mouchen.app.collectors

import android.content.Context
import android.util.Xml
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.models.SecureSettingsStore
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.security.MessageDigest
import java.time.Instant
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import org.xmlpull.v1.XmlPullParser

data class RssFeedConfig(
    val id: String,
    val url: String,
    val domain: String = "general",
    val keywords: List<String> = emptyList(),
    val minimumRelevance: Double = 0.2,
    val treatMatchesAsThreats: Boolean = false,
    val enabled: Boolean = true,
)

class RssFeedStore(context: Context) {
    private val secureStore = SecureSettingsStore(context, "mouchen_rss_feeds")

    fun save(feeds: List<RssFeedConfig>) {
        val array = JSONArray()
        feeds.forEach { feed ->
            require(feed.id.isNotBlank() && feed.domain.isNotBlank()) { "Feed ID and target domain are required" }
            require(feed.url.startsWith("http://") || feed.url.startsWith("https://")) { "Feed URL must use HTTP or HTTPS" }
            array.put(
                JSONObject()
                    .put("id", feed.id)
                    .put("url", feed.url)
                    .put("domain", feed.domain)
                    .put("keywords", JSONArray(feed.keywords))
                    .put("minimum_relevance", feed.minimumRelevance.coerceIn(0.0, 1.0))
                    .put("treat_matches_as_threats", feed.treatMatchesAsThreats)
                    .put("enabled", feed.enabled),
            )
        }
        secureStore.put(KEY, array.toString())
    }

    fun load(): List<RssFeedConfig> = runCatching {
        val array = JSONArray(secureStore.get(KEY) ?: "[]")
        buildList {
            for (index in 0 until array.length()) {
                val json = array.getJSONObject(index)
                val keywords = json.optJSONArray("keywords") ?: JSONArray()
                add(
                    RssFeedConfig(
                        id = json.getString("id"),
                        url = json.getString("url"),
                        domain = json.optString("domain", "general"),
                        keywords = buildList {
                            for (keywordIndex in 0 until keywords.length()) add(keywords.getString(keywordIndex))
                        },
                        minimumRelevance = json.optDouble("minimum_relevance", 0.2).coerceIn(0.0, 1.0),
                        treatMatchesAsThreats = json.optBoolean("treat_matches_as_threats", false),
                        enabled = json.optBoolean("enabled", true),
                    ),
                )
            }
        }
    }.getOrDefault(emptyList())

    fun clear() = secureStore.remove(KEY)

    fun clearDurably(): Boolean = secureStore.removeDurably(KEY)

    private companion object {
        const val KEY = "feeds"
    }
}

class RssIntelCollector(
    private val dao: MouchenDao,
    private val feedStore: RssFeedStore,
    private val client: OkHttpClient = OkHttpClient(),
) {
    suspend fun collect(): Int = withContext(Dispatchers.IO) {
        var inserted = 0
        feedStore.load().filter { it.enabled }.forEach { feed ->
            runCatching { fetch(feed) }.getOrDefault(emptyList()).forEach itemLoop@ { item ->
                val relevance = relevance(item, feed.keywords)
                if (relevance < feed.minimumRelevance) return@itemLoop
                val stableKey = item.guid.ifBlank { item.link.ifBlank { "${item.title}|${item.publishedAt}" } }
                val type = if (feed.treatMatchesAsThreats) "threat.detected" else "intel.external"
                val rowId = dao.insertEventIfAbsent(
                    LocalEventEntity(
                        id = "rss-${sha256("${feed.id}|$stableKey")}",
                        source = "rss.${feed.id}".take(80),
                        type = type,
                        occurredAt = item.publishedAt ?: System.currentTimeMillis(),
                        sensitivity = "public",
                        payloadJson = JSONObject()
                            .put("domain", feed.domain)
                            .put("topic", item.title.take(240))
                            .put("title", item.title.take(1000))
                            .put("summary", item.summary.take(4000))
                            .put("url", item.link)
                            .put("feed_url", feed.url)
                            .put("relevance", relevance)
                            .put("independent_sources", 1)
                            .put("primary_source", false)
                            .toString(),
                    ),
                )
                if (rowId != -1L) inserted += 1
            }
        }
        inserted
    }

    private fun fetch(feed: RssFeedConfig): List<RssItem> {
        val request = Request.Builder().url(feed.url).header("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml").build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) error("RSS returned HTTP ${response.code}")
            val body = response.body ?: return emptyList()
            if (body.contentLength() > MAX_FEED_BYTES) error("RSS document exceeds size limit")
            val bytes = body.byteStream().use(::readLimited)
            return parse(bytes.inputStream())
        }
    }

    private fun parse(stream: InputStream): List<RssItem> {
        val parser = Xml.newPullParser()
        runCatching { parser.setFeature("http://xmlpull.org/v1/doc/features.html#process-docdecl", false) }
        parser.setInput(stream, null)
        val items = mutableListOf<RssItem>()
        var current: MutableRssItem? = null
        while (parser.eventType != XmlPullParser.END_DOCUMENT && items.size < MAX_ITEMS) {
            if (parser.eventType == XmlPullParser.START_TAG) {
                val name = parser.name.substringAfter(':').lowercase(Locale.ROOT)
                when {
                    name == "item" || name == "entry" -> current = MutableRssItem()
                    current != null && name == "title" -> current.title = readElementText(parser)
                    current != null && name in setOf("description", "summary", "content", "encoded") -> current.summary = readElementText(parser)
                    current != null && name in setOf("guid", "id") -> current.guid = readElementText(parser)
                    current != null && name == "link" -> {
                        current.link = parser.getAttributeValue(null, "href") ?: readElementText(parser)
                    }
                    current != null && name in setOf("pubdate", "published", "updated", "date") -> {
                        current.publishedAt = parseDate(readElementText(parser))
                    }
                }
            } else if (
                parser.eventType == XmlPullParser.END_TAG &&
                parser.name.substringAfter(':').lowercase(Locale.ROOT) in setOf("item", "entry")
            ) {
                current?.let { items += it.freeze() }
                current = null
            }
            parser.next()
        }
        return items
    }

    private fun readElementText(parser: XmlPullParser): String {
        val startDepth = parser.depth
        val text = StringBuilder()
        while (parser.next() != XmlPullParser.END_DOCUMENT) {
            when (parser.eventType) {
                XmlPullParser.TEXT, XmlPullParser.CDSECT -> text.append(parser.text)
                XmlPullParser.END_TAG -> if (parser.depth == startDepth) break
            }
        }
        return text.toString().trim()
    }

    private fun relevance(item: RssItem, keywords: List<String>): Double {
        if (keywords.isEmpty()) return 1.0
        val haystack = "${item.title}\n${item.summary}".lowercase(Locale.ROOT)
        val normalized = keywords.map { it.trim().lowercase(Locale.ROOT) }.filter { it.isNotBlank() }.distinct()
        if (normalized.isEmpty()) return 1.0
        return normalized.count(haystack::contains).toDouble() / normalized.size
    }

    private fun parseDate(value: String): Long? {
        return runCatching { Instant.parse(value).toEpochMilli() }.getOrNull()
            ?: runCatching { ZonedDateTime.parse(value, DateTimeFormatter.RFC_1123_DATE_TIME).toInstant().toEpochMilli() }.getOrNull()
            ?: runCatching { ZonedDateTime.parse(value).toInstant().toEpochMilli() }.getOrNull()
    }

    private fun readLimited(input: InputStream): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        var total = 0
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            total += count
            if (total > MAX_FEED_BYTES) error("RSS document exceeds size limit")
            output.write(buffer, 0, count)
        }
        return output.toByteArray()
    }

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private data class RssItem(val title: String, val summary: String, val link: String, val guid: String, val publishedAt: Long?)
    private class MutableRssItem {
        var title = ""
        var summary = ""
        var link = ""
        var guid = ""
        var publishedAt: Long? = null
        fun freeze() = RssItem(title, summary, link, guid, publishedAt)
    }

    private companion object {
        const val MAX_FEED_BYTES = 2 * 1024 * 1024L
        const val MAX_ITEMS = 200
    }
}
