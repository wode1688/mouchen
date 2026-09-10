package com.mouchen.app.collectors

import android.content.Context
import android.util.Log
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.UrgencyClassifier
import com.mouchen.app.sync.AuthFence
import com.mouchen.app.sync.captureAuthFence
import com.mouchen.app.sync.effectiveCollectionConsent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.atomic.AtomicLong

/** Serializes high-frequency sensor events through a bounded, failure-isolated queue. */
internal class BoundedEventWriter(
    context: Context,
    scope: CoroutineScope,
    capacity: Int,
) : AutoCloseable {
    private val appContext = context.applicationContext
    private val queue = Channel<AuthBoundEvent>(capacity = capacity)
    private val lastErrorLogAt = AtomicLong(0L)
    private val writeMutex = Mutex()

    private val worker = scope.launch {
        try {
            for (event in queue) safeInsert(event, enqueueSync = true)
        } catch (_: CancellationException) {
            // Service shutdown is an expected terminal state for this root coroutine.
        } catch (error: Exception) {
            logFailure(error)
        }
    }

    fun offer(event: LocalEventEntity): Boolean {
        val fence = captureAuthFence(appContext) ?: return false
        if (!effectiveCollectionConsent(appContext) || !fence.isCurrent(appContext)) return false
        val bound = AuthBoundEvent(event, fence)
        return offerDurably(
            event = bound,
            mustPersistNow = UrgencyClassifier.classify(event) != null || event.source == DURABLE_IME_SOURCE,
            offerToMemory = { queue.trySend(it).isSuccess },
            persistNow = ::persistBlocking,
        )
    }

    suspend fun writeNow(event: LocalEventEntity, enqueueSync: Boolean = true): Boolean {
        val fence = captureAuthFence(appContext)?.takeIf { it.isCurrent(appContext) } ?: return false
        if (!effectiveCollectionConsent(appContext)) return false
        return safeInsert(AuthBoundEvent(event, fence), enqueueSync)
    }

    private suspend fun safeInsert(bound: AuthBoundEvent, enqueueSync: Boolean): Boolean {
        if (!bound.fence.isCurrent(appContext) || !effectiveCollectionConsent(appContext)) return false
        return try {
            val retained = writeMutex.withLock {
                val dao = MouchenDatabase.get(appContext).dao()
                dao.insertEventIfAbsent(bound.event)
                if (!bound.fence.isCurrent(appContext)) {
                    dao.deleteEvent(bound.event.id)
                    false
                } else {
                    true
                }
            }
            if (!retained) return false
            if (!bound.fence.isCurrent(appContext)) return false
            if (enqueueSync) RealtimeSyncWorker.enqueueForEvent(appContext, bound.event)
            true
        } catch (error: CancellationException) {
            throw error
        } catch (error: Exception) {
            logFailure(error)
            false
        }
    }

    /** Queue overflow/closure is uncommon, so blocking is reserved for the no-loss fallback. */
    private fun persistBlocking(event: AuthBoundEvent): Boolean =
        runBlocking(Dispatchers.IO) { safeInsert(event, enqueueSync = true) }

    private fun logFailure(error: Exception) {
        val now = System.currentTimeMillis()
        val previous = lastErrorLogAt.get()
        if (now - previous >= ERROR_LOG_INTERVAL_MS && lastErrorLogAt.compareAndSet(previous, now)) {
            Log.w(TAG, "Local event write failed", error)
        }
    }

    override fun close() {
        queue.close()
    }

    /** Closes the queue and waits until every queued event has completed its persistence attempt. */
    suspend fun closeAndDrain() {
        queue.close()
        worker.join()
    }

    private companion object {
        const val TAG = "MouchenEventWriter"
        const val DURABLE_IME_SOURCE = "android.ime"
        const val ERROR_LOG_INTERVAL_MS = 60_000L
    }
}

private data class AuthBoundEvent(val event: LocalEventEntity, val fence: AuthFence)

internal fun <T> offerDurably(
    event: T,
    mustPersistNow: Boolean,
    offerToMemory: (T) -> Boolean,
    persistNow: (T) -> Boolean,
): Boolean {
    if (!mustPersistNow && offerToMemory(event)) return true
    return persistNow(event)
}
