package com.mouchen.app.collectors

import android.provider.CallLog
import android.provider.Telephony
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PrivatePhoneContextPolicyTest {
    private val secret = ByteArray(32) { it.toByte() }

    @Test
    fun phoneFormattingMapsToOneIrreversibleLinkHash() {
        val formatted = phoneContextParticipant(secret, "+86 138-0013-8000")
        val compact = phoneContextParticipant(secret, "8613800138000")

        assertEquals(ParticipantKind.PHONE, formatted.kind)
        assertEquals(compact.identifierHash, formatted.identifierHash)
        assertEquals("***8000", formatted.masked)
        assertFalse(formatted.identifierHash.contains("13800138000"))
        assertEquals(64, formatted.identifierHash.length)
    }

    @Test
    fun identifierHashIsInstallScopedAndEmailMaskDoesNotRetainAddress() {
        val address = "Owner.Name@example.com"
        val first = phoneContextParticipant(secret, address)
        val otherInstall = phoneContextParticipant(ByteArray(32) { (it + 1).toByte() }, address)

        assertEquals(ParticipantKind.EMAIL, first.kind)
        assertNotEquals(first.identifierHash, otherInstall.identifierHash)
        assertFalse(first.masked.contains("Owner.Name", ignoreCase = true))
        assertFalse(first.masked.contains("example", ignoreCase = true))
        assertTrue(first.masked.endsWith(".com"))
    }

    @Test
    fun stableEventIdDeduplicatesSameProviderRecord() {
        val first = stablePhoneContextEventId("sms", secret, 42L, 1_700_000_000_000L)
        val duplicate = stablePhoneContextEventId("sms", secret, 42L, 1_700_000_000_000L)
        val changed = stablePhoneContextEventId("sms", secret, 43L, 1_700_000_000_000L)

        assertEquals(first, duplicate)
        assertNotEquals(first, changed)
        assertFalse(first.contains("1700000000000"))
    }

    @Test
    fun firstRunBackfillsSevenDaysAndFullPageResumesExactly() {
        val now = 10L * 24 * 60 * 60 * 1000
        assertEquals(now - PHONE_CONTEXT_LOOKBACK_MS, initialTimelineCursor(now).timestampMs)

        val last = TimelineCursor(now - 1_000L, 99L)
        assertEquals(last, nextTimelineCursor(now, PHONE_CONTEXT_PAGE_SIZE, last))
        assertTrue(nextTimelineCursor(now, PHONE_CONTEXT_PAGE_SIZE - 1, last).timestampMs < last.timestampMs)
    }

    @Test
    fun staleWatermarkIsClampedToRollingSevenDayWindow() {
        val now = 20L * 24 * 60 * 60 * 1000
        val clamped = clampTimelineCursor(TimelineCursor(1L, 123L), now)

        assertEquals(now - PHONE_CONTEXT_LOOKBACK_MS, clamped.timestampMs)
        assertEquals(-1L, clamped.rowId)
    }

    @Test
    fun providerTypesBecomeExplicitDirections() {
        assertEquals("received", smsDirection(Telephony.Sms.MESSAGE_TYPE_INBOX))
        assertEquals("sent", smsDirection(Telephony.Sms.MESSAGE_TYPE_SENT))
        assertEquals("incoming", callDirection(CallLog.Calls.INCOMING_TYPE))
        assertEquals("outgoing", callDirection(CallLog.Calls.OUTGOING_TYPE))
        assertEquals("missed", callDirection(CallLog.Calls.MISSED_TYPE))
    }
}
