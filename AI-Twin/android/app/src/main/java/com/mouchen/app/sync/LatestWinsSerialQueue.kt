package com.mouchen.app.sync

/**
 * Serializes writes while retaining only the newest value selected during an active write.
 *
 * [enqueue] returns the value that should start immediately, or null when another write owns the
 * lane. After that write finishes, [complete] returns the newest queued value and keeps the lane
 * reserved for it. This makes network completion order identical to selection order without
 * blocking rapid UI changes.
 */
internal class LatestWinsSerialQueue<T : Any> {
    private var active = false
    private var pending: T? = null

    @Synchronized
    fun enqueue(value: T): T? {
        if (active) {
            pending = value
            return null
        }
        active = true
        return value
    }

    @Synchronized
    fun complete(): T? {
        val next = pending
        pending = null
        if (next == null) active = false
        return next
    }

    @Synchronized
    fun discardPending() {
        pending = null
    }

    @Synchronized
    fun hasWork(): Boolean = active || pending != null
}
