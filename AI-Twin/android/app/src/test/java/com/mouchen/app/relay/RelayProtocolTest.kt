package com.mouchen.app.relay

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class RelayProtocolTest {
    private val request = RelayRequest("11111111-1111-4111-8111-111111111111", "partition", "snapshot.get", "{}")
    private fun response() = JSONObject().put("schema", RELAY_SCHEMA).put("kind", "response")
        .put("message_id", "22222222-2222-4222-8222-222222222222").put("in_reply_to", request.id)
        .put("sender", "computer-a").put("created_at", "2026-01-01T00:00:00Z").put("operation", request.operation)
        .put("ok", true).put("result", JSONObject().put("goals", JSONArray()).put("advice", JSONArray()))
    private fun bytes(value: JSONObject) = value.toString().toByteArray(Charsets.UTF_8)

    @Test fun matchingResponseIsAccepted() {
        val parsed = validateResponse(bytes(response()), "computer-a", request)
        assertTrue(parsed.ok)
        assertEquals(request.id, parsed.requestId)
    }
    @Test fun wrongSenderCannotCompleteRequest() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("sender", "other-peer")), "computer-a", request) }
    }
    @Test fun wrongOperationCannotCompleteRequest() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("operation", "goal.create")), "computer-a", request) }
    }
    @Test fun responseMustNameOriginalRequest() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("in_reply_to", "other")), "computer-a", request) }
    }
    @Test fun futureProtocolDoesNotSilentlyApply() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("schema", "other/v2")), "computer-a", request) }
    }
    @Test fun oversizedPayloadIsRejectedBeforeParsing() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(ByteArray(MAX_MESSAGE_BYTES + 1), "computer-a", request) }
    }
    @Test fun applicationFailureIsAnExplicitReceipt() {
        val parsed = validateResponse(bytes(response().put("ok", false).put("error", JSONObject().put("code", "invalid_goal").put("message", "Synthetic invalid goal"))), "computer-a", request)
        assertFalse(parsed.ok)
        assertEquals("invalid_goal", parsed.errorCode)
    }
    @Test fun cloudUploadDoesNotCompleteOrImmediatelyRetryRequest() {
        val uploaded = request.copy(uploadedAt = 1000L)
        assertEquals("pending", uploaded.state)
        assertFalse(needsTransportRetry(uploaded, 1000L + TRANSPORT_TTL_MS - 1))
        assertTrue(needsTransportRetry(uploaded, 1000L + TRANSPORT_TTL_MS))
        assertFalse(needsTransportRetry(uploaded.copy(state = "completed"), Long.MAX_VALUE))
    }
    @Test fun unsentAndInterruptedUploadRemainRetryable() {
        assertTrue(needsTransportRetry(request, 10L))
        assertTrue(needsTransportRetry(request.copy(transportStartedAt = 5L), 10L))
    }
    @Test fun requestEnvelopeHasStableCallerProvidedIdentifier() {
        val envelope = requestEnvelope("phone-a", "snapshot.get", JSONObject(), request.id)
        assertEquals(request.id, envelope.getString("message_id"))
        assertFalse(envelope.has("token"))
        assertThrows(IllegalArgumentException::class.java) { requestEnvelope("phone-a", "shell.run", JSONObject()) }
    }
    @Test fun successMustContainBothRecordArraysAndRealBoolean() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("ok", "true")), "computer-a", request) }
        assertThrows(org.json.JSONException::class.java) { validateResponse(bytes(response().put("result", JSONObject())), "computer-a", request) }
    }
    @Test fun oversizedRecordBatchCannotBeApplied() {
        val goals = JSONArray()
        repeat(101) { goals.put(JSONObject().put("id", request.id)) }
        val result = JSONObject().put("goals", goals).put("advice", JSONArray())
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response().put("result", result)), "computer-a", request) }
    }
    @Test fun trailingOrMalformedUtf8ContentIsRejected() {
        assertThrows(IllegalArgumentException::class.java) { validateResponse(bytes(response()) + " trailing".toByteArray(), "computer-a", request) }
        assertThrows(java.nio.charset.CharacterCodingException::class.java) { validateResponse(bytes(response()) + byteArrayOf(0xff.toByte()), "computer-a", request) }
    }
}
