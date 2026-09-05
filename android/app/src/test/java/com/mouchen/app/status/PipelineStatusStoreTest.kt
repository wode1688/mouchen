package com.mouchen.app.status

import org.junit.Assert.assertEquals
import org.junit.Test

class PipelineStatusStoreTest {
    @Test
    fun statusSnapshotRoundTripsWithoutLosingDiagnostics() {
        val snapshot = PipelineStatusSnapshot(
            collection = PipelineStageStatus(
                phase = PipelinePhase.PARTIAL_FAILURE,
                trigger = "manual_scan",
                requestedAt = 10,
                startedAt = 11,
                finishedAt = 12,
                attempted = 6,
                succeeded = 5,
                failed = 1,
                detail = "imap",
            ),
            sync = PipelineStageStatus(
                phase = PipelinePhase.QUEUED,
                trigger = "collection:manual_scan",
                requestedAt = 13,
            ),
        )

        assertEquals(snapshot, PipelineStatusCodec.decode(PipelineStatusCodec.encode(snapshot)))
    }

    @Test
    fun malformedStatusFallsBackToIdle() {
        assertEquals(PipelineStatusSnapshot(), PipelineStatusCodec.decode("not-json"))
    }
}
