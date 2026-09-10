package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ContentObservationMetadataTest {
    @Test
    fun wechatVisibleTextIsContextNotOwnerSpeechAndUsesBoundedWindow() {
        val first = event(10_001, "张三：项目今天必须做完")
        val second = event(19_999, "张三：项目今天必须做完")

        val a = ContentObservationMetadata.enrich(first, JSONObject(first.payloadJson))
        val b = ContentObservationMetadata.enrich(second, JSONObject(second.payloadJson))

        assertEquals("chat", a.getString("content_kind"))
        assertEquals("unknown", a.getString("speaker"))
        assertEquals("contextual", a.getString("evidence_strength"))
        assertTrue(a.getBoolean("visible_only"))
        assertEquals(a.getString("session_key"), b.getString("session_key"))
    }

    @Test
    fun sessionKeyNeverContainsContactTextOrUrlQuery() {
        val payload = JSONObject()
            .put("package", "com.android.chrome")
            .put("visible_text", JSONArray().put("张三 https://example.com/?token=secret 搜索贷款"))
        val event = LocalEventEntity(
            source = "android.accessibility",
            type = "ui.visible_text",
            occurredAt = 20_000,
            payloadJson = payload.toString(),
        )

        val facts = ContentObservationMetadata.enrich(event, payload)
        val key = facts.getString("session_key")

        assertEquals("search", facts.getString("content_kind"))
        assertFalse(key.contains("张三"))
        assertFalse(key.contains("secret"))
        assertTrue(Regex("search-[a-f0-9]{24}").matches(key))
    }

    @Test
    fun imeContentIsDirectOwnerInput() {
        val event = LocalEventEntity(
            source = "android.ime",
            type = "ime.text_committed",
            occurredAt = 20_000,
            payloadJson = JSONObject().put("package", "com.tencent.mm").put("text", "我明天完成").toString(),
        )

        val facts = ContentObservationMetadata.enrich(event, JSONObject(event.payloadJson))

        assertEquals("user_input", facts.getString("content_kind"))
        assertEquals("user", facts.getString("speaker"))
        assertEquals("outbound", facts.getString("message_direction"))
        assertEquals("direct", facts.getString("evidence_strength"))
    }

    @Test
    fun chatNotificationIsContextualRatherThanSystemFact() {
        val event = LocalEventEntity(
            source = "android.notification",
            type = "notification.posted",
            occurredAt = 30_000,
            payloadJson = JSONObject()
                .put("package", "com.tencent.mm")
                .put("text", "项目延期了")
                .toString(),
        )

        val facts = ContentObservationMetadata.enrich(event, JSONObject(event.payloadJson))

        assertEquals("chat", facts.getString("content_kind"))
        assertEquals("unknown", facts.getString("speaker"))
        assertEquals("contextual", facts.getString("evidence_strength"))
    }

    @Test
    fun nativeVideoAppIsClassifiedWithoutDependingOnTextKeywords() {
        val event = LocalEventEntity(
            source = "android.screen_ocr",
            type = "ui.visible_text",
            occurredAt = 40_000,
            payloadJson = JSONObject()
                .put("package", "com.google.android.youtube")
                .put("visible_text", JSONArray().put("A title"))
                .toString(),
        )

        val facts = ContentObservationMetadata.enrich(event, JSONObject(event.payloadJson))

        assertEquals("video", facts.getString("content_kind"))
        assertTrue(facts.getBoolean("visible_only"))
        assertEquals("inferred", facts.getString("evidence_strength"))
    }

    @Test
    fun explicitOwnerReportIsDirectSelfEvidenceNotScreenObservation() {
        val event = LocalEventEntity(
            source = "android.self_report",
            type = "ui.visible_text",
            occurredAt = 50_000,
            payloadJson = JSONObject()
                .put("context", "owner_self_report")
                .put("visible_text", JSONArray().put("我决定今天完成报价"))
                .toString(),
        )

        val facts = ContentObservationMetadata.enrich(event, JSONObject(event.payloadJson))

        assertEquals("user_input", facts.getString("content_kind"))
        assertEquals("user", facts.getString("speaker"))
        assertEquals("self", facts.getString("message_direction"))
        assertEquals("direct", facts.getString("evidence_strength"))
        assertFalse(facts.getBoolean("visible_only"))
    }

    private fun event(at: Long, text: String) = LocalEventEntity(
        source = "android.accessibility",
        type = "ui.visible_text",
        occurredAt = at,
        payloadJson = JSONObject()
            .put("package", "com.tencent.mm")
            .put("visible_text", JSONArray().put(text))
            .toString(),
    )
}
