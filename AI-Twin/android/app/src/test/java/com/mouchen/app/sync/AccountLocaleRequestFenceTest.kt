package com.mouchen.app.sync

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AccountLocaleRequestFenceTest {
    private val original = AuthSession(
        userId = "user-a",
        username = "owner-a",
        accessToken = "token-a",
        sessionId = "session-a",
        deviceId = "device",
        serverOrigin = "https://example.test",
        epoch = 1L,
    )

    @Test
    fun oldSuccessOrFailureCannotApplyAfterLogoutInvalidatesGeneration() {
        val fence = AccountLocaleRequestFence.capture(7L, original)

        assertFalse(fence.matches(8L, null))
        assertFalse(fence.matches(8L, original))
    }

    @Test
    fun oldResponseCannotApplyToReplacementAccountOrRotatedToken() {
        val fence = AccountLocaleRequestFence.capture(7L, original)

        assertFalse(fence.matches(7L, original.copy(userId = "user-b", accessToken = "token-b")))
        assertFalse(fence.matches(7L, original.copy(accessToken = "rotated-token")))
        assertFalse(fence.matches(7L, original.copy(sessionId = "replacement-session")))
    }

    @Test
    fun responseAppliesOnlyToTheExactStillCurrentSession() {
        val fence = AccountLocaleRequestFence.capture(7L, original)

        assertTrue(fence.matches(7L, original.copy(locale = "en-US")))
    }

    @Test
    fun diagnosticsNeverExposeTheAccessToken() {
        val fence = AccountLocaleRequestFence.capture(7L, original)

        assertFalse(fence.toString().contains(original.accessToken))
    }
}
