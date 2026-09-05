package com.mouchen.app.collectors

import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

internal enum class MicrophoneUse(val label: String) {
    CONTINUOUS_CAPTURE("持续录音"),
    IME_DICTATION("输入法语音"),
}

/**
 * Serializes microphone owners inside the app process. The persisted capture status remains useful
 * for UI, but it cannot close the start/stop race around the actual AudioRecord resource.
 */
internal object ProcessMicrophoneLease {
    private data class Holder(val use: MicrophoneUse)

    private val holder = AtomicReference<Holder?>(null)

    fun tryAcquire(use: MicrophoneUse): Lease? {
        val requested = Holder(use)
        return if (holder.compareAndSet(null, requested)) Lease(requested) else null
    }

    fun currentUse(): MicrophoneUse? = holder.get()?.use

    internal class Lease internal constructor(
        private val acquired: Any,
    ) : AutoCloseable {
        private val closed = AtomicBoolean(false)

        override fun close() {
            if (closed.compareAndSet(false, true)) holder.compareAndSet(acquired as? Holder, null)
        }
    }
}
