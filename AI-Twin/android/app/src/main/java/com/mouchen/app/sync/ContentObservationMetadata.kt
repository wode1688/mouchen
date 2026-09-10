package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import java.security.MessageDigest
import java.util.Locale
import org.json.JSONArray
import org.json.JSONObject

/** Adds provenance to content without promoting observed text into an owner fact. */
internal object ContentObservationMetadata {
    fun enrich(event: LocalEventEntity, facts: JSONObject): JSONObject {
        if (!isContentBearing(event.type)) return facts
        val original = runCatching { JSONObject(event.payloadJson) }.getOrDefault(JSONObject())
        val kind = contentKind(event, original)
        val speaker = speaker(event, original, kind)
        val direction = messageDirection(event, original, speaker)
        val windowMillis = when (kind) {
            "chat", "search", "video", "web_page", "app_ui", "user_input" -> 10_000L
            else -> 15_000L
        }
        val bucket = event.occurredAt.coerceAtLeast(0L) / windowMillis
        val packageName = original.optString("package").trim().lowercase(Locale.ROOT).take(160)
        val context = safeContext(original.optString("context"))
        // Only non-secret routing metadata enters the session key. Contact names, content and URL
        // queries are deliberately excluded even when full-context cloud sync is enabled.
        val sessionMaterial = listOf(kind, event.source.take(80), packageName, context, bucket.toString())
            .joinToString("|")
        val hashMaterial = canonicalText(facts).ifBlank {
            listOf(event.type, event.source, packageName, bucket.toString()).joinToString("|")
        }
        return facts
            .put("content_kind", kind)
            .put("speaker", speaker)
            .put("message_direction", direction)
            .put("visible_only", visibleOnly(event, original))
            .put("evidence_strength", evidenceStrength(event, speaker, kind))
            .put("session_key", "$kind-${sha256(sessionMaterial).take(24)}")
            .put("content_hash", sha256(hashMaterial).take(32))
            .put("resolution_state", original.optString("resolution_state").takeIf {
                it in setOf("unknown", "unresolved", "resolved")
            } ?: "unknown")
    }

    private fun isContentBearing(type: String): Boolean = type in setOf(
        "notification.posted",
        "ui.visible_text",
        "speech.transcript",
        "ime.text_committed",
        "mail.received",
        "calendar.scheduled",
        "app.foreground_session",
        "message.sms",
        "intel.external",
        "threat.detected",
    )

    private fun contentKind(event: LocalEventEntity, payload: JSONObject): String {
        val packageName = payload.optString("package").lowercase(Locale.ROOT)
        val context = payload.optString("context").lowercase(Locale.ROOT)
        return when {
            event.type == "ime.text_committed" -> "user_input"
            event.type == "speech.transcript" -> "audio"
            context in setOf("owner_self_report", "owner_question") -> "user_input"
            event.type == "message.sms" || isChatPackage(packageName) || context.contains("conversation") -> "chat"
            event.type == "mail.received" -> "document"
            event.type == "calendar.scheduled" || event.type == "notification.posted" -> "system_notice"
            event.type in setOf("intel.external", "threat.detected") -> "web_page"
            event.type == "app.foreground_session" -> "app_ui"
            context.contains("shared_image") -> "document"
            isVideoPackage(packageName) || (isBrowserPackage(packageName) && looksLikeVideo(payload)) -> "video"
            isSearchPackage(packageName) || (isBrowserPackage(packageName) && looksLikeSearch(payload)) -> "search"
            isBrowserPackage(packageName) -> "web_page"
            else -> "app_ui"
        }
    }

    private fun speaker(event: LocalEventEntity, payload: JSONObject, kind: String): String = when {
        event.type == "ime.text_committed" || event.type == "speech.transcript" -> "user"
        kind == "user_input" -> "user"
        event.type == "message.sms" && payload.optString("direction") in setOf("sent", "outgoing") -> "user"
        event.type == "message.sms" -> "counterparty"
        event.type in setOf("intel.external", "threat.detected", "mail.received") -> "author"
        kind == "chat" -> "unknown"
        event.type in setOf("calendar.scheduled", "notification.posted", "app.foreground_session") -> "system"
        kind in setOf("search", "video", "web_page") -> "unknown"
        else -> "unknown"
    }

    private fun messageDirection(event: LocalEventEntity, payload: JSONObject, speaker: String): String = when {
        event.type == "message.sms" && payload.optString("direction") in setOf("sent", "outgoing") -> "outbound"
        event.type == "message.sms" -> "inbound"
        event.type == "ime.text_committed" -> "outbound"
        event.type == "speech.transcript" -> "self"
        speaker == "user" -> "self"
        speaker == "system" || speaker == "author" -> "inbound"
        else -> "unknown"
    }

    private fun visibleOnly(event: LocalEventEntity, payload: JSONObject): Boolean =
        event.type == "ui.visible_text" &&
            payload.optString("context").lowercase(Locale.ROOT) !in NON_VISIBLE_CONTEXTS

    private fun evidenceStrength(event: LocalEventEntity, speaker: String, kind: String): String = when {
        event.type in setOf("calendar.scheduled", "app.foreground_session") -> "direct"
        speaker == "user" -> "direct"
        speaker in setOf("counterparty", "author") -> "contextual"
        event.type == "notification.posted" || kind == "chat" -> "contextual"
        else -> "inferred"
    }

    private fun isChatPackage(value: String): Boolean = value in setOf(
        "com.tencent.mm",
        "com.tencent.mobileqq",
        "com.tencent.wework",
        "com.alibaba.android.rimet",
        "com.whatsapp",
        "org.telegram.messenger",
        "com.facebook.orca",
    )

    private fun isBrowserPackage(value: String): Boolean = value.contains("chrome") ||
        value.contains("browser") || value.contains("firefox") || value.contains("edge")

    private fun isSearchPackage(value: String): Boolean = value in setOf(
        "com.baidu.searchbox",
        "com.google.android.googlequicksearchbox",
        "com.microsoft.bing",
    )

    private fun isVideoPackage(value: String): Boolean = value in setOf(
        "com.google.android.youtube",
        "tv.danmaku.bili",
        "com.ss.android.ugc.aweme",
        "com.smile.gifmaker",
        "com.kuaishou.nebula",
        "com.tencent.qqlive",
        "com.youku.phone",
    )

    private fun looksLikeSearch(payload: JSONObject): Boolean = canonicalText(payload).containsAny(
        "搜索", "search", "百度", "bing", "google"
    )

    private fun looksLikeVideo(payload: JSONObject): Boolean = canonicalText(payload).containsAny(
        "视频", "youtube", "bilibili", "播放", "暂停"
    )

    private fun safeContext(value: String): String = value.lowercase(Locale.ROOT)
        .takeIf { it in SAFE_CONTEXTS }
        .orEmpty()

    private fun canonicalText(value: Any?): String = when (value) {
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString("|") { key ->
            "$key=${canonicalText(value.opt(key))}"
        }
        is JSONArray -> (0 until value.length()).joinToString("|") { canonicalText(value.opt(it)) }
        null, JSONObject.NULL -> ""
        else -> value.toString().replace(Regex("\\s+"), " ").trim().lowercase(Locale.ROOT)
    }.take(32_000)

    private fun String.containsAny(vararg needles: String): Boolean =
        needles.any { contains(it, ignoreCase = true) }

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private val SAFE_CONTEXTS = setOf(
        "owner_self_report",
        "owner_question",
        "shared_image",
        "conversation_import",
        "audio_transcript",
    )

    private val NON_VISIBLE_CONTEXTS = setOf(
        "owner_self_report",
        "owner_question",
        "shared_image",
        "conversation_import",
    )
}
