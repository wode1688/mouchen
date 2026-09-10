package com.mouchen.app

import com.mouchen.app.sync.AudioProcessingLocation
import com.mouchen.app.sync.BackendConnection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PrivacyDisclosureTest {
    private val base = BackendConnection(
        baseUrl = "https://backend.example",
        bearerToken = "token",
        enabled = true,
    )

    @Test
    fun ownerDisclosureTracksDisabledMinimizedAndFullContextModes() {
        assertTrue(ownerContextDisclosure(base.copy(enabled = false)).contains("不会上云"))
        assertTrue(ownerContextDisclosure(base.copy(minimizedContextOnly = true)).contains("脱敏的最小片段"))
        assertTrue(
            ownerContextDisclosure(
                base.copy(minimizedContextOnly = false, proactiveCloudEnabled = true),
            ).contains("完整上下文"),
        )
    }

    @Test
    fun audioDisclosureNamesTheActualProcessingDestination() {
        val configured = base.copy(sttBaseUrl = "https://stt.example")

        assertTrue(
            audioCaptureDisclosure(
                configured.copy(sttProcessingLocation = AudioProcessingLocation.PRIVATE_VPS),
            ).contains("私人服务器"),
        )
        assertTrue(
            audioCaptureDisclosure(
                configured.copy(sttProcessingLocation = AudioProcessingLocation.PUBLIC_CLOUD),
            ).contains("公共云语音服务"),
        )
        assertTrue(
            audioCaptureDisclosure(
                configured.copy(sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN),
            ).contains("可信局域网"),
        )
    }

    @Test
    fun missingAddressOrTokenNeverClaimsAudioUpload() {
        assertFalse(audioTranscriptionReady(base.copy(sttBaseUrl = null)))
        assertFalse(audioTranscriptionReady(base.copy(sttBaseUrl = "https://stt.example", bearerToken = null)))
        assertEquals("disabled", audioTranscriptionMode(base.copy(sttBaseUrl = null)))
        assertTrue(audioCaptureDisclosure(base.copy(sttBaseUrl = null)).contains("不会上传"))
    }

    @Test
    fun batteryCopyDoesNotPromiseKeepAlive() {
        assertTrue(batteryOptimizationDescription(true).contains("不是常驻保证"))
        assertTrue(batteryOptimizationDescription(false).contains("不会自动修改"))
    }
}
