package com.mouchen.app.collectors

import android.content.Context
import com.mouchen.app.data.MouchenDao

/** Play distribution deliberately has no phone-provider implementation. */
internal class ContactContextCollector(
    @Suppress("UNUSED_PARAMETER") context: Context,
    @Suppress("UNUSED_PARAMETER") dao: MouchenDao,
) {
    @Suppress("UNUSED_PARAMETER")
    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int = 0
}

internal class SmsContextCollector(
    @Suppress("UNUSED_PARAMETER") context: Context,
    @Suppress("UNUSED_PARAMETER") dao: MouchenDao,
) {
    @Suppress("UNUSED_PARAMETER")
    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int = 0
}

internal class CallLogContextCollector(
    @Suppress("UNUSED_PARAMETER") context: Context,
    @Suppress("UNUSED_PARAMETER") dao: MouchenDao,
) {
    @Suppress("UNUSED_PARAMETER")
    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int = 0
}
