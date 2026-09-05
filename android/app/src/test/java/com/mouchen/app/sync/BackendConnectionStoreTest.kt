package com.mouchen.app.sync

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BackendConnectionStoreTest {
    @Test
    fun sensitiveBackendRequiresTls() {
        assertTrue(isSecureBackendUrl("https://192.168.50.11:8788"))
        assertTrue(isSecureBackendUrl("https://mouchen.local"))
        assertFalse(isSecureBackendUrl("http://192.168.50.11:8788"))
        assertFalse(isSecureBackendUrl("not-a-url"))
    }

    @Test
    fun separateSpeechEndpointAndLocationRoundTripWithoutTokenDisclosure() {
        val original = BackendConnection(
            baseUrl = "https://mouchen.example.com",
            userId = "owner-1",
            bearerToken = "secret-token",
            sttBaseUrl = "https://mouchen.example.com/speech/",
            sttProcessingLocation = AudioProcessingLocation.PUBLIC_CLOUD,
        )

        val encoded = encodeBackendConnection(original)
        val decoded = decodeBackendConnection(encoded, "https://fallback.example.com")

        assertEquals("https://mouchen.example.com", decoded.baseUrl)
        assertEquals("https://mouchen.example.com/speech", decoded.sttBaseUrl)
        assertEquals(AudioProcessingLocation.PUBLIC_CLOUD, decoded.sttProcessingLocation)
        assertEquals("secret-token", decoded.bearerToken)
        assertFalse(original.toString().contains("secret-token"))
    }

    @Test
    fun speechEndpointOnAnotherOriginFailsClosed() {
        val stored = JSONObject()
            .put("base_url", "https://mouchen.example.com")
            .put("user_id", "owner")
            .put("bearer_token", "secret")
            .put("stt_base_url", "https://speech.example.com")
            .put("stt_processing_location", "public_cloud")
            .toString()

        val decoded = decodeBackendConnection(stored, "https://fallback.example.com")

        assertNull(decoded.sttBaseUrl)
    }

    @Test
    fun legacyPrivateBackendKeepsTranscriptionWithConservativeRemoteLabel() {
        val legacy = JSONObject()
            .put("base_url", "https://192.168.50.10:8788")
            .put("user_id", "owner-1")
            .put("bearer_token", "token")
            .toString()

        val decoded = decodeBackendConnection(legacy, "https://fallback.example.com")

        assertEquals("https://192.168.50.10:8788", decoded.sttBaseUrl)
        assertEquals(AudioProcessingLocation.PRIVATE_VPS, decoded.sttProcessingLocation)
    }

    @Test
    fun legacyPublicOrIncompleteSpeechConfigurationFailsClosed() {
        val legacyPublic = JSONObject()
            .put("base_url", "https://api.example.com")
            .put("bearer_token", "token")
            .toString()
        val incomplete = JSONObject(legacyPublic)
            .put("stt_base_url", "https://speech.example.com")
            .toString()

        assertNull(decodeBackendConnection(legacyPublic, "https://fallback.example.com").sttBaseUrl)
        assertNull(decodeBackendConnection(incomplete, "https://fallback.example.com").sttBaseUrl)
    }

    @Test
    fun unsupportedOnDeviceClaimNeverActivatesNetworkTranscription() {
        val stored = JSONObject()
            .put("base_url", "https://api.example.com")
            .put("stt_base_url", "https://speech.example.com")
            .put("stt_processing_location", "on_device")
            .toString()

        val decoded = decodeBackendConnection(stored, "https://fallback.example.com")

        assertNull(decoded.sttBaseUrl)
        assertEquals(AudioProcessingLocation.ON_DEVICE, decoded.sttProcessingLocation)
    }

    @Test
    fun corruptEncryptedPayloadDisablesConnection() {
        val decoded = decodeBackendConnection("not-json", "https://fallback.example.com")

        assertFalse(decoded.enabled)
        assertNull(decoded.sttBaseUrl)
    }
}
