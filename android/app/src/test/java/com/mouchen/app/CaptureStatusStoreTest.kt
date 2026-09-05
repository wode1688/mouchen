package com.mouchen.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureStatusStoreTest {
    @Test
    fun restoredStateRequiresTheCorrespondingServiceToStillBeAlive() {
        val persisted = CaptureStatusSnapshot(audioRunning = true, screenRunning = true)

        assertEquals(
            CaptureStatusSnapshot(
                audioRunning = true,
                screenRunning = false,
                screenAuthorizationRequired = true,
                screenStatusDetail = "capture_session_ended",
            ),
            reconcileCaptureStatus(persisted, setOf(AUDIO_CAPTURE_SERVICE_CLASS)),
        )
    }

    @Test
    fun aServiceThatHasNotPublishedSuccessfulStartupIsNotShownAsRunning() {
        assertEquals(
            CaptureStatusSnapshot(),
            reconcileCaptureStatus(
                CaptureStatusSnapshot(),
                setOf(AUDIO_CAPTURE_SERVICE_CLASS, SCREEN_CAPTURE_SERVICE_CLASS),
            ),
        )
    }

    @Test
    fun staleStateAfterProcessDeathIsCleared() {
        assertEquals(
            CaptureStatusSnapshot(
                screenAuthorizationRequired = true,
                screenStatusDetail = "capture_session_ended",
            ),
            reconcileCaptureStatus(
                CaptureStatusSnapshot(audioRunning = true, screenRunning = true),
                emptySet(),
            ),
        )
    }

    @Test
    fun anExpiredProjectionIsVisibleAndRequiresAUserGrant() {
        val result = reconcileCaptureStatus(
            CaptureStatusSnapshot(screenRunning = true),
            emptySet(),
        )

        assertFalse(result.screenRunning)
        assertTrue(result.screenAuthorizationRequired)
        assertEquals("capture_session_ended", result.screenStatusDetail)
    }

    @Test
    fun bothPublishedCapturesRemainVisibleWhileBothServicesAreAlive() {
        val activeServices = setOf(AUDIO_CAPTURE_SERVICE_CLASS, SCREEN_CAPTURE_SERVICE_CLASS)

        assertEquals(
            CaptureStatusSnapshot(audioRunning = true, screenRunning = true),
            reconcileCaptureStatus(
                CaptureStatusSnapshot(audioRunning = true, screenRunning = true),
                activeServices,
            ),
        )
    }

    @Test
    fun android14AndNewerRequestTheWholeDefaultDisplay() {
        assertFalse(usesDefaultDisplayProjectionConfig(33))
        assertTrue(usesDefaultDisplayProjectionConfig(34))
        assertTrue(usesDefaultDisplayProjectionConfig(36))
    }
}
