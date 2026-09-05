package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioTranscriptOutboundPolicyTest {
    @Test
    fun transcriptKeepsRedactedTextAndAccuratePrivateVpsAttestation() {
        val event = LocalEventEntity(
            source = "android.microphone.transcript",
            type = "speech.transcript",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("transcript", "联系 owner@example.com，支付失败 123456")
                .put("language", "zh")
                .put("context", "audio_transcript")
                .put("analysis_requested", true)
                .put("raw_audio_left_device", true)
                .put("processing_location", "private_vps")
                .put("raw_audio_cloud", true)
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(event, minimizedContextOnly = true)

        assertEquals("audio_transcript", prepared.facts.getString("context"))
        assertTrue(prepared.facts.getBoolean("analysis_requested"))
        assertTrue(prepared.facts.getBoolean("raw_audio_left_device"))
        assertEquals("private_vps", prepared.facts.getString("processing_location"))
        assertTrue(prepared.facts.getBoolean("raw_audio_cloud"))
        assertFalse(prepared.facts.toString().contains("owner@example.com"))
        assertFalse(prepared.facts.toString().contains("123456"))
        assertTrue(prepared.facts.toString().contains("支付失败"))
    }
}
