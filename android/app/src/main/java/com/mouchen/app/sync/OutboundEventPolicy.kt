package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONArray
import org.json.JSONObject

internal data class PreparedEvent(
    val source: String,
    val type: String,
    val facts: JSONObject,
    val sensitivity: String,
    val consentScope: String,
)

/** Keeps complete context on-device unless the owner explicitly enables private-node full sync. */
internal object OutboundEventPolicy {
    fun prepare(
        event: LocalEventEntity,
        minimizedContextOnly: Boolean,
        proactiveCloudEnabled: Boolean = false,
    ): PreparedEvent {
        val parsed = runCatching { JSONObject(event.payloadJson) }
            .getOrElse { JSONObject().put("unreadable_payload", true) }
        // Full-context consent never authorizes credentials. This recursive guard deliberately
        // runs before either the minimized or owner-full branch so neither path can bypass it.
        val credentialSanitization = OutboundCredentialSanitizer.sanitize(parsed)
        val original = credentialSanitization.payload
        val mandatoryPhoneMinimization = event.type in PRIVATE_PHONE_EVENT_TYPES
        val mandatoryMetadataOnly = event.type in ALWAYS_METADATA_ONLY_EVENT_TYPES
        val rawApproved = proactiveCloudEnabled && !minimizedContextOnly && !mandatoryMetadataOnly
        if (rawApproved) {
            val approvedFacts = if (mandatoryPhoneMinimization) {
                fullPrivatePhonePayload(event.type, original)
            } else {
                sanitizedOwnerFullPayload(event.type, original)
            }
            if (credentialSanitization.redacted) {
                approvedFacts.put(OutboundCredentialSanitizer.MARKER_KEY, true)
            }
            return PreparedEvent(
                event.source,
                event.type,
                ContentObservationMetadata.enrich(event, approvedFacts),
                event.sensitivity,
                "owner_full_context",
            )
        }
        val minimized = when (event.type) {
            "notification.posted" -> JSONObject()
                .copyString(original, "package", 200)
                .copyString(original, "category", 80)
                .putRedacted(original, "title", 240)
                .putRedacted(original, "text", 1200)
                .putRedacted(original, "big_text", 1800)
                .putRedacted(original, "sub_text", 300)
                .putRedacted(original, "summary_text", 300)
                .putRedactedArray(original, "text_lines", 12, 500)
            "ui.visible_text" -> JSONObject()
                .copyString(original, "package", 200)
                .copyNumber(original, "event_type")
                .copyString(original, "context", 80)
                .copyBoolean(original, "analysis_requested")
                .copyString(original, "import_id", 80)
                .copyNumber(original, "chunk_index")
                .copyNumber(original, "chunk_count")
                .copyNumber(original, "source_bytes")
                .copyString(original, "source_mime", 80)
                .copyString(original, "ocr_engine", 80)
                .copyBoolean(original, "ocr_local")
                .copyNumber(original, "character_count")
                .copyBoolean(original, "device_locked_excluded")
                .copyBoolean(original, "secure_surface_bypass")
                .put("visible_text", JSONArray().put(redactText(flatten(original.opt("visible_text")).take(1400))))
            "speech.transcript" -> JSONObject()
                .putRedacted(original, "transcript", 1800)
                .copyString(original, "language", 24)
                .copyString(original, "context", 80)
                .copyBoolean(original, "analysis_requested")
                .copyNumber(original, "duration_ms")
                .copyString(original, "engine", 80)
                .copyBoolean(original, "raw_audio_left_device")
                .copyString(original, "processing_location", 32)
                .copyBoolean(original, "raw_audio_cloud")
            "ime.text_committed" -> JSONObject()
                .copyString(original, "package", 200)
                .putRedacted(original, "text", 800)
                .put("password_excluded", original.optBoolean("password_excluded", true))
            "mail.received" -> JSONObject()
                .copyString(original, "domain", 80)
                .putRedacted(original, "subject", 300)
                .putRedacted(original, "body", 1800)
                .put("body_truncated", true)
            "calendar.scheduled" -> JSONObject()
                .copyNumber(original, "event_id")
                .putRedacted(original, "title", 300)
                .copyNumber(original, "begin")
                .copyNumber(original, "end")
                .copyNumber(original, "reschedule_count")
                .putRedacted(original, "description", 1200)
                .putRedacted(original, "location", 300)
                .putRedacted(original, "organizer", 200)
                .put("all_day", original.optBoolean("all_day", false))
            "app.foreground_session" -> JSONObject()
                .copyString(original, "package", 200)
                .putRedacted(original, "app_label", 200)
                .copyNumber(original, "duration_ms")
                .copyNumber(original, "timezone_offset_minutes")
            "location.snapshot" -> JSONObject()
                .copyNumber(original, "grid_latitude")
                .copyNumber(original, "grid_longitude")
                .copyNumber(original, "grid_size_degrees")
                .copyNumber(original, "grid_longitude_size_degrees")
                .copyNumber(original, "precision_m")
                .copyNumber(original, "age_ms")
            "message.sms" -> JSONObject()
                .copyString(original, "direction", 32)
                .copyDigest(original, "party_ref")
                .copyNumber(original, "recorded_at_ms")
                .copyNumber(original, "sent_at_ms")
                .copyNumber(original, "received_at_ms")
                .putRedacted(original, "body", 4_000)
                .copyBoolean(original, "body_truncated")
            "call.observed" -> JSONObject()
                .copyString(original, "direction", 32)
                .copyDigest(original, "party_ref")
                .copyNumber(original, "started_at_ms")
                .copyNumber(original, "duration_seconds")
            "contact.identifier" -> JSONObject()
                .copyDigest(original, "contact_ref")
                .copyString(original, "identifier_kind", 16)
                .copyDigest(original, "identifier_hash")
                .copyNumber(original, "last_updated_ms")
            "screen.ocr_diagnostic" -> JSONObject()
                .copyString(original, "status", 32)
                .copyString(original, "stage", 48)
                .copyString(original, "reason", 80)
                .copyString(original, "engine", 80)
                .copyString(original, "exception_type", 80)
                .copyNumber(original, "recognized_character_count")
                .copyNumber(original, "accepted_character_count")
                .copyNumber(original, "line_count")
                .copyNumber(original, "frame_age_ms")
                .copyNumber(original, "operation_age_ms")
                .copyBoolean(original, "queue_accepted")
                .copyNumber(original, "frames_in_session")
            else -> safeMetadata(original)
        }
        if (credentialSanitization.redacted) {
            minimized.put(OutboundCredentialSanitizer.MARKER_KEY, true)
        }
        return PreparedEvent(
            source = event.source,
            type = event.type,
            facts = ContentObservationMetadata.enrich(event, minimized),
            sensitivity = event.sensitivity,
            consentScope = if (mandatoryPhoneMinimization) {
                "alpha.private_phone_minimized"
            } else {
                "alpha.minimized_context"
            },
        )
    }

    internal fun redactText(value: String): String {
        var result = value
        result = result.replace(Regex("https?://\\S+", RegexOption.IGNORE_CASE), "[url]")
        result = result.replace(Regex("[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}"), "[email]")
        result = result.replace(Regex("(?<!\\d)(?:\\+?\\d[\\d\\s().-]{5,}\\d)(?!\\d)"), "[phone]")
        result = result.replace(Regex("(?<!\\d)\\d{4,8}(?!\\d)"), "[number]")
        result = result.replace(Regex("\\b[A-Fa-f0-9]{24,}\\b"), "[id]")
        result = OutboundCredentialSanitizer.sanitizeText(result)
        return result.replace(Regex("\\s+"), " ").trim()
    }

    /**
     * Full-context approval never overrides password/OTP/payment-secret exclusions. The IME
     * collector also suppresses password fields before persistence; this is a second outbound
     * boundary in case a malformed or imported event claims otherwise.
     */
    private fun sanitizedOwnerFullPayload(type: String, source: JSONObject): JSONObject {
        val sanitized = source
        if (type == "ime.text_committed" && !source.optBoolean("password_excluded", false)) {
            sanitized.remove("text")
            sanitized.remove("committed_text")
            sanitized.put("sensitive_content_excluded", true)
        }
        return sanitized
    }

    /** Phone bodies may be complete after double opt-in, but counterpart identifiers stay hashed. */
    private fun fullPrivatePhonePayload(type: String, source: JSONObject): JSONObject = when (type) {
        "message.sms" -> JSONObject()
            .copyString(source, "direction", 32)
            .copyDigest(source, "party_ref")
            .copyNumber(source, "recorded_at_ms")
            .copyNumber(source, "sent_at_ms")
            .copyNumber(source, "received_at_ms")
            .putApprovedBody(source, "body")
            .copyBoolean(source, "body_truncated")
        "call.observed" -> JSONObject()
            .copyString(source, "direction", 32)
            .copyDigest(source, "party_ref")
            .copyNumber(source, "started_at_ms")
            .copyNumber(source, "duration_seconds")
        "contact.identifier" -> JSONObject()
            .copyDigest(source, "contact_ref")
            .copyString(source, "identifier_kind", 16)
            .copyDigest(source, "identifier_hash")
            .copyNumber(source, "last_updated_ms")
        else -> JSONObject()
    }

    private fun JSONObject.putApprovedBody(source: JSONObject, key: String): JSONObject {
        val value = source.optString(key).takeIf(String::isNotBlank) ?: return this
        return put(key, value)
    }

    private fun JSONObject.putRedacted(source: JSONObject, key: String, limit: Int): JSONObject {
        val value = source.optString(key).takeIf { it.isNotBlank() } ?: return this
        return put(key, redactText(value).take(limit))
    }

    private fun JSONObject.copyString(source: JSONObject, key: String, limit: Int): JSONObject {
        val value = source.optString(key).takeIf { it.isNotBlank() } ?: return this
        return put(key, value.take(limit))
    }

    private fun JSONObject.copyDigest(source: JSONObject, key: String): JSONObject {
        val value = source.optString(key)
        if (HMAC_SHA256_HEX.matches(value)) put(key, value.lowercase())
        return this
    }

    private fun JSONObject.putRedactedArray(
        source: JSONObject,
        key: String,
        itemLimit: Int,
        characterLimit: Int,
    ): JSONObject {
        val values = source.optJSONArray(key) ?: return this
        val redacted = JSONArray()
        for (index in 0 until minOf(values.length(), itemLimit)) {
            values.optString(index).takeIf(String::isNotBlank)?.let {
                redacted.put(redactText(it).take(characterLimit))
            }
        }
        if (redacted.length() > 0) put(key, redacted)
        return this
    }

    private fun JSONObject.copyNumber(source: JSONObject, key: String): JSONObject {
        if (source.has(key) && source.opt(key) is Number) put(key, source.opt(key))
        return this
    }

    private fun JSONObject.copyBoolean(source: JSONObject, key: String): JSONObject {
        if (source.has(key) && source.opt(key) is Boolean) put(key, source.optBoolean(key))
        return this
    }

    private fun flatten(value: Any?): String = when (value) {
        is JSONArray -> buildList {
            for (index in 0 until value.length()) value.optString(index).takeIf(String::isNotBlank)?.let(::add)
        }.joinToString(" ")
        is String -> value
        else -> ""
    }

    private fun safeMetadata(source: JSONObject): JSONObject = JSONObject().apply {
        listOf(
            "duration_ms",
            "frames",
            "width",
            "height",
            "sample_rate",
            "visible_foreground_service",
            "password_excluded",
            "raw_audio_left_device",
            "processing_location",
            "raw_audio_cloud",
        )
            .forEach { key -> if (source.has(key)) put(key, source.opt(key)) }
    }

    private val PRIVATE_PHONE_EVENT_TYPES = setOf(
        "message.sms",
        "call.observed",
        "contact.identifier",
    )
    private val ALWAYS_METADATA_ONLY_EVENT_TYPES = setOf("screen.ocr_diagnostic")
    private val HMAC_SHA256_HEX = Regex("[A-Fa-f0-9]{64}")
}
