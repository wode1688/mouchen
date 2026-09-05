package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.json.JSONArray
import org.json.JSONObject

class OutboundEventPolicyTest {
    @Test
    fun redactionRemovesCommonIdentifiers() {
        val redacted = OutboundEventPolicy.redactText(
            "联系 owner@example.com 或 +86 138 0013 8000，验证码 123456，详情 https://example.com/x",
        )

        assertFalse(redacted.contains("owner@example.com"))
        assertFalse(redacted.contains("138 0013 8000"))
        assertFalse(redacted.contains("123456"))
        assertFalse(redacted.contains("https://"))
        assertTrue(redacted.contains("[email]"))
        assertTrue(redacted.contains("[phone]"))
        assertTrue(redacted.contains("[number]"))
        assertTrue(redacted.contains("[url]"))
    }

    @Test
    fun redactionRemovesParenthesizedPhoneFormats() {
        val redacted = OutboundEventPolicy.redactText("Call +86 (138) 0013-8000 now")

        assertFalse(redacted.contains("138"))
        assertFalse(redacted.contains("8000"))
        assertTrue(redacted.contains("[phone]"))
    }

    @Test
    fun minimizedNotificationKeepsUsefulRedactedContext() {
        val event = LocalEventEntity(
            source = "android.notification",
            type = "notification.posted",
            payloadJson = JSONObject()
                .put("package", "com.example.deploy")
                .put("title", "发布失败")
                .put("big_text", "联系 owner@example.com，连接超时")
                .put("text_lines", JSONArray(listOf("验证码 123456", "切换备用通道")))
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(event, minimizedContextOnly = true)

        assertEquals("alpha.minimized_context", prepared.consentScope)
        assertEquals("com.example.deploy", prepared.facts.getString("package"))
        assertFalse(prepared.facts.toString().contains("owner@example.com"))
        assertFalse(prepared.facts.toString().contains("123456"))
        assertTrue(prepared.facts.toString().contains("连接超时"))
        assertTrue(prepared.facts.toString().contains("备用通道"))
    }

    @Test
    fun minimizedSelfReportKeepsAnalysisIntent() {
        val event = LocalEventEntity(
            source = "android.self_report",
            type = "ui.visible_text",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("visible_text", JSONArray().put("我最近一直避开产品发布这件事"))
                .put("context", "owner_self_report")
                .put("analysis_requested", true)
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(event, minimizedContextOnly = true)

        assertEquals("owner_self_report", prepared.facts.getString("context"))
        assertTrue(prepared.facts.getBoolean("analysis_requested"))
        assertTrue(prepared.facts.toString().contains("避开产品发布"))
    }

    @Test
    fun minimizedConversationImportKeepsBoundedGroupingMetadata() {
        val event = LocalEventEntity(
            source = "android.conversation_import",
            type = "ui.visible_text",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("visible_text", JSONArray().put("selected text"))
                .put("context", "conversation_import")
                .put("analysis_requested", true)
                .put("import_id", "import-123")
                .put("chunk_index", 2)
                .put("chunk_count", 3)
                .put("source_bytes", 1024)
                .put("source_mime", "text/plain")
                .toString(),
        )

        val facts = OutboundEventPolicy.prepare(event, minimizedContextOnly = true).facts

        assertEquals("conversation_import", facts.getString("context"))
        assertEquals("import-123", facts.getString("import_id"))
        assertEquals(2, facts.getInt("chunk_index"))
        assertEquals(3, facts.getInt("chunk_count"))
        assertEquals(1024, facts.getInt("source_bytes"))
        assertEquals("text/plain", facts.getString("source_mime"))
    }

    @Test
    fun minimizedScreenOcrKeepsOnlyUsefulOcrMetadataAlongsideRedactedText() {
        val event = LocalEventEntity(
            source = "android.screen_ocr",
            type = "ui.visible_text",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("package", "com.example.gallery")
                .put("context", "screen_ocr")
                .put("analysis_requested", true)
                .put("visible_text", JSONArray().put("deadline owner@example.com"))
                .put("ocr_engine", "mlkit_latin")
                .put("ocr_local", true)
                .put("source_frame_blob", "private-frame-name")
                .put("character_count", 26)
                .put("device_locked_excluded", true)
                .put("secure_surface_bypass", false)
                .toString(),
        )

        val facts = OutboundEventPolicy.prepare(event, minimizedContextOnly = true).facts

        assertEquals("screen_ocr", facts.getString("context"))
        assertEquals("mlkit_latin", facts.getString("ocr_engine"))
        assertTrue(facts.getBoolean("ocr_local"))
        assertEquals(26, facts.getInt("character_count"))
        assertTrue(facts.getBoolean("device_locked_excluded"))
        assertFalse(facts.getBoolean("secure_surface_bypass"))
        assertFalse(facts.has("source_frame_blob"))
        assertFalse(facts.toString().contains("owner@example.com"))
    }

    @Test
    fun screenOcrDiagnosticStaysMetadataOnlyWhenRawCloudIsApproved() {
        val event = LocalEventEntity(
            source = "android.screen",
            type = "screen.ocr_diagnostic",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("status", "error")
                .put("stage", "latin_recognizer")
                .put("reason", "recognizer_failure")
                .put("engine", "mlkit_latin")
                .put("exception_type", "MlKitException")
                .put("recognized_character_count", 0)
                .put("frame_age_ms", 8_000)
                .put("frames_in_session", 2)
                .put("raw_text", "must-not-leave-device")
                .put("exception_message", "may contain user data")
                .put("blob", "private-frame-name")
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(
            event,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )
        val facts = prepared.facts

        assertEquals("alpha.minimized_context", prepared.consentScope)
        assertEquals("error", facts.getString("status"))
        assertEquals("latin_recognizer", facts.getString("stage"))
        assertEquals("MlKitException", facts.getString("exception_type"))
        assertEquals(8_000, facts.getInt("frame_age_ms"))
        assertFalse(facts.has("raw_text"))
        assertFalse(facts.has("exception_message"))
        assertFalse(facts.has("blob"))
    }

    @Test
    fun minimizedLocationKeepsOnlyCoarseGridMetadata() {
        val event = LocalEventEntity(
            source = "android.location",
            type = "location.snapshot",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("grid_latitude", 31.225)
                .put("grid_longitude", 121.475)
                .put("grid_size_degrees", 0.05)
                .put("grid_longitude_size_degrees", 0.058)
                .put("precision_m", 5_000)
                .put("age_ms", 12_000)
                .put("latitude", 31.230416)
                .put("longitude", 121.473701)
                .put("address", "private address")
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(event, minimizedContextOnly = true)

        assertEquals(31.225, prepared.facts.getDouble("grid_latitude"), 0.0)
        assertEquals(121.475, prepared.facts.getDouble("grid_longitude"), 0.0)
        assertEquals(0.058, prepared.facts.getDouble("grid_longitude_size_degrees"), 0.0)
        assertEquals(5_000, prepared.facts.getInt("precision_m"))
        assertFalse(prepared.facts.has("latitude"))
        assertFalse(prepared.facts.has("longitude"))
        assertFalse(prepared.facts.has("address"))
    }

    @Test
    fun minimizedSmsRedactsBodyAndNeverExportsRawAddress() {
        val event = LocalEventEntity(
            source = "android.sms",
            type = "message.sms",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("direction", "received")
                .put("party_ref", "a".repeat(64))
                .put("party_display", "owner@example.com")
                .put("address", "+86 138 0013 8000")
                .put("received_at_ms", 1_700_000_000_000L)
                .put("body", "Call +86 138 0013 8000 or owner@example.com code 123456")
                .toString(),
        )

        val facts = OutboundEventPolicy.prepare(event, minimizedContextOnly = true).facts

        assertFalse(facts.has("address"))
        assertFalse(facts.toString().contains("138 0013 8000"))
        assertFalse(facts.toString().contains("owner@example.com"))
        assertTrue(facts.getString("body").contains("[phone]"))
        assertTrue(facts.getString("body").contains("[email]"))
    }

    @Test
    fun minimizedCallAndContactExportOnlyLinkageMetadata() {
        val call = LocalEventEntity(
            source = "android.call_log",
            type = "call.observed",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("direction", "incoming")
                .put("party_ref", "b".repeat(64))
                .put("party_display", "Private Contact")
                .put("raw_number", "13800138000")
                .put("started_at_ms", 1_700_000_000_000L)
                .put("duration_seconds", 60)
                .toString(),
        )
        val contact = LocalEventEntity(
            source = "android.contacts",
            type = "contact.identifier",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("contact_ref", "c".repeat(64))
                .put("identifier_kind", "phone")
                .put("identifier_hash", "d".repeat(64))
                .put("display_name", "Private Contact")
                .put("raw_number", "13800138000")
                .put("last_updated_ms", 1_700_000_000_000L)
                .toString(),
        )

        val callFacts = OutboundEventPolicy.prepare(call, minimizedContextOnly = true).facts
        val contactFacts = OutboundEventPolicy.prepare(contact, minimizedContextOnly = true).facts

        assertFalse(callFacts.has("party_display"))
        assertFalse(callFacts.has("raw_number"))
        assertFalse(contactFacts.has("display_name"))
        assertFalse(contactFacts.has("raw_number"))
        assertEquals("phone", contactFacts.getString("identifier_kind"))
    }

    @Test
    fun disablingMinimizationAloneDoesNotEnableRawPrivatePhoneData() {
        val sms = LocalEventEntity(
            source = "android.sms",
            type = "message.sms",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("direction", "received")
                .put("party_ref", "A".repeat(64))
                .put("party_display", "Private Contact")
                .put("address", "+86 138 0013 8000")
                .put("received_at_ms", 1_700_000_000_000L)
                .put("body", "Call +86 138 0013 8000 or owner@example.com code 123456")
                .toString(),
        )
        val call = LocalEventEntity(
            source = "android.call_log",
            type = "call.observed",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("direction", "incoming")
                .put("party_ref", "not-a-digest-13800138000")
                .put("party_display", "Private Contact")
                .put("raw_number", "13800138000")
                .put("started_at_ms", 1_700_000_000_000L)
                .put("duration_seconds", 60)
                .toString(),
        )
        val contact = LocalEventEntity(
            source = "android.contacts",
            type = "contact.identifier",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("contact_ref", "c".repeat(64))
                .put("identifier_kind", "phone")
                .put("identifier_hash", "d".repeat(64))
                .put("display_name", "Private Contact")
                .put("raw_number", "13800138000")
                .put("last_updated_ms", 1_700_000_000_000L)
                .toString(),
        )

        val smsPrepared = OutboundEventPolicy.prepare(sms, minimizedContextOnly = false)
        val callPrepared = OutboundEventPolicy.prepare(call, minimizedContextOnly = false)
        val contactPrepared = OutboundEventPolicy.prepare(contact, minimizedContextOnly = false)

        assertEquals("alpha.private_phone_minimized", smsPrepared.consentScope)
        assertEquals("alpha.private_phone_minimized", callPrepared.consentScope)
        assertEquals("alpha.private_phone_minimized", contactPrepared.consentScope)
        assertFalse(smsPrepared.facts.has("party_display"))
        assertFalse(smsPrepared.facts.has("address"))
        assertFalse(smsPrepared.facts.toString().contains("138 0013 8000"))
        assertFalse(smsPrepared.facts.toString().contains("owner@example.com"))
        assertFalse(smsPrepared.facts.toString().contains("123456"))
        assertEquals("a".repeat(64), smsPrepared.facts.getString("party_ref"))
        assertFalse(callPrepared.facts.has("party_ref"))
        assertFalse(callPrepared.facts.has("party_display"))
        assertFalse(callPrepared.facts.has("raw_number"))
        assertFalse(contactPrepared.facts.has("display_name"))
        assertFalse(contactPrepared.facts.has("raw_number"))
        assertEquals("c".repeat(64), contactPrepared.facts.getString("contact_ref"))
        assertEquals("d".repeat(64), contactPrepared.facts.getString("identifier_hash"))
    }

    @Test
    fun privatePhoneBodyRequiresBothCloudAndFullContextApproval() {
        val sms = LocalEventEntity(
            source = "android.sms",
            type = "message.sms",
            sensitivity = "sensitive",
            payloadJson = JSONObject()
                .put("direction", "received")
                .put("party_ref", "a".repeat(64))
                .put("party_display", "Private Contact")
                .put("address", "+86 138 0013 8000")
                .put("received_at_ms", 1_700_000_000_000L)
                .put("body", "今晚八点在图书馆见，带上项目方案原稿。")
                .toString(),
        )

        val cloudOnly = OutboundEventPolicy.prepare(
            sms,
            minimizedContextOnly = true,
            proactiveCloudEnabled = true,
        )
        val fullContextOnly = OutboundEventPolicy.prepare(
            sms,
            minimizedContextOnly = false,
            proactiveCloudEnabled = false,
        )
        val doubleApproved = OutboundEventPolicy.prepare(
            sms,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )

        assertEquals("alpha.private_phone_minimized", cloudOnly.consentScope)
        assertEquals("alpha.private_phone_minimized", fullContextOnly.consentScope)
        assertEquals("owner_full_context", doubleApproved.consentScope)
        assertEquals("今晚八点在图书馆见，带上项目方案原稿。", doubleApproved.facts.getString("body"))
        assertEquals("a".repeat(64), doubleApproved.facts.getString("party_ref"))
        assertFalse(doubleApproved.facts.has("party_display"))
        assertFalse(doubleApproved.facts.has("address"))
    }

    @Test
    fun doubleApprovalStillExcludesOtpPaymentAndPasswordContent() {
        val otpSms = LocalEventEntity(
            source = "android.sms",
            type = "message.sms",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("party_ref", "b".repeat(64))
                .put("body", "支付验证码 123456，请勿告诉他人")
                .toString(),
        )
        val malformedImePasswordEvent = LocalEventEntity(
            source = "android.ime",
            type = "ime.text_committed",
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("package", "com.example.pay")
                .put("text", "secret-value")
                .put("password_excluded", false)
                .toString(),
        )

        val otpPrepared = OutboundEventPolicy.prepare(
            otpSms,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )
        val imePrepared = OutboundEventPolicy.prepare(
            malformedImePasswordEvent,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )

        assertEquals("owner_full_context", otpPrepared.consentScope)
        assertFalse(otpPrepared.facts.toString().contains("123456"))
        assertTrue(otpPrepared.facts.getBoolean("sensitive_content_excluded"))
        assertFalse(imePrepared.facts.has("text"))
        assertTrue(imePrepared.facts.getBoolean("sensitive_content_excluded"))
    }
}
