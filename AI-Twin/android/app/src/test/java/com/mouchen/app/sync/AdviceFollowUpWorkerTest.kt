package com.mouchen.app.sync

import com.mouchen.app.data.AdviceEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class AdviceFollowUpWorkerTest {
    @Test
    fun schedulesAtPredictionDeadline() {
        val now = 1_700_000_000_000L
        val deadline = "2023-11-14T23:13:20Z"

        assertEquals(3_600_000L, adviceFollowUpDelayMillis("{\"deadline\":\"$deadline\"}", now))
    }

    @Test
    fun overduePredictionRunsImmediately() {
        assertEquals(
            0L,
            adviceFollowUpDelayMillis("{\"deadline\":\"2020-01-01T00:00:00Z\"}", 1_700_000_000_000L),
        )
    }

    @Test
    fun malformedPredictionDoesNotCreateUnboundedWork() {
        assertNull(adviceFollowUpDelayMillis("{}", 1_700_000_000_000L))
        assertNull(adviceFollowUpDelayMillis("not-json", 1_700_000_000_000L))
    }

    @Test
    fun pulledAdoptedAdviceRestoresMissingFollowUp() {
        assertEquals(
            AdviceFollowUpReconcileAction.Schedule,
            adviceFollowUpReconcileAction(advice(status = "adopted")),
        )
    }

    @Test
    fun durableLedgerPreventsRepeatedFollowUp() {
        assertEquals(
            AdviceFollowUpReconcileAction.None,
            adviceFollowUpReconcileAction(
                advice(status = "adopted", followUpNotifiedAt = 1_700_000_000_000L),
            ),
        )
    }

    @Test
    fun serverTerminalStatusesCancelPendingFollowUp() {
        assertEquals(
            AdviceFollowUpReconcileAction.Cancel,
            adviceFollowUpReconcileAction(advice(status = "verified")),
        )
        assertEquals(
            AdviceFollowUpReconcileAction.Cancel,
            adviceFollowUpReconcileAction(advice(status = "withdrawn")),
        )
    }

    private fun advice(status: String, followUpNotifiedAt: Long? = null) = AdviceEntity(
        id = "7b240867-736a-489d-8367-3151dd446c20",
        domain = "work",
        level = 2,
        goalQuote = "本周完成发布",
        evidenceJson = "[]",
        action = "锁定发布范围",
        firstStep = "打开任务清单",
        predictionJson = "{\"deadline\":\"2026-08-04T00:00:00Z\"}",
        dedupeKey = "release",
        status = status,
        followUpNotifiedAt = followUpNotifiedAt,
        createdAt = 1L,
    )
}
