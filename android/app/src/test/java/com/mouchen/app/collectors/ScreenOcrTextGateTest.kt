package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ScreenOcrTextGateTest {
    @Test
    fun normalizesBoundsAndAcceptsMeaningfulChineseAndLatinText() {
        val gate = ScreenOcrTextGate(maxLines = 3, maxLineCharacters = 10, maxTotalCharacters = 18)

        val accepted = gate.accept("  项目   截止日期  \nProject needs attention today\n项目 截止日期\nignored", 1_000)

        assertNotNull(accepted)
        assertEquals(listOf("项目 截止日期", "Project ne"), accepted!!.lines)
        assertEquals(18, accepted.characterCount)
    }

    @Test
    fun rejectsExactRepeatUntilWindowExpires() {
        val gate = ScreenOcrTextGate(exactRepeatWindowMs = 1_000, nearRepeatWindowMs = 0)

        assertNotNull(gate.accept("Client deadline tomorrow", 1_000))
        assertNull(gate.accept("Client deadline tomorrow", 1_999))
        assertNotNull(gate.accept("Client deadline tomorrow", 2_000))
    }

    @Test
    fun rejectsNearDuplicateDynamicScreen() {
        val gate = ScreenOcrTextGate(exactRepeatWindowMs = 10_000, nearRepeatWindowMs = 2_000)
        val base = "项目交付风险：客户尚未确认最终方案，请今天完成确认并记录反馈。"

        assertNotNull(gate.accept("$base 10:31", 1_000))
        assertNull(gate.accept("$base 10:32", 2_000))
        assertNotNull(gate.accept("完全不同的页面：预算审批已经通过，可以开始执行采购。", 2_100))
    }

    @Test
    fun rejectsBlankPunctuationAndMaskedPasswordNoise() {
        val gate = ScreenOcrTextGate()

        assertNull(gate.accept("\n •••••• \n ----", 1_000))
        assertNull(gate.accept("12", 2_000))
    }

    @Test
    fun reportsEmptyMeaninglessExactAndNearDuplicateReasonsWithoutText() {
        val gate = ScreenOcrTextGate(exactRepeatWindowMs = 10_000, nearRepeatWindowMs = 2_000)

        val empty = gate.evaluate("  \n\t", 1_000) as ScreenOcrGateResult.Rejected
        assertEquals(ScreenOcrRejectionReason.EMPTY_TEXT, empty.reason)
        assertEquals(4, empty.recognizedCharacterCount)

        val meaningless = gate.evaluate("12", 2_000) as ScreenOcrGateResult.Rejected
        assertEquals(ScreenOcrRejectionReason.NOT_MEANINGFUL, meaningless.reason)
        assertEquals(2, meaningless.recognizedCharacterCount)

        assertTrue(gate.evaluate("Project deadline tomorrow", 3_000) is ScreenOcrGateResult.Accepted)
        val exact = gate.evaluate("Project deadline tomorrow", 3_500) as ScreenOcrGateResult.Rejected
        assertEquals(ScreenOcrRejectionReason.EXACT_REPEAT, exact.reason)

        val nearGate = ScreenOcrTextGate(
            exactRepeatWindowMs = 10_000,
            nearRepeatWindowMs = 2_000,
            nearDuplicateThreshold = 0.90,
        )
        assertTrue(
            nearGate.evaluate(
                "Client delivery risk: final plan still needs approval 10:31",
                5_000,
            ) is ScreenOcrGateResult.Accepted,
        )
        val near = nearGate.evaluate(
            "Client delivery risk: final plan still needs approval 10:32",
            5_500,
        ) as ScreenOcrGateResult.Rejected
        assertEquals(ScreenOcrRejectionReason.NEAR_REPEAT, near.reason)
    }

    @Test
    fun recentAccessibilityPasswordSignalExcludesScreenCapture() {
        assertEquals(true, isRecentPasswordScreen(passwordSeenAt = 1_000, now = 31_000))
        assertEquals(false, isRecentPasswordScreen(passwordSeenAt = 1_000, now = 31_001))
        assertEquals(false, isRecentPasswordScreen(passwordSeenAt = 5_000, now = 4_000))
        assertEquals(false, isRecentPasswordScreen(passwordSeenAt = Long.MIN_VALUE, now = 4_000))
    }

    @Test
    fun captureRequiresEnabledAccessibilityAndRecentForegroundPackage() {
        val allowed = screenCaptureContext(
            accessibilityProtectionEnabled = true,
            foregroundPackage = "com.example.notes",
            foregroundSeenAt = 9_000,
            passwordSeenAt = Long.MIN_VALUE,
            now = 10_000,
            ownPackage = "com.mouchen.app.alpha",
        )
        assertEquals("com.example.notes", allowed?.foregroundPackage)

        assertNull(
            screenCaptureContext(
                accessibilityProtectionEnabled = false,
                foregroundPackage = "com.example.notes",
                foregroundSeenAt = 9_000,
                passwordSeenAt = Long.MIN_VALUE,
                now = 10_000,
                ownPackage = "com.mouchen.app.alpha",
            ),
        )
        assertNull(
            screenCaptureContext(
                accessibilityProtectionEnabled = true,
                foregroundPackage = null,
                foregroundSeenAt = Long.MIN_VALUE,
                passwordSeenAt = Long.MIN_VALUE,
                now = 10_000,
                ownPackage = "com.mouchen.app.alpha",
            ),
        )
        assertNull(
            screenCaptureContext(
                accessibilityProtectionEnabled = true,
                foregroundPackage = "com.example.notes",
                foregroundSeenAt = 0,
                passwordSeenAt = Long.MIN_VALUE,
                now = 10_001,
                ownPackage = "com.mouchen.app.alpha",
            ),
        )
    }

    @Test
    fun delayedOcrCompletionIgnoresSignalAgeButRejectsNewRiskSignals() {
        val captured = ScreenCaptureContext(
            foregroundPackage = "com.example.gallery",
            foregroundSeenAt = 1_000,
            passwordSeenAt = Long.MIN_VALUE,
            foregroundRevision = 4,
            passwordRevision = 7,
        )

        // No new signal is safe even if OCR completes long after the normal 10-second window.
        assertTrue(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 1_000,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 4,
                currentPasswordRevision = 7,
            ),
        )
        assertTrue(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 60_000,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 4,
                currentPasswordRevision = 7,
            ),
        )

        // Disabled protection, cleared/rolled-back signals, or any risk revision fail closed.
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = false,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 1_000,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 4,
                currentPasswordRevision = 7,
            ),
        )
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = null,
                currentForegroundSeenAt = Long.MIN_VALUE,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 5,
                currentPasswordRevision = 7,
            ),
        )
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 999,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 4,
                currentPasswordRevision = 7,
            ),
        )
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.passwordmanager",
                currentForegroundSeenAt = 1_000,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 5,
                currentPasswordRevision = 7,
            ),
        )
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 1_000,
                currentPasswordSeenAt = Long.MIN_VALUE,
                currentForegroundRevision = 5,
                currentPasswordRevision = 7,
            ),
        )
        assertFalse(
            isScreenOcrCompletionSafe(
                captured = captured,
                accessibilityProtectionEnabled = true,
                currentForegroundPackage = "com.example.gallery",
                currentForegroundSeenAt = 60_000,
                currentPasswordSeenAt = 59_000,
                currentForegroundRevision = 4,
                currentPasswordRevision = 8,
            ),
        )
    }

    @Test
    fun passwordSignalAndSensitivePackagesBlockCapture() {
        assertNull(
            screenCaptureContext(
                accessibilityProtectionEnabled = true,
                foregroundPackage = "com.example.notes",
                foregroundSeenAt = 9_000,
                passwordSeenAt = 9_500,
                now = 10_000,
                ownPackage = "com.mouchen.app.alpha",
            ),
        )
        listOf(
            "com.example.mobilebank",
            "com.google.android.apps.walletnfcrel",
            "com.google.android.apps.authenticator2",
            "com.azure.authenticator",
            "com.x8bit.bitwarden",
            "com.twofasapp",
            "org.fedorahosted.freeotp",
            "com.eg.android.AlipayGphone",
            "com.android.systemui",
            "com.mouchen.app.alpha",
        ).forEach { packageName ->
            assertNull(
                screenCaptureContext(
                    accessibilityProtectionEnabled = true,
                    foregroundPackage = packageName,
                    foregroundSeenAt = 9_000,
                    passwordSeenAt = Long.MIN_VALUE,
                    now = 10_000,
                    ownPackage = "com.mouchen.app.alpha",
                ),
            )
        }
    }

    @Test
    fun accessibilityServiceListRequiresExactEnabledComponent() {
        val expectedPackage = "com.mouchen.app.alpha.debug"
        val expectedClass = "com.mouchen.app.collectors.MouchenAccessibilityService"
        assertEquals(
            true,
            isAccessibilityServiceListed(
                "com.other/.Service:$expectedPackage/$expectedClass",
                expectedPackage,
                expectedClass,
            ),
        )
        assertEquals(
            false,
            isAccessibilityServiceListed(
                "com.other/.Service:com.mouchen.app/$expectedClass",
                expectedPackage,
                expectedClass,
            ),
        )
        assertEquals(false, isAccessibilityServiceListed(null, expectedPackage, expectedClass))
    }

    @Test
    fun unsafeFramesThrottleGuardChecksWithoutConsumingTheCaptureSlot() {
        val firstGuardCheck = 100_000L
        val sampleInterval = 15_000L
        val guardInterval = 500L

        assertTrue(
            isScreenCaptureGuardCheckDue(
                now = firstGuardCheck,
                lastCaptureAt = 0,
                lastGuardCheckAt = 0,
                sampleIntervalMs = sampleInterval,
                guardCheckIntervalMs = guardInterval,
            ),
        )
        assertFalse(
            isScreenCaptureGuardCheckDue(
                now = firstGuardCheck + guardInterval - 1,
                lastCaptureAt = 0,
                lastGuardCheckAt = firstGuardCheck,
                sampleIntervalMs = sampleInterval,
                guardCheckIntervalMs = guardInterval,
            ),
        )
        assertTrue(
            isScreenCaptureGuardCheckDue(
                now = firstGuardCheck + guardInterval,
                lastCaptureAt = 0,
                lastGuardCheckAt = firstGuardCheck,
                sampleIntervalMs = sampleInterval,
                guardCheckIntervalMs = guardInterval,
            ),
        )

        val successfulCapture = firstGuardCheck + guardInterval
        assertFalse(
            isScreenCaptureGuardCheckDue(
                now = successfulCapture + sampleInterval - 1,
                lastCaptureAt = successfulCapture,
                lastGuardCheckAt = successfulCapture,
                sampleIntervalMs = sampleInterval,
                guardCheckIntervalMs = guardInterval,
            ),
        )
        assertTrue(
            isScreenCaptureGuardCheckDue(
                now = successfulCapture + sampleInterval,
                lastCaptureAt = successfulCapture,
                lastGuardCheckAt = successfulCapture,
                sampleIntervalMs = sampleInterval,
                guardCheckIntervalMs = guardInterval,
            ),
        )
    }
}
