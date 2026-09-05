package com.mouchen.app.sync

import android.content.Context
import android.content.SharedPreferences

/**
 * Persists a coalesced sync demand. While a worker owns the active demand, new events only
 * advance its generation; they do not append one WorkManager request per event.
 */
internal class SyncDemandStore internal constructor(
    private val persistence: SyncDemandPersistence,
) {
    constructor(context: Context) : this(
        SharedPreferencesSyncDemandPersistence(
            context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE),
        ),
    )

    internal constructor(context: Context, preferencesName: String) : this(
        SharedPreferencesSyncDemandPersistence(
            context.applicationContext.getSharedPreferences(preferencesName, Context.MODE_PRIVATE),
        ),
    )

    fun request(trigger: String): SyncDemandLease = synchronized(lock) {
        val current = persistence.read()
        val transition = requestSyncDemand(current.toState())
        val persisted = persistence.write(
            SyncDemandSnapshot(transition.state.generation, transition.state.active, trigger.take(120)),
        )
        SyncDemandLease(transition.state.generation, transition.shouldSchedule, persisted)
    }

    fun recover(trigger: String): SyncDemandLease = synchronized(lock) {
        // WorkManager normally survives process death, but an explicitly cancelled/pruned work
        // must not leave a permanent active=true tombstone that suppresses future requests.
        val transition = recoverSyncDemand(persistence.read().toState())
        val persisted = persistence.write(
            SyncDemandSnapshot(transition.state.generation, transition.state.active, trigger.take(120)),
        )
        SyncDemandLease(transition.state.generation, transition.shouldSchedule, persisted)
    }

    fun snapshot(): SyncDemandSnapshot = synchronized(lock) { persistence.read() }

    fun completeIfUnchanged(handledGeneration: Long): Boolean = synchronized(lock) {
        val current = persistence.read()
        val transition = completeSyncDemand(current.toState(), handledGeneration)
        transition.completed && persistence.write(current.copy(active = transition.state.active))
    }

    fun release(): Boolean = synchronized(lock) {
        val current = persistence.read()
        persistence.write(current.copy(active = false))
    }

    fun rollbackScheduledRequest(expectedGeneration: Long): Boolean = synchronized(lock) {
        val current = persistence.read()
        if (current.generation != expectedGeneration) return@synchronized false
        persistence.write(current.copy(active = false))
    }

    private companion object {
        val lock = Any()
    }
}

internal interface SyncDemandPersistence {
    fun read(): SyncDemandSnapshot
    fun write(snapshot: SyncDemandSnapshot): Boolean
}

private class SharedPreferencesSyncDemandPersistence(
    private val preferences: SharedPreferences,
) : SyncDemandPersistence {
    override fun read(): SyncDemandSnapshot = SyncDemandSnapshot(
        generation = preferences.getLong(KEY_GENERATION, 0L),
        active = preferences.getBoolean(KEY_ACTIVE, false),
        trigger = preferences.getString(KEY_TRIGGER, null).orEmpty().ifBlank { "event" },
    )

    override fun write(snapshot: SyncDemandSnapshot): Boolean = preferences.edit()
        .putLong(KEY_GENERATION, snapshot.generation)
        .putBoolean(KEY_ACTIVE, snapshot.active)
        .putString(KEY_TRIGGER, snapshot.trigger)
        .commit()
}

internal data class SyncDemandSnapshot(
    val generation: Long,
    val active: Boolean,
    val trigger: String,
)

internal data class SyncDemandLease(
    val generation: Long,
    val shouldSchedule: Boolean,
    val persisted: Boolean,
)

internal data class SyncDemandState(
    val generation: Long,
    val active: Boolean,
)

internal data class SyncDemandRequest(
    val state: SyncDemandState,
    val shouldSchedule: Boolean,
)

internal data class SyncDemandCompletion(
    val state: SyncDemandState,
    val completed: Boolean,
)

internal fun requestSyncDemand(state: SyncDemandState): SyncDemandRequest {
    val nextGeneration = if (state.generation == Long.MAX_VALUE) 1L else state.generation + 1L
    return SyncDemandRequest(
        state = SyncDemandState(generation = nextGeneration, active = true),
        shouldSchedule = !state.active,
    )
}

internal fun recoverSyncDemand(state: SyncDemandState): SyncDemandRequest =
    requestSyncDemand(state.copy(active = false))

private fun SyncDemandSnapshot.toState(): SyncDemandState =
    SyncDemandState(generation = generation, active = active)

internal fun completeSyncDemand(
    state: SyncDemandState,
    handledGeneration: Long,
): SyncDemandCompletion {
    if (state.generation != handledGeneration) {
        return SyncDemandCompletion(state = state, completed = false)
    }
    return SyncDemandCompletion(
        state = state.copy(active = false),
        completed = true,
    )
}

private const val PREFERENCES = "realtime_sync_demand"
private const val KEY_GENERATION = "generation"
private const val KEY_ACTIVE = "active"
private const val KEY_TRIGGER = "trigger"
