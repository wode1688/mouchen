package com.mouchen.app.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey
import java.util.UUID

@Entity(
    tableName = "local_events",
    indices = [
        Index("occurredAt"),
        Index("type"),
        Index("synced"),
        Index(value = ["synced", "nextDeliveryAttemptAt", "occurredAt"]),
    ],
)
data class LocalEventEntity(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    val source: String,
    val type: String,
    val occurredAt: Long = System.currentTimeMillis(),
    val sensitivity: String = "sensitive",
    val payloadJson: String,
    val synced: Boolean = false,
    val deliveryAttempts: Int = 0,
    val nextDeliveryAttemptAt: Long = 0L,
)

@Entity(
    tableName = "goals",
    indices = [
        Index(value = ["domain", "version"], unique = true),
        Index(value = ["synced", "nextDeliveryAttemptAt", "createdAt"]),
    ],
)
data class GoalEntity(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    val domain: String,
    val title: String,
    val quote: String,
    val version: Int,
    val isRedline: Boolean = false,
    val targetJson: String = "{}",
    val createdAt: Long = System.currentTimeMillis(),
    val synced: Boolean = false,
    val deliveryAttempts: Int = 0,
    val nextDeliveryAttemptAt: Long = 0L,
)

/** Durable priority queue; WorkManager only needs one running job regardless of event volume. */
@Entity(
    tableName = "urgent_event_queue",
    indices = [Index("nextAttemptAt"), Index("enqueuedAt")],
)
data class UrgentEventQueueEntity(
    @PrimaryKey val eventId: String,
    val category: String,
    val attempts: Int = 0,
    val nextAttemptAt: Long = 0L,
    val enqueuedAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    val lastFailure: String = "",
)

/** Durable asynchronous-analysis ledger; WorkManager is only a wake-up mechanism. */
@Entity(
    tableName = "pending_analysis",
    indices = [Index("nextAttemptAt"), Index("enqueuedAt")],
)
data class PendingAnalysisEntity(
    @PrimaryKey val eventId: String,
    val attempts: Int = 0,
    val enqueuedAt: Long = System.currentTimeMillis(),
    val nextAttemptAt: Long = pendingAnalysisInitialAttemptAt(enqueuedAt),
    val updatedAt: Long = enqueuedAt,
)

internal fun pendingAnalysisInitialAttemptAt(enqueuedAt: Long): Long =
    safeAddMillis(enqueuedAt, PENDING_ANALYSIS_INITIAL_DELAY_MS)

internal fun pendingAnalysisNextAttemptAt(now: Long, completedAttempts: Int): Long {
    val exponent = (completedAttempts - 1).coerceIn(0, 2)
    return safeAddMillis(now, PENDING_ANALYSIS_RETRY_DELAY_MS shl exponent)
}

private fun safeAddMillis(now: Long, delay: Long): Long =
    if (now > Long.MAX_VALUE - delay) Long.MAX_VALUE else now + delay

internal const val MAX_PENDING_ANALYSIS_ATTEMPTS = 4
private const val PENDING_ANALYSIS_INITIAL_DELAY_MS = 5_000L
private const val PENDING_ANALYSIS_RETRY_DELAY_MS = 10_000L

@Entity(tableName = "advice", indices = [Index("domain"), Index("status"), Index("dedupeKey")])
data class AdviceEntity(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    val domain: String,
    val level: Int,
    val goalQuote: String,
    val evidenceJson: String,
    val action: String,
    val firstStep: String,
    val alternative: String = "",
    val predictionJson: String,
    val dedupeKey: String,
    val delivery: String = "immediate",
    val status: String = "active",
    /** Legacy first-device receipt; central delivery counts now live on the backend. */
    val notifiedAt: Long? = null,
    /** Null until the adopted-advice checkpoint reminder was successfully posted. */
    val followUpNotifiedAt: Long? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

@Entity(tableName = "action_drafts", indices = [Index("status")])
data class ActionDraftEntity(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    val actionType: String,
    val payloadJson: String,
    val status: String = "draft",
    val confirmedAt: Long? = null,
    val executedAt: Long? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

/**
 * Durable local ledger for encrypted microphone segments waiting for transcription.
 *
 * The encrypted blob remains the source of truth. This row only records scheduling metadata so a
 * segment rejected by WorkManager's bounded queue can be picked up after capacity becomes free.
 */
@Entity(
    tableName = "audio_transcription_jobs",
    indices = [Index(value = ["status", "nextAttemptAt"]), Index("updatedAt")],
)
data class AudioTranscriptionJobEntity(
    @PrimaryKey val blobName: String,
    val segmentStartedAt: Long,
    val segmentEndedAt: Long,
    /** Non-secret SHA-256 binding to the STT destination selected when this segment closed. */
    val destinationFingerprint: String = "",
    val status: String = "pending",
    val recoveryCycles: Int = 0,
    val nextAttemptAt: Long = 0L,
    val lastFailure: String = "",
    val updatedAt: Long = System.currentTimeMillis(),
)
