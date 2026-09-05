package com.mouchen.app.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

@Dao
interface MouchenDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertEvent(event: LocalEventEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertEvents(events: List<LocalEventEntity>)

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertEventIfAbsent(event: LocalEventEntity): Long

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertUrgentEventIfAbsent(event: UrgentEventQueueEntity): Long

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertPendingAnalysisIfAbsent(pending: PendingAnalysisEntity): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertGoal(goal: GoalEntity)

    /** Keeps an unsent local goal authoritative while allowing server history to be restored. */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertGoalIfAbsent(goal: GoalEntity): Long

    @Query("SELECT * FROM goals WHERE id = :id LIMIT 1")
    suspend fun goal(id: String): GoalEntity?

    @Update(onConflict = OnConflictStrategy.IGNORE)
    suspend fun updateGoalIfNoConflict(goal: GoalEntity): Int

    /** Server state updates only goals whose local upload has already committed. */
    @Transaction
    suspend fun mergeServerGoal(goal: GoalEntity): ServerGoalMergeResult {
        val existing = goal(goal.id)
        if (existing != null && !existing.synced) return ServerGoalMergeResult.LOCAL_PENDING
        if (existing == null) {
            return if (insertGoalIfAbsent(goal) != -1L) {
                ServerGoalMergeResult.INSERTED
            } else {
                ServerGoalMergeResult.CONFLICT
            }
        }
        return if (updateGoalIfNoConflict(goal) == 1) {
            ServerGoalMergeResult.UPDATED
        } else {
            ServerGoalMergeResult.CONFLICT
        }
    }

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAdvice(advice: AdviceEntity)

    @Query("SELECT * FROM advice WHERE id = :id LIMIT 1")
    suspend fun advice(id: String): AdviceEntity?

    @Query("UPDATE advice SET status = :status WHERE id = :id")
    suspend fun updateAdviceStatus(id: String, status: String): Int

    @Query("UPDATE advice SET notifiedAt = :notifiedAt WHERE id = :id AND notifiedAt IS NULL")
    suspend fun markAdviceNotified(id: String, notifiedAt: Long): Int

    @Query(
        "UPDATE advice SET followUpNotifiedAt = :notifiedAt " +
            "WHERE id = :id AND followUpNotifiedAt IS NULL",
    )
    suspend fun markAdviceFollowUpNotified(id: String, notifiedAt: Long): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertActionDraft(draft: ActionDraftEntity)

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insertAudioTranscriptionJobIfAbsent(job: AudioTranscriptionJobEntity): Long

    @Query("SELECT * FROM audio_transcription_jobs WHERE blobName = :blobName LIMIT 1")
    suspend fun audioTranscriptionJob(blobName: String): AudioTranscriptionJobEntity?

    @Query("SELECT blobName FROM audio_transcription_jobs WHERE segmentEndedAt >= :cutoff")
    suspend fun recentAudioTranscriptionJobNames(cutoff: Long): List<String>

    @Query(
        """
        SELECT * FROM audio_transcription_jobs
        WHERE status = 'pending' AND nextAttemptAt <= :now
        ORDER BY nextAttemptAt, segmentEndedAt, blobName
        LIMIT :limit
        """,
    )
    suspend fun dueAudioTranscriptionJobs(now: Long, limit: Int): List<AudioTranscriptionJobEntity>

    @Query(
        """
        UPDATE audio_transcription_jobs
        SET status = 'enqueued', updatedAt = :updatedAt
        WHERE blobName = :blobName AND status = 'pending'
        """,
    )
    suspend fun markAudioTranscriptionEnqueued(blobName: String, updatedAt: Long): Int

    @Query(
        """
        UPDATE audio_transcription_jobs
        SET status = 'pending', nextAttemptAt = :nextAttemptAt,
            lastFailure = :reason, updatedAt = :updatedAt
        WHERE blobName = :blobName AND status = 'enqueued'
        """,
    )
    suspend fun releaseAudioTranscriptionEnqueue(
        blobName: String,
        nextAttemptAt: Long,
        reason: String,
        updatedAt: Long,
    ): Int

    @Query(
        """
        UPDATE audio_transcription_jobs
        SET status = 'pending', nextAttemptAt = 0,
            lastFailure = 'enqueue_lease_expired', updatedAt = :updatedAt
        WHERE status = 'enqueued' AND updatedAt <= :staleBefore
        """,
    )
    suspend fun reclaimStaleAudioTranscriptionEnqueues(staleBefore: Long, updatedAt: Long): Int

    @Query(
        """
        UPDATE audio_transcription_jobs
        SET status = :newStatus, recoveryCycles = :newRecoveryCycles,
            nextAttemptAt = :nextAttemptAt, lastFailure = :reason, updatedAt = :updatedAt
        WHERE blobName = :blobName AND status != 'completed'
            AND recoveryCycles = :expectedRecoveryCycles
        """,
    )
    suspend fun transitionAudioTranscriptionFailure(
        blobName: String,
        expectedRecoveryCycles: Int,
        newStatus: String,
        newRecoveryCycles: Int,
        nextAttemptAt: Long,
        reason: String,
        updatedAt: Long,
    ): Int

    @Query(
        """
        UPDATE audio_transcription_jobs
        SET status = 'completed', nextAttemptAt = 0, lastFailure = '', updatedAt = :updatedAt
        WHERE blobName = :blobName AND status != 'completed'
        """,
    )
    suspend fun markAudioTranscriptionCompleted(blobName: String, updatedAt: Long): Int

    @Query("SELECT * FROM action_drafts WHERE id = :id LIMIT 1")
    suspend fun actionDraft(id: String): ActionDraftEntity?

    @Query("UPDATE action_drafts SET status = 'confirmed', confirmedAt = :confirmedAt WHERE id = :id AND status = 'draft'")
    suspend fun confirmActionDraft(id: String, confirmedAt: Long): Int

    @Query("UPDATE action_drafts SET status = :toStatus WHERE id = :id AND status = :fromStatus")
    suspend fun transitionActionDraft(id: String, fromStatus: String, toStatus: String): Int

    @Query("UPDATE action_drafts SET status = :status, payloadJson = :payloadJson, executedAt = :executedAt WHERE id = :id")
    suspend fun finishActionDraft(id: String, status: String, payloadJson: String, executedAt: Long?): Int

    @Query(
        "SELECT * FROM local_events WHERE synced = 0 AND nextDeliveryAttemptAt <= :now " +
            "ORDER BY nextDeliveryAttemptAt, occurredAt LIMIT :limit",
    )
    suspend fun unsyncedEvents(now: Long, limit: Int = 200): List<LocalEventEntity>

    @Query("SELECT * FROM local_events WHERE id = :id LIMIT 1")
    suspend fun event(id: String): LocalEventEntity?

    @Query("DELETE FROM local_events WHERE id = :id")
    suspend fun deleteEvent(id: String): Int

    @Query("SELECT COUNT(*) FROM local_events WHERE synced = 0")
    suspend fun unsyncedEventCount(): Int

    @Query("SELECT COUNT(*) FROM local_events WHERE synced = 0")
    fun observeUnsyncedEventCount(): Flow<Int>

    @Query("UPDATE local_events SET synced = 1 WHERE id IN (:ids)")
    suspend fun markSynced(ids: List<String>)

    @Query("UPDATE local_events SET synced = 1 WHERE id = :id AND synced = 0")
    suspend fun markEventSynced(id: String): Int

    @Query("SELECT COUNT(*) FROM pending_analysis")
    suspend fun pendingAnalysisCount(): Int

    @Query("SELECT * FROM pending_analysis WHERE eventId = :eventId LIMIT 1")
    suspend fun pendingAnalysis(eventId: String): PendingAnalysisEntity?

    @Query(
        "SELECT * FROM pending_analysis WHERE nextAttemptAt <= :now " +
            "ORDER BY nextAttemptAt, enqueuedAt, eventId LIMIT :limit",
    )
    suspend fun duePendingAnalysis(
        now: Long,
        limit: Int = 500,
    ): List<PendingAnalysisEntity>

    @Query("SELECT MIN(nextAttemptAt) FROM pending_analysis")
    suspend fun earliestPendingAnalysisAttemptAt(): Long?

    @Query("DELETE FROM pending_analysis WHERE eventId = :eventId")
    suspend fun deletePendingAnalysis(eventId: String): Int

    @Query("DELETE FROM pending_analysis")
    suspend fun deleteAllPendingAnalysis(): Int

    @Query(
        "DELETE FROM pending_analysis WHERE eventId = :eventId AND attempts = :expectedAttempts",
    )
    suspend fun deletePendingAnalysisAtAttempt(eventId: String, expectedAttempts: Int): Int

    @Query(
        """
        UPDATE pending_analysis
        SET attempts = :newAttempts, nextAttemptAt = :nextAttemptAt, updatedAt = :updatedAt
        WHERE eventId = :eventId AND attempts = :expectedAttempts
        """,
    )
    suspend fun advancePendingAnalysisAtAttempt(
        eventId: String,
        expectedAttempts: Int,
        newAttempts: Int,
        nextAttemptAt: Long,
        updatedAt: Long,
    ): Int

    /** Atomically removes any stale same-event pending row and commits a direct delivery. */
    @Transaction
    suspend fun markEventSyncedWithoutPendingAnalysis(eventId: String) {
        deletePendingAnalysis(eventId)
        check(markEventSynced(eventId) == 1) { "Event disappeared before direct sync commit" }
    }

    /** Atomically makes the accepted queued response replayable before the source row is closed. */
    @Transaction
    suspend fun markEventSyncedWithPendingAnalysis(pending: PendingAnalysisEntity): Boolean {
        val queueWasEmpty = pendingAnalysisCount() == 0
        insertPendingAnalysisIfAbsent(pending)
        check(markEventSynced(pending.eventId) == 1) { "Event disappeared before queued sync commit" }
        return queueWasEmpty && pendingAnalysisCount() > 0
    }

    /** Advances exactly the rows observed by one pull and returns the next durable wake time. */
    @Transaction
    suspend fun finishPendingAnalysisPoll(
        observed: List<PendingAnalysisEntity>,
        attemptedAt: Long,
    ): Long? {
        observed.forEach { row ->
            val current = pendingAnalysis(row.eventId) ?: return@forEach
            if (current.attempts != row.attempts) return@forEach
            val completedAttempts = current.attempts + 1
            if (completedAttempts >= MAX_PENDING_ANALYSIS_ATTEMPTS) {
                deletePendingAnalysisAtAttempt(current.eventId, current.attempts)
            } else {
                advancePendingAnalysisAtAttempt(
                    eventId = current.eventId,
                    expectedAttempts = current.attempts,
                    newAttempts = completedAttempts,
                    nextAttemptAt = pendingAnalysisNextAttemptAt(attemptedAt, completedAttempts),
                    updatedAt = attemptedAt,
                )
            }
        }
        return earliestPendingAnalysisAttemptAt()
    }

    @Query(
        "UPDATE local_events SET deliveryAttempts = deliveryAttempts + 1, " +
            "nextDeliveryAttemptAt = :nextAttemptAt WHERE id = :id AND synced = 0",
    )
    suspend fun markEventDeliveryFailed(id: String, nextAttemptAt: Long): Int

    @Query("UPDATE local_events SET nextDeliveryAttemptAt = 0 WHERE id = :id AND synced = 0")
    suspend fun makeEventDeliveryDueNow(id: String): Int

    @Query(
        "SELECT * FROM goals WHERE synced = 0 AND nextDeliveryAttemptAt <= :now " +
            "ORDER BY nextDeliveryAttemptAt, createdAt LIMIT :limit",
    )
    suspend fun unsyncedGoals(now: Long, limit: Int = 100): List<GoalEntity>

    @Query("SELECT COUNT(*) FROM goals WHERE synced = 0")
    suspend fun unsyncedGoalCount(): Int

    @Query("SELECT COUNT(*) FROM goals WHERE synced = 0")
    fun observeUnsyncedGoalCount(): Flow<Int>

    @Query("UPDATE goals SET synced = 1 WHERE id IN (:ids)")
    suspend fun markGoalsSynced(ids: List<String>)

    @Query("UPDATE goals SET synced = 1 WHERE id = :id AND synced = 0")
    suspend fun markGoalSynced(id: String): Int

    @Query(
        "UPDATE goals SET deliveryAttempts = deliveryAttempts + 1, " +
            "nextDeliveryAttemptAt = :nextAttemptAt WHERE id = :id AND synced = 0",
    )
    suspend fun markGoalDeliveryFailed(id: String, nextAttemptAt: Long): Int

    @Query(
        """
        SELECT * FROM urgent_event_queue
        WHERE nextAttemptAt <= :now
        ORDER BY CASE category
            WHEN 'urgent_health' THEN 0
            WHEN 'account_security' THEN 1
            WHEN 'deadline' THEN 2
            WHEN 'owner_request' THEN 3
            ELSE 4 END,
            nextAttemptAt, enqueuedAt
        LIMIT 1
        """,
    )
    suspend fun nextDueUrgentEvent(now: Long): UrgentEventQueueEntity?

    @Query("SELECT COUNT(*) FROM urgent_event_queue")
    suspend fun urgentEventCount(): Int

    @Query("SELECT MIN(nextAttemptAt) FROM urgent_event_queue")
    suspend fun earliestUrgentAttemptAt(): Long?

    @Query("DELETE FROM urgent_event_queue WHERE eventId = :eventId")
    suspend fun deleteUrgentEvent(eventId: String): Int

    @Query(
        """
        UPDATE urgent_event_queue
        SET attempts = attempts + 1, nextAttemptAt = :nextAttemptAt,
            updatedAt = :updatedAt, lastFailure = :reason
        WHERE eventId = :eventId AND attempts = :expectedAttempts
        """,
    )
    suspend fun markUrgentEventFailed(
        eventId: String,
        expectedAttempts: Int,
        nextAttemptAt: Long,
        updatedAt: Long,
        reason: String,
    ): Int

    @Query("SELECT * FROM advice ORDER BY createdAt DESC")
    fun observeAdvice(): Flow<List<AdviceEntity>>

    @Query("SELECT * FROM goals ORDER BY createdAt DESC")
    fun observeGoals(): Flow<List<GoalEntity>>

    @Query("SELECT * FROM local_events ORDER BY occurredAt DESC LIMIT 300")
    fun observeRecentEvents(): Flow<List<LocalEventEntity>>

    @Query("DELETE FROM local_events WHERE occurredAt < :cutoff")
    suspend fun deleteEventsBefore(cutoff: Long): Int

    @Query("DELETE FROM local_events WHERE type IN (:types) AND occurredAt < :cutoff")
    suspend fun deleteEventsByTypesBefore(types: List<String>, cutoff: Long): Int

    @Query("DELETE FROM local_events WHERE type = :type")
    suspend fun deleteEventsByType(type: String): Int

    @Query("DELETE FROM audio_transcription_jobs WHERE segmentEndedAt < :cutoff")
    suspend fun deleteAudioTranscriptionJobsBefore(cutoff: Long): Int
}

enum class ServerGoalMergeResult {
    INSERTED,
    UPDATED,
    LOCAL_PENDING,
    CONFLICT,
}
