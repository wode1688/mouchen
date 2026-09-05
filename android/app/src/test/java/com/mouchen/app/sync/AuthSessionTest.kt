package com.mouchen.app.sync

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import com.mouchen.app.localization.LOCALE_EN_US

class AuthSessionTest {
    @Test
    fun sessionRoundTripKeepsTokenOutOfToStringSurfaces() {
        val original = AuthSession(
            "user-1",
            "mouchen",
            "secret-token",
            "session-1",
            "android-device",
            serverOrigin = "https://mouchen.example.com",
            epoch = 7,
            locale = LOCALE_EN_US,
        )
        assertEquals(original, decodeAuthSession(encodeAuthSession(original)))
        assertFalse(original.toString().contains("secret-token"))
    }

    @Test
    fun canonicalUserComesFromSessionEndpointNotLoginForm() {
        val parsed = parseCanonicalSession(
            """{"session":{"id":"s1","user_id":"server-user","username":"Canonical","device_id":"device-1"}}""",
            "token",
            "fallback",
        )
        assertEquals("server-user", parsed?.userId)
        assertEquals("Canonical", parsed?.username)
        assertEquals("s1", parsed?.sessionId)
        assertEquals("zh-CN", parsed?.locale)
    }

    @Test
    fun nestedUserShapeIsAcceptedButMissingUserIsRejected() {
        assertEquals(
            "u2",
            parseCanonicalSession("""{"user":{"id":"u2","username":"owner"}}""", "token", "device")?.userId,
        )
        assertNull(parseCanonicalSession("{}", "token", "device"))
    }

    @Test
    fun credentialValidationMatchesCommercialContract() {
        assertNull(validateCredentials("owner", "long-password"))
        assertTrue(validateCredentials("ab", "long-password")!!.contains("用户名"))
        assertTrue(validateCredentials("bad name", "long-password")!!.contains("空格"))
        assertTrue(validateCredentials("owner", "short")!!.contains("密码"))
        assertNull(validateCredentials("owner", "short", requireStrongPassword = false))
        assertTrue(validateCredentials("owner", "", requireStrongPassword = false)!!.contains("密码"))
        assertFalse(isValidDeviceId("bad device"))
    }

    @Test
    fun registrationCarriesInviteButLoginNeverDoes() {
        val registration = JSONObject(
            buildAuthRequestJson(
                AuthMode.REGISTER,
                " owner ",
                "long-password",
                "device-id",
                "Android Phone",
                "  one-use-invite  ",
                LOCALE_EN_US,
            ),
        )
        val login = JSONObject(
            buildAuthRequestJson(
                AuthMode.LOGIN,
                "owner",
                "password",
                "device-id",
                "Android Phone",
                "must-not-leak",
            ),
        )

        assertEquals("owner", registration.getString("username"))
        assertEquals("one-use-invite", registration.getString("registration_code"))
        assertEquals(LOCALE_EN_US, registration.getString("locale"))
        assertFalse(login.has("registration_code"))
        assertFalse(login.has("locale"))
    }

    @Test
    fun canonicalSessionLocaleIsServerAuthoritative() {
        val parsed = parseCanonicalSession(
            """{"session":{"user_id":"u1","device_id":"d1","locale":"en-US"}}""",
            "token",
            "fallback",
        )
        assertEquals(LOCALE_EN_US, parsed?.locale)
    }

    @Test
    fun invalidSessionStatusesForceRelogin() {
        assertTrue(shouldInvalidateSessionStatus(401))
        assertTrue(shouldInvalidateSessionStatus(403))
        assertFalse(shouldInvalidateSessionStatus(429))
        assertFalse(shouldInvalidateSessionStatus(503))
    }

    @Test
    fun sameAccountKeepsRowsAndAccountSwitchClearsRows() {
        assertFalse(shouldClearAccountLocalData(null, "u1"))
        assertFalse(shouldClearAccountLocalData("u1", "u1"))
        assertTrue(shouldClearAccountLocalData("u1", "u2"))
        val legacy = BackendConnection(
            baseUrl = "https://mouchen.example.com",
            userId = "legacy-owner",
            bearerToken = "legacy-token",
        )
        assertEquals("legacy-owner", previousAccountUserId(null, legacy))
        assertFalse(shouldClearAccountLocalData(previousAccountUserId(null, legacy), "legacy-owner"))
        assertTrue(shouldClearAccountLocalData(previousAccountUserId(null, legacy), "new-user"))
        assertFalse(
            shouldClearAccountLocalData(
                "u1",
                "u1",
                "https://mouchen.example.com/api",
                "https://mouchen.example.com",
            ),
        )
        assertTrue(
            shouldClearAccountLocalData(
                "u1",
                "u1",
                "https://mouchen.example.com",
                "https://other.example.com",
            ),
        )
    }

    @Test
    fun legacyMarkerSurvivesSecureSessionEncoding() {
        val original = AuthSession("user-1", "owner", "token", "legacy-session", "device", legacy = true)

        assertTrue(decodeAuthSession(encodeAuthSession(original))!!.legacy)
    }
}
