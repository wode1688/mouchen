package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioSegmentPolicyTest {
    @Test
    fun segmentRotatesAtDurationOrBeforeCrossingSizeLimit() {
        assertTrue(
            shouldRotateAudioSegment(
                segmentStartedAt = 1_000,
                now = 2_000,
                currentPlainBytes = 4,
                nextPlainBytes = 1,
                maxDurationMs = 1_000,
                maxPlainBytes = 100,
            ),
        )
        assertTrue(
            shouldRotateAudioSegment(
                segmentStartedAt = 1_000,
                now = 1_500,
                currentPlainBytes = 8,
                nextPlainBytes = 3,
                maxDurationMs = 1_000,
                maxPlainBytes = 10,
            ),
        )
    }

    @Test
    fun emptySegmentIsNotRotatedAndNamesAreStable() {
        assertFalse(
            shouldRotateAudioSegment(
                segmentStartedAt = 1_000,
                now = 5_000,
                currentPlainBytes = 0,
                nextPlainBytes = 3,
                maxDurationMs = 1_000,
                maxPlainBytes = 2,
            ),
        )
        assertEquals("audio-123-segment-0007.pcm.mch", audioSegmentName(123, 7))
        assertEquals("audio.segment_completed", audioSegmentTerminalEventType(closeSucceeded = true))
        assertEquals("audio.segment_failed", audioSegmentTerminalEventType(closeSucceeded = false))
    }
}
