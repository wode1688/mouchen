package com.mouchen.app.status

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import org.json.JSONObject

enum class PipelinePhase {
    IDLE,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    PARTIAL_FAILURE,
    FAILED,
    DISABLED,
    AUTH_REQUIRED,
}

data class PipelineStageStatus(
    val phase: PipelinePhase = PipelinePhase.IDLE,
    val trigger: String = "",
    val requestedAt: Long = 0L,
    val startedAt: Long = 0L,
    val finishedAt: Long = 0L,
    val attempted: Int = 0,
    val succeeded: Int = 0,
    val failed: Int = 0,
    val detail: String = "",
)

data class PipelineStatusSnapshot(
    val collection: PipelineStageStatus = PipelineStageStatus(),
    val sync: PipelineStageStatus = PipelineStageStatus(),
)

class PipelineStatusStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun observe(): Flow<PipelineStatusSnapshot> = callbackFlow {
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == SNAPSHOT_KEY) trySend(load())
        }
        trySend(load())
        preferences.registerOnSharedPreferenceChangeListener(listener)
        awaitClose { preferences.unregisterOnSharedPreferenceChangeListener(listener) }
    }.distinctUntilChanged()

    fun load(): PipelineStatusSnapshot = PipelineStatusCodec.decode(preferences.getString(SNAPSHOT_KEY, null))

    fun markCollectionQueued(trigger: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            collection = snapshot.collection.copy(
                phase = PipelinePhase.QUEUED,
                trigger = trigger,
                requestedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = "",
            ),
        )
    }

    fun markCollectionRunning(trigger: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            collection = snapshot.collection.copy(
                phase = PipelinePhase.RUNNING,
                trigger = trigger,
                requestedAt = snapshot.collection.requestedAt.takeIf { it > 0L } ?: now,
                startedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = "",
            ),
        )
    }

    fun markCollectionFinished(
        attempted: Int,
        succeeded: Int,
        failures: List<String>,
        now: Long = System.currentTimeMillis(),
    ) = update { snapshot ->
        snapshot.copy(
            collection = snapshot.collection.copy(
                phase = if (failures.isEmpty()) PipelinePhase.SUCCEEDED else PipelinePhase.PARTIAL_FAILURE,
                finishedAt = now,
                attempted = attempted,
                succeeded = succeeded,
                failed = failures.size,
                detail = failures.joinToString(limit = 4, truncated = "…"),
            ),
        )
    }

    fun markCollectionFailed(detail: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            collection = snapshot.collection.copy(
                phase = PipelinePhase.FAILED,
                finishedAt = now,
                failed = maxOf(snapshot.collection.failed, 1),
                detail = detail.take(MAX_DETAIL_CHARS),
            ),
        )
    }

    fun markCollectionDisabled(detail: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            collection = snapshot.collection.copy(
                phase = PipelinePhase.DISABLED,
                finishedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = detail.take(MAX_DETAIL_CHARS),
            ),
        )
    }

    fun markSyncQueued(trigger: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.QUEUED,
                trigger = trigger,
                requestedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = "",
            ),
        )
    }

    fun markSyncRunning(trigger: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.RUNNING,
                trigger = trigger,
                requestedAt = snapshot.sync.requestedAt.takeIf { it > 0L } ?: now,
                startedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = "",
            ),
        )
    }

    fun markSyncFinished(
        attempted: Int,
        succeeded: Int,
        failed: Int,
        detail: String = "",
        now: Long = System.currentTimeMillis(),
    ) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = when {
                    failed == 0 -> PipelinePhase.SUCCEEDED
                    succeeded > 0 -> PipelinePhase.PARTIAL_FAILURE
                    else -> PipelinePhase.FAILED
                },
                finishedAt = now,
                attempted = attempted,
                succeeded = succeeded,
                failed = failed,
                detail = detail.take(MAX_DETAIL_CHARS),
            ),
        )
    }

    fun markSyncDisabled(now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.DISABLED,
                finishedAt = now,
                attempted = 0,
                succeeded = 0,
                failed = 0,
                detail = "backend_disabled",
            ),
        )
    }

    fun markSyncAuthRequired(detail: String = "authentication_required", now: Long = System.currentTimeMillis()) =
        update { snapshot ->
            snapshot.copy(
                sync = snapshot.sync.copy(
                    phase = PipelinePhase.AUTH_REQUIRED,
                    finishedAt = now,
                    attempted = 0,
                    succeeded = 0,
                    failed = 1,
                    detail = detail.take(MAX_DETAIL_CHARS),
                ),
            )
        }

    fun markSessionHealthy(userId: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.QUEUED,
                requestedAt = now,
                detail = "session_healthy:${userId.take(12)}",
            ),
        )
    }

    fun markSessionCheckFailed(detail: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.FAILED,
                finishedAt = now,
                failed = maxOf(snapshot.sync.failed, 1),
                detail = "session_check_failed:${detail.take(220)}",
            ),
        )
    }

    fun markSyncContinuation(
        attempted: Int,
        succeeded: Int,
        detail: String,
        now: Long = System.currentTimeMillis(),
    ) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.QUEUED,
                finishedAt = now,
                attempted = attempted,
                succeeded = succeeded,
                failed = 0,
                detail = detail.take(MAX_DETAIL_CHARS),
            ),
        )
    }

    fun markSyncFailed(detail: String, now: Long = System.currentTimeMillis()) = update { snapshot ->
        snapshot.copy(
            sync = snapshot.sync.copy(
                phase = PipelinePhase.FAILED,
                finishedAt = now,
                failed = maxOf(snapshot.sync.failed, 1),
                detail = detail.take(MAX_DETAIL_CHARS),
            ),
        )
    }

    private fun update(transform: (PipelineStatusSnapshot) -> PipelineStatusSnapshot) {
        synchronized(UPDATE_LOCK) {
            val next = transform(load())
            preferences.edit().putString(SNAPSHOT_KEY, PipelineStatusCodec.encode(next)).apply()
        }
    }

    private companion object {
        const val PREFERENCES = "mouchen_pipeline_status"
        const val SNAPSHOT_KEY = "snapshot"
        const val MAX_DETAIL_CHARS = 500
        val UPDATE_LOCK = Any()
    }
}

internal object PipelineStatusCodec {
    fun encode(snapshot: PipelineStatusSnapshot): String = JSONObject()
        .put("collection", encodeStage(snapshot.collection))
        .put("sync", encodeStage(snapshot.sync))
        .toString()

    fun decode(raw: String?): PipelineStatusSnapshot = runCatching {
        if (raw.isNullOrBlank()) {
            PipelineStatusSnapshot()
        } else {
            val root = JSONObject(raw)
            PipelineStatusSnapshot(
                collection = decodeStage(root.optJSONObject("collection")),
                sync = decodeStage(root.optJSONObject("sync")),
            )
        }
    }.getOrDefault(PipelineStatusSnapshot())

    private fun encodeStage(stage: PipelineStageStatus): JSONObject = JSONObject()
        .put("phase", stage.phase.name)
        .put("trigger", stage.trigger)
        .put("requested_at", stage.requestedAt)
        .put("started_at", stage.startedAt)
        .put("finished_at", stage.finishedAt)
        .put("attempted", stage.attempted)
        .put("succeeded", stage.succeeded)
        .put("failed", stage.failed)
        .put("detail", stage.detail)

    private fun decodeStage(json: JSONObject?): PipelineStageStatus {
        if (json == null) return PipelineStageStatus()
        return PipelineStageStatus(
            phase = runCatching { PipelinePhase.valueOf(json.optString("phase")) }
                .getOrDefault(PipelinePhase.IDLE),
            trigger = json.optString("trigger"),
            requestedAt = json.optLong("requested_at"),
            startedAt = json.optLong("started_at"),
            finishedAt = json.optLong("finished_at"),
            attempted = json.optInt("attempted").coerceAtLeast(0),
            succeeded = json.optInt("succeeded").coerceAtLeast(0),
            failed = json.optInt("failed").coerceAtLeast(0),
            detail = json.optString("detail"),
        )
    }
}
