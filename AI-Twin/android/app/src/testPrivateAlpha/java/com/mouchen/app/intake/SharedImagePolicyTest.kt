package com.mouchen.app.intake

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SharedImagePolicyTest {
    @Test
    fun imageMimeValidationIsFailClosed() {
        assertTrue(isSupportedImageMime("image/png"))
        assertTrue(isSupportedImageMime("IMAGE/JPEG; charset=binary"))
        assertFalse(isSupportedImageMime("text/plain"))
        assertFalse(isSupportedImageMime(null))
    }

    @Test
    fun decodeSampleBoundsBothEdgeAndPixelCount() {
        val sample = calculateImageSampleSize(width = 12_000, height = 8_000)

        assertEquals(8, sample)
        assertTrue(12_000L / sample <= MAX_SHARED_IMAGE_EDGE)
        assertTrue((12_000L / sample) * (8_000L / sample) <= MAX_SHARED_IMAGE_PIXELS)
    }

    @Test
    fun bestOcrCandidateUsesMeaningfulCharacterCount() {
        assertEquals("mlkit_chinese", selectBestOcrText("你好世界", "hi").second)
        assertEquals("mlkit_latin", selectBestOcrText("", "hello world").second)
    }
}
