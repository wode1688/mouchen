package com.mouchen.app.sync

import com.mouchen.app.localization.LOCALE_EN_US
import com.mouchen.app.localization.LOCALE_ZH_CN
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AccountLocaleClientTest {
    @Test
    fun putBodyUsesTheSharedBackendContract() {
        assertEquals(LOCALE_EN_US, JSONObject(encodeAccountLocaleRequest(LOCALE_EN_US)).getString("locale"))
    }

    @Test
    fun responseAcceptsFlatAndWrappedPreferencesShapes() {
        assertEquals(LOCALE_ZH_CN, parseAccountLocaleResponse("""{"locale":"zh-CN"}"""))
        assertEquals(LOCALE_EN_US, parseAccountLocaleResponse("""{"preferences":{"locale":"en-US"}}"""))
        assertNull(parseAccountLocaleResponse("""{"locale":"fr-FR"}"""))
    }

    @Test
    fun outboundUpdateRemainsBoundToTheCapturedUserAndToken() {
        val expected = AuthSession(
            userId = "user-a",
            username = "owner-a",
            accessToken = "token-a",
            sessionId = "session-a",
            deviceId = "device-a",
            serverOrigin = "https://example.test",
            epoch = 7L,
        )

        assertTrue(accountLocaleSessionMatches(expected, expected.copy(locale = LOCALE_EN_US)))
        assertFalse(accountLocaleSessionMatches(expected, expected.copy(userId = "user-b")))
        assertFalse(accountLocaleSessionMatches(expected, expected.copy(accessToken = "token-b")))
        assertFalse(accountLocaleSessionMatches(expected, expected.copy(sessionId = "session-b")))
    }

    @Test
    fun localeMergePreservesCurrentSessionAndRejectsAConcurrentCanonicalReplacement() {
        val expected = AuthSession(
            userId = "user-a",
            username = "owner-a",
            accessToken = "token-a",
            sessionId = "session-a",
            deviceId = "device-a",
            serverOrigin = "https://example.test",
            epoch = 7L,
            locale = LOCALE_ZH_CN,
        )

        val merged = mergeAccountLocaleIfSessionMatches(expected, expected, LOCALE_EN_US)
        assertEquals(expected.copy(locale = LOCALE_EN_US), merged)
        assertNull(
            mergeAccountLocaleIfSessionMatches(
                expected,
                expected.copy(sessionId = "new-session", username = "renamed-owner"),
                LOCALE_EN_US,
            ),
        )
    }
}
