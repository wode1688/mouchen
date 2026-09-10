package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OutboundCredentialSanitizerTest {
    @Test
    fun recursivelyRemovesCredentialKeysAndEmbeddedSecrets() {
        val privateKey = listOf(
            "-----BEGIN " + "PRIVATE KEY-----",
            "synthetic-test-material",
            "-----END " + "PRIVATE KEY-----",
        ).joinToString("\n")
        val source = JSONObject()
            .put("safe", "keep this useful context")
            .put("password", "NeverExportThisPassword")
            .put("passcode", "778899")
            .put("pin", 4321)
            .put("otp", "654321")
            .put("api_key", "NeverExportThisApiKey")
            .put("provider_secret", "NeverExportThisProviderSecret")
            .put("access_token", "NeverExportThisAccessToken")
            .put("auth_token", "NeverExportThisAuthToken")
            .put("credentials", "NeverExportGenericCredentials")
            .put(
                "nested",
                JSONObject()
                    .put("client_secret", "NeverExportThisClientSecret")
                    .put(
                        "items",
                        JSONArray()
                            .put("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
                            .put(JSONObject().put("refresh_token", "NeverExportThisRefreshToken"))
                            .put("JWT " + "eyJ" + "hbGciOiJIUzI1NiJ9.payload.signature")
                            .put("OpenAI " + "sk-" + "proj-synthetic-test-value")
                            .put(privateKey)
                            .put("card 4111 1111 1111 1111, CVV 123"),
                    ),
            )

        val result = OutboundCredentialSanitizer.sanitize(source)
        val serialized = result.payload.toString()

        assertTrue(result.redacted)
        assertTrue(result.payload.getBoolean(OutboundCredentialSanitizer.MARKER_KEY))
        assertEquals("keep this useful context", result.payload.getString("safe"))
        listOf(
            "password",
            "passcode",
            "pin",
            "otp",
            "api_key",
            "provider_secret",
            "access_token",
            "auth_token",
            "credentials",
        ).forEach { key -> assertFalse("Credential key survived: $key", result.payload.has(key)) }
        assertFalse(result.payload.getJSONObject("nested").has("client_secret"))
        listOf(
            "NeverExportThisPassword",
            "NeverExportThisClientSecret",
            "NeverExportThisRefreshToken",
            "abcdefghijklmnopqrstuvwxyz123456",
            "eyJhbGciOiJIUzI1NiJ9",
            "c3VwZXItc2VjcmV0LWtleS1tYXRlcmlhbA",
            "4111 1111 1111 1111",
            "CVV 123",
        ).forEach { secret -> assertFalse("Leaked: $secret", serialized.contains(secret)) }
    }

    @Test
    fun removesCommonProviderTokensAndCredentialAssignments() {
        val text = listOf(
            "password=CorrectHorseBatteryStaple",
            "PIN 4321",
            "OTP 654321",
            "api_key: api-value-that-must-not-leak",
            "github_" + "pat_" + "S".repeat(24),
            "ghp_" + "S".repeat(24),
            "xoxb-" + "synthetic-test-value",
            "AIza" + "S".repeat(35),
            "AKIA" + "S".repeat(16),
            "https://owner:basic-auth-password@example.test/path",
        ).joinToString(" | ")

        val sanitized = OutboundCredentialSanitizer.sanitizeText(text)

        listOf(
            "CorrectHorseBatteryStaple",
            "4321",
            "654321",
            "api-value-that-must-not-leak",
            "github_pat_",
            "ghp_",
            "xoxb-",
            "AIza",
            "AKIA" + "S".repeat(16),
            "basic-auth-password",
        ).forEach { secret -> assertFalse("Leaked: $secret", sanitized.contains(secret)) }
        assertTrue(sanitized.contains(OutboundCredentialSanitizer.MARKER))
        assertEquals(
            OutboundCredentialSanitizer.MARKER,
            OutboundCredentialSanitizer.sanitizeText("778899"),
        )
    }

    @Test
    fun notificationAccessibilityOcrAndImeAreSanitizedInMinimizedAndFullModes() {
        val events = listOf(
            LocalEventEntity(
                source = "android.notification",
                type = "notification.posted",
                payloadJson = JSONObject()
                    .put("package", "com.tencent.mm")
                    .put("title", "Account alert")
                    .put("text", "Authorization: Bearer NotificationSecretToken123456")
                    .toString(),
            ),
            LocalEventEntity(
                source = "android.accessibility",
                type = "ui.visible_text",
                payloadJson = JSONObject()
                    .put("package", "com.tencent.mm")
                    .put("visible_text", JSONArray().put("password=AccessibilitySecret123"))
                    .toString(),
            ),
            LocalEventEntity(
                source = "android.screen_ocr",
                type = "ui.visible_text",
                payloadJson = JSONObject()
                    .put("package", "com.example.browser")
                    .put("context", "screen_ocr")
                    .put("visible_text", JSONArray().put("API key " + "sk-" + "proj-synthetic-ocr-value"))
                    .toString(),
            ),
            LocalEventEntity(
                source = "android.ime",
                type = "ime.text_committed",
                payloadJson = JSONObject()
                    .put("package", "com.example.pay")
                    .put("text", "银行卡号 4111 1111 1111 1111，CVV 123")
                    .put("password_excluded", true)
                    .toString(),
            ),
        )
        val forbidden = listOf(
            "NotificationSecretToken123456",
            "AccessibilitySecret123",
            "OcrSecretToken1234567890",
            "4111 1111 1111 1111",
            "CVV 123",
        )

        for (event in events) {
            val minimized = OutboundEventPolicy.prepare(
                event,
                minimizedContextOnly = true,
                proactiveCloudEnabled = true,
            )
            val full = OutboundEventPolicy.prepare(
                event,
                minimizedContextOnly = false,
                proactiveCloudEnabled = true,
            )

            assertEquals("alpha.minimized_context", minimized.consentScope)
            assertEquals("owner_full_context", full.consentScope)
            for (prepared in listOf(minimized, full)) {
                val serialized = prepared.facts.toString()
                forbidden.forEach { secret ->
                    assertFalse("${event.source}/${prepared.consentScope} leaked $secret", serialized.contains(secret))
                }
                assertTrue(prepared.facts.getBoolean(OutboundCredentialSanitizer.MARKER_KEY))
                assertTrue(prepared.facts.has("content_kind"))
                assertTrue(prepared.facts.has("session_key"))
            }
        }
    }

    @Test
    fun fullContextRetainsSafeNestedContextAfterRecursiveSanitization() {
        val event = LocalEventEntity(
            source = "android.owner_import",
            type = "threat.detected",
            payloadJson = JSONObject()
                .put(
                    "analysis",
                    JSONObject()
                        .put("summary", "Deployment is blocked")
                        .put("auth_token", "NestedTokenMustNotLeave")
                        .put("steps", JSONArray().put("rotate token").put("retry deployment")),
                )
                .toString(),
        )

        val prepared = OutboundEventPolicy.prepare(
            event,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )

        assertEquals("owner_full_context", prepared.consentScope)
        assertEquals("Deployment is blocked", prepared.facts.getJSONObject("analysis").getString("summary"))
        assertFalse(prepared.facts.getJSONObject("analysis").has("auth_token"))
        assertFalse(prepared.facts.toString().contains("NestedTokenMustNotLeave"))
        assertEquals("rotate token", prepared.facts.getJSONObject("analysis").getJSONArray("steps").getString(0))
    }
}
