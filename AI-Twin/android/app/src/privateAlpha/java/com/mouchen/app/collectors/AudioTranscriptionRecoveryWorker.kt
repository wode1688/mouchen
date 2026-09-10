package com.mouchen.app.collectors

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.mouchen.app.data.AudioTranscriptionJobEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.security.BlobRetentionEntry
import com.mouchen.app.security.EncryptedBlobStore
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Refills the bounded transcription queue from a durable local ledger.
 *
 * A periodic pass is the crash/reboot safety net. A short coalesced pass after each completed work
 * item fills newly freed capacity without creating an unbounded WorkManager chain.
 */
class AudioTranscriptionRecoveryWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = when (dispatchDueAudioTranscriptions(applicationContext)) {
        AudioDispatchResult.STORAGE_UNAVAILABLE,
        AudioDispatchResult.WORK_MANAGER_UNAVAILABLE,
        -> Result.retry()
        AudioDispatchResult.IDLE,
        AudioDispatchResult.DISPATCHED,
        AudioDispatchResult.CAPACITY_FULL,
        -> Result.success()
    }

    companion object {
        private const val PERIODIC_WORK = "mouchen-audio-transcription-recovery-periodic"
        private const val IMMEDIATE_WORK = "mouchen-audio-transcription-recovery-immediate"

        fun ensurePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<AudioTranscriptionRecoveryWorker>(
                AUDIO_RECOVERY_PERIOD_MINUTES,
                TimeUnit.MINUTES,
            ).setConstraints(networkConstraints()).build()
            WorkManager.getInstance(context.applicationContext).enqueueUniquePeriodicWork(
                PERIODIC_WORK,
                ExistingPeriodicWorkPolicy.KEEP,
                request,
            )
        }

        /** Coalesces a burst of completions into one delayed refill after workers become finished. */
        fun enqueueImmediate(context: Context) {
            val request = OneTimeWorkRequestBuilder<AudioTranscriptionRecoveryWorker>()
                .setInitialDelay(AUDIO_RECOVERY_REFILL_DELAY_SECONDS, TimeUnit.SECONDS)
                .setConstraints(networkConstraints())
                .build()
            WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
                IMMEDIATE_WORK,
                ExistingWorkPolicy.REPLACE,
                request,
            )
        }

        private fun networkConstraints(): Constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
    }
}

internal enum class AudioDispatchResult {
    IDLE,
    DISPATCHED,
    CAPACITY_FULL,
    STORAGE_UNAVAILABLE,
    WORK_MANAGER_UNAVAILABLE,
}

internal suspend fun registerAudioTranscriptionSegment(
    context: Context,
    blobName: String,
    startedAt: Long,
    endedAt: Long,
    destinationFingerprint: String,
): Boolean = withContext(Dispatchers.IO) {
    if (!validAudioTranscriptionMetadata(blobName, startedAt, endedAt) ||
        !validAudioDestinationFingerprint(destinationFingerprint)
    ) return@withContext false
    runCatching { AudioTranscriptionRecoveryWorker.ensurePeriodic(context) }
    val dao = runCatching { MouchenDatabase.get(context.applicationContext).dao() }.getOrNull()
        ?: return@withContext false
    val now = System.currentTimeMillis()
    val candidate = AudioTranscriptionJobEntity(
        blobName = blobName,
        segmentStartedAt = startedAt,
        segmentEndedAt = endedAt,
        destinationFingerprint = destinationFingerprint,
        updatedAt = now,
    )
    runCatching { dao.insertAudioTranscriptionJobIfAbsent(candidate) }.getOrElse { return@withContext false }
    val stored = runCatching { dao.audioTranscriptionJob(blobName) }.getOrNull() ?: return@withContext false
    if (stored.status == AUDIO_JOB_COMPLETED || stored.status == AUDIO_JOB_ABANDONED) {
        return@withContext false
    }
    audioDispatchMutex.withLock {
        dispatchAudioJobs(context.applicationContext, listOf(stored), now) == AudioDispatchResult.DISPATCHED
    }
}

private suspend fun dispatchDueAudioTranscriptions(context: Context): AudioDispatchResult =
    withContext(Dispatchers.IO) {
        audioDispatchMutex.withLock {
            val dao = runCatching { MouchenDatabase.get(context).dao() }.getOrNull()
                ?: return@withLock AudioDispatchResult.STORAGE_UNAVAILABLE
            val now = System.currentTimeMillis()
            runCatching { recoverOrphanAudioTranscriptionJobs(context, dao, now) }
                .getOrNull() ?: return@withLock AudioDispatchResult.STORAGE_UNAVAILABLE
            runCatching {
                dao.reclaimStaleAudioTranscriptionEnqueues(now - AUDIO_ENQUEUE_LEASE_MS, now)
            }.getOrNull() ?: return@withLock AudioDispatchResult.STORAGE_UNAVAILABLE
            val due = runCatching {
                dao.dueAudioTranscriptionJobs(now, AUDIO_RECOVERY_SCAN_LIMIT)
            }.getOrNull() ?: return@withLock AudioDispatchResult.STORAGE_UNAVAILABLE
            val candidates = selectAudioRecoveryJobs(due, now, AUDIO_RECOVERY_SCAN_LIMIT)
            if (candidates.isEmpty()) return@withLock AudioDispatchResult.IDLE
            dispatchAudioJobs(context, candidates, now)
        }
    }

/**
 * Recovers encrypted segments closed by the OS after a process death before the durable job row
 * was inserted. Only stale, bounded, private-audio filenames are considered; active writers and
 * other encrypted captures are excluded.
 */
private suspend fun recoverOrphanAudioTranscriptionJobs(
    context: Context,
    dao: MouchenDao,
    now: Long,
): Int {
    val knownBlobNames = dao.recentAudioTranscriptionJobNames(
        (now - AUDIO_ORPHAN_LOOKBACK_MS).coerceAtLeast(0L),
    ).toHashSet()
    val candidates = selectOrphanAudioSegments(
        EncryptedBlobStore(context).inventory(),
        now,
        AUDIO_ORPHAN_SCAN_LIMIT,
        knownBlobNames,
    )
    var inserted = 0
    for (candidate in candidates) {
        if (dao.audioTranscriptionJob(candidate.blobName) != null) continue
        val row = dao.insertAudioTranscriptionJobIfAbsent(
            AudioTranscriptionJobEntity(
                blobName = candidate.blobName,
                segmentStartedAt = candidate.startedAt,
                segmentEndedAt = candidate.endedAt,
                lastFailure = "recovered_orphan_blob",
                updatedAt = now,
            ),
        )
        if (row != -1L) inserted += 1
    }
    return inserted
}

internal data class OrphanAudioSegment(
    val blobName: String,
    val startedAt: Long,
    val endedAt: Long,
)

internal fun selectOrphanAudioSegments(
    entries: List<BlobRetentionEntry>,
    now: Long,
    maxEntries: Int,
    knownBlobNames: Set<String> = emptySet(),
): List<OrphanAudioSegment> {
    if (now <= 0L || maxEntries <= 0) return emptyList()
    val oldestAllowed = (now - AUDIO_ORPHAN_LOOKBACK_MS).coerceAtLeast(0L)
    val newestAllowed = (now - AUDIO_ORPHAN_STALE_MS).coerceAtLeast(0L)
    return entries.asSequence()
        .filter { it.sizeBytes >= AUDIO_ORPHAN_MIN_ENCRYPTED_BYTES }
        .filter { it.modifiedAt in oldestAllowed..newestAllowed }
        .filterNot { it.name in knownBlobNames }
        .mapNotNull { entry -> orphanAudioSegment(entry) }
        .sortedByDescending(OrphanAudioSegment::endedAt)
        .take(maxEntries)
        .toList()
}

private fun orphanAudioSegment(entry: BlobRetentionEntry): OrphanAudioSegment? {
    val match = AUDIO_SEGMENT_FILE.matchEntire(entry.name) ?: return null
    val sessionStartedAt = match.groupValues[1].toLongOrNull()?.takeIf { it > 0L } ?: return null
    val segmentIndex = match.groupValues[2].toLongOrNull()?.takeIf { it >= 0L } ?: return null
    val estimatedStart = runCatching {
        Math.addExact(sessionStartedAt, Math.multiplyExact(segmentIndex, AUDIO_SEGMENT_ESTIMATED_MS))
    }.getOrNull() ?: return null
    if (entry.modifiedAt < estimatedStart) return null
    return OrphanAudioSegment(entry.name, estimatedStart, entry.modifiedAt)
}

private suspend fun dispatchAudioJobs(
    context: Context,
    candidates: List<AudioTranscriptionJobEntity>,
    now: Long,
): AudioDispatchResult {
    val dao = MouchenDatabase.get(context).dao()
    var dispatched = 0
    candidates.forEach { job ->
        // Claim before asking WorkManager to enqueue. This prevents a very fast worker failure from
        // being overwritten by a late "enqueued" update. A bounded stale-lease scan repairs the
        // opposite crash window (process death between this claim and WorkManager acceptance).
        if (dao.markAudioTranscriptionEnqueued(job.blobName, now) == 0) return@forEach
        when (val queued = enqueueAudioTranscriptionRequest(
            context,
            job.blobName,
            job.segmentStartedAt,
            job.segmentEndedAt,
            job.destinationFingerprint,
        )) {
            is AudioQueueEnqueueResult.Accepted -> {
                dispatched += 1
            }
            is AudioQueueEnqueueResult.CapacityFull -> {
                dao.releaseAudioTranscriptionEnqueue(job.blobName, now, "queue_capacity_full", now)
                return if (dispatched > 0) AudioDispatchResult.DISPATCHED else AudioDispatchResult.CAPACITY_FULL
            }
            is AudioQueueEnqueueResult.ConfigurationBlocked -> {
                dao.releaseAudioTranscriptionEnqueue(
                    blobName = job.blobName,
                    nextAttemptAt = now + AUDIO_CONFIGURATION_RECHECK_MS,
                    reason = queued.reason.take(AUDIO_FAILURE_REASON_LIMIT),
                    updatedAt = now,
                )
            }
            is AudioQueueEnqueueResult.ManagerUnavailable -> {
                dao.releaseAudioTranscriptionEnqueue(job.blobName, now, queued.reason, now)
                return if (dispatched > 0) AudioDispatchResult.DISPATCHED
                else AudioDispatchResult.WORK_MANAGER_UNAVAILABLE
            }
            is AudioQueueEnqueueResult.Invalid -> {
                val current = dao.audioTranscriptionJob(job.blobName) ?: return@forEach
                dao.transitionAudioTranscriptionFailure(
                    blobName = job.blobName,
                    expectedRecoveryCycles = current.recoveryCycles,
                    newStatus = AUDIO_JOB_ABANDONED,
                    newRecoveryCycles = current.recoveryCycles,
                    nextAttemptAt = 0L,
                    reason = queued.reason.take(AUDIO_FAILURE_REASON_LIMIT),
                    updatedAt = now,
                )
            }
        }
    }
    return if (dispatched > 0) AudioDispatchResult.DISPATCHED else AudioDispatchResult.IDLE
}

internal fun selectAudioRecoveryJobs(
    jobs: List<AudioTranscriptionJobEntity>,
    now: Long,
    maxJobs: Int,
): List<AudioTranscriptionJobEntity> {
    if (maxJobs <= 0) return emptyList()
    return jobs.asSequence()
        .filter { it.status == AUDIO_JOB_PENDING }
        .filter { it.nextAttemptAt <= now }
        .filter {
            validAudioTranscriptionMetadata(it.blobName, it.segmentStartedAt, it.segmentEndedAt)
        }
        .distinctBy(AudioTranscriptionJobEntity::blobName)
        .sortedWith(
            compareBy<AudioTranscriptionJobEntity> { it.nextAttemptAt }
                .thenBy { it.segmentEndedAt }
                .thenBy { it.blobName },
        )
        .take(maxJobs)
        .toList()
}

internal data class AudioFailureRecoveryState(
    val status: String,
    val recoveryCycles: Int,
    val nextAttemptAt: Long,
)

internal fun nextAudioFailureRecoveryState(
    currentRecoveryCycles: Int,
    now: Long,
    recoverable: Boolean,
    maxRecoveryCycles: Int = AUDIO_MAX_RECOVERY_CYCLES,
): AudioFailureRecoveryState {
    if (!recoverable || currentRecoveryCycles < 0 || currentRecoveryCycles >= maxRecoveryCycles) {
        return AudioFailureRecoveryState(AUDIO_JOB_ABANDONED, currentRecoveryCycles.coerceAtLeast(0), 0L)
    }
    val nextCycle = currentRecoveryCycles + 1
    return AudioFailureRecoveryState(
        status = AUDIO_JOB_PENDING,
        recoveryCycles = nextCycle,
        nextAttemptAt = now + audioRecoveryDelayMs(nextCycle),
    )
}

internal fun audioRecoveryDelayMs(recoveryCycle: Int): Long {
    if (recoveryCycle <= 0) return AUDIO_RECOVERY_BASE_DELAY_MS
    var delay = AUDIO_RECOVERY_BASE_DELAY_MS
    repeat((recoveryCycle - 1).coerceAtMost(8)) {
        delay = (delay * 4L).coerceAtMost(AUDIO_RECOVERY_MAX_DELAY_MS)
    }
    return delay
}

internal fun shouldRecoverAudioFailure(reason: String): Boolean = when {
    reason.startsWith("encrypted_segment_unreadable") -> false
    reason.startsWith("invalid_segment_metadata") -> false
    else -> true
}

internal fun validAudioTranscriptionMetadata(blobName: String, startedAt: Long, endedAt: Long): Boolean =
    blobName.isNotBlank() && startedAt > 0L && endedAt >= startedAt

internal const val AUDIO_JOB_PENDING = "pending"
internal const val AUDIO_JOB_ENQUEUED = "enqueued"
internal const val AUDIO_JOB_COMPLETED = "completed"
internal const val AUDIO_JOB_ABANDONED = "abandoned"
internal const val AUDIO_MAX_RECOVERY_CYCLES = 3
internal const val AUDIO_FAILURE_REASON_LIMIT = 160
private const val AUDIO_RECOVERY_SCAN_LIMIT = 36
private const val AUDIO_ORPHAN_SCAN_LIMIT = 256
private const val AUDIO_ORPHAN_STALE_MS = 2L * 60 * 1000
private const val AUDIO_ORPHAN_LOOKBACK_MS = 7L * 24 * 60 * 60 * 1000
private const val AUDIO_ORPHAN_MIN_ENCRYPTED_BYTES = 32L * 1024
private const val AUDIO_SEGMENT_ESTIMATED_MS = 30L * 1000
private const val AUDIO_RECOVERY_PERIOD_MINUTES = 15L
private const val AUDIO_RECOVERY_REFILL_DELAY_SECONDS = 2L
private const val AUDIO_CONFIGURATION_RECHECK_MS = 15L * 60 * 1000
private const val AUDIO_ENQUEUE_LEASE_MS = 2L * 60 * 60 * 1000
private const val AUDIO_RECOVERY_BASE_DELAY_MS = 15L * 60 * 1000
private const val AUDIO_RECOVERY_MAX_DELAY_MS = 24L * 60 * 60 * 1000
private val audioDispatchMutex = Mutex()
private val AUDIO_SEGMENT_FILE = Regex("^audio-(\\d+)-segment-(\\d+)\\.pcm\\.mch$")
