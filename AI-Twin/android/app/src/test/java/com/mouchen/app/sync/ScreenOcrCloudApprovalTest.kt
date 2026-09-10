package com.mouchen.app.sync

import okhttp3.Request
import org.junit.Assert.assertEquals
import org.junit.Test

class ScreenOcrCloudApprovalTest {
    @Test
    fun diagnosticUploadCannotApproveProactiveOrRawCloudAnalysis() {
        val connection = BackendConnection(
            baseUrl = "https://example.com",
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )
        val request = Request.Builder()
            .url("https://example.com/v1/events")
            .applyCloudApprovalHeaders(
                connection,
                proactiveCloudAnalysisAllowed = eventAllowsProactiveCloudAnalysis("screen.ocr_diagnostic"),
            )
            .build()

        assertEquals("false", request.header("X-Proactive-Cloud-Approved"))
        assertEquals("false", request.header("X-Raw-Cloud-Approved"))
    }

    @Test
    fun ordinaryEventsKeepOwnerApprovedCloudAnalysis() {
        val connection = BackendConnection(
            baseUrl = "https://example.com",
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )
        val request = Request.Builder()
            .url("https://example.com/v1/events")
            .applyCloudApprovalHeaders(
                connection,
                proactiveCloudAnalysisAllowed = eventAllowsProactiveCloudAnalysis("ui.visible_text"),
            )
            .build()

        assertEquals("true", request.header("X-Proactive-Cloud-Approved"))
        assertEquals("true", request.header("X-Raw-Cloud-Approved"))
    }
}
