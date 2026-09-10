package com.mouchen.app.relay

import java.time.Instant
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener

internal const val RELAY_SCHEMA = "ai-twin.sync/v1"
internal const val RELAY_PROJECT = "ai-twin-sync-v1"
internal const val MAX_MESSAGE_BYTES = 512 * 1024
internal const val TRANSPORT_TTL_MS = 24L * 60 * 60 * 1000
internal val OPERATIONS = setOf("goal.create", "event.create", "feedback.create", "snapshot.get")

internal fun requireUuid(value: String): String = value.also {
    require(UUID.fromString(it).toString() == it.lowercase()) { "Invalid message identifier" }
}

internal fun requestEnvelope(sender: String, operation: String, body: JSONObject, id: String = UUID.randomUUID().toString()): JSONObject {
    require(operation in OPERATIONS)
    return JSONObject().put("schema", RELAY_SCHEMA).put("kind", "request")
        .put("message_id", requireUuid(id)).put("sender", sender)
        .put("created_at", Instant.now().toString()).put("operation", operation).put("body", body)
        .also { require(it.toString().toByteArray(Charsets.UTF_8).size <= MAX_MESSAGE_BYTES) }
}

internal data class ValidatedResponse(
    val id: String,
    val requestId: String,
    val createdAt: Long,
    val ok: Boolean,
    val goals: JSONArray,
    val advice: JSONArray,
    val errorCode: String,
)

internal fun validateResponse(bytes: ByteArray, peer: String, request: RelayRequest): ValidatedResponse {
    require(bytes.size <= MAX_MESSAGE_BYTES) { "Response is too large" }
    val decoded = Charsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
        .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT).decode(java.nio.ByteBuffer.wrap(bytes)).toString()
    val parser = JSONTokener(decoded)
    val value = parser.nextValue() as? JSONObject ?: throw IllegalArgumentException("Expected message object")
    require(parser.nextClean() == '\u0000') { "Unexpected content after response" }
    require(value.getString("schema") == RELAY_SCHEMA && value.getString("kind") == "response")
    require(value.getString("sender") == peer) { "Unexpected response source" }
    require(value.getString("in_reply_to") == request.id && value.getString("operation") == request.operation)
    require(value.get("ok") is Boolean) { "Response ok must be a boolean" }
    val ok = value.getBoolean("ok")
    val result = if (ok) value.getJSONObject("result") else JSONObject()
    return ValidatedResponse(
        requireUuid(value.getString("message_id")), request.id,
        Instant.parse(value.getString("created_at")).toEpochMilli(), ok,
        if (ok) result.getJSONArray("goals") else JSONArray(), if (ok) result.getJSONArray("advice") else JSONArray(),
        if (ok) "" else value.getJSONObject("error").getString("code").take(80),
    ).also {
        require(it.goals.length() <= 100 && it.advice.length() <= 100)
        for ((kind, records) in listOf("goal" to it.goals, "advice" to it.advice)) for (index in 0 until records.length()) {
            val record = records.getJSONObject(index)
            requireUuid(record.getString("id"))
            requireText(record, "domain", 80)
            Instant.parse(record.getString("created_at"))
            if (kind == "goal") {
                requireText(record, "title", 200)
                requireText(record, "quote", 4000)
                require(record.get("version") is Number && record.getInt("version") > 0)
                record.getJSONObject("target")
            } else {
                requireText(record, "action", 2000)
                requireText(record, "first_step", 1000)
                requireText(record, "goal_quote", 4000)
                record.getJSONArray("evidence")
                record.getJSONObject("prediction")
                require(record.getString("status") in setOf("active", "adopted", "dismissed", "withdrawn", "verified"))
            }
        }
    }
}

private fun requireText(record: JSONObject, name: String, limit: Int) {
    val value = record.get(name)
    require(value is String && value.isNotBlank() && value.length <= limit) { "Invalid record field" }
}

/** Cloud acceptance is deliberately not an application receipt. */
internal fun needsTransportRetry(request: RelayRequest, now: Long): Boolean =
    request.state == "pending" && (request.uploadedAt == 0L || now - request.uploadedAt >= TRANSPORT_TTL_MS)

internal fun sha256(bytes: ByteArray): String = java.security.MessageDigest.getInstance("SHA-256")
    .digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }
