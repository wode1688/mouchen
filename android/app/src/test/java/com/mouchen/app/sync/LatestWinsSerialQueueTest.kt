package com.mouchen.app.sync

import com.mouchen.app.localization.LOCALE_EN_US
import com.mouchen.app.localization.LOCALE_ZH_CN
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LatestWinsSerialQueueTest {
    @Test
    fun slowFirstWriteThenFastSecondSelectionLeavesServerAtLastSelection() {
        val queue = LatestWinsSerialQueue<String>()
        val executor = Executors.newCachedThreadPool()
        val firstStarted = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val completed = CountDownLatch(2)
        val serverLocale = AtomicReference(LOCALE_EN_US)
        val serverWrites = Collections.synchronizedList(mutableListOf<String>())

        lateinit var send: (String) -> Unit
        send = { locale ->
            executor.execute {
                if (locale == LOCALE_ZH_CN) {
                    firstStarted.countDown()
                    if (!releaseFirst.await(5, TimeUnit.SECONDS)) return@execute
                }
                serverLocale.set(locale)
                serverWrites += locale
                completed.countDown()
                queue.complete()?.let(send)
            }
        }

        try {
            queue.enqueue(LOCALE_ZH_CN)?.let(send)
            assertTrue("the deliberately slow first write did not start", firstStarted.await(5, TimeUnit.SECONDS))

            // The user changes their mind while the first PUT is still in flight. The second value
            // is retained but must not race the first request at the server.
            assertNull(queue.enqueue(LOCALE_EN_US))
            releaseFirst.countDown()

            assertTrue("both serialized writes did not finish", completed.await(5, TimeUnit.SECONDS))
            assertEquals(listOf(LOCALE_ZH_CN, LOCALE_EN_US), serverWrites)
            assertEquals(LOCALE_EN_US, serverLocale.get())
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
        }
    }

    @Test
    fun coalescedOptimisticSelectionsRollBackToLastServerAcknowledgement() {
        val session = AuthSession(
            userId = "user-a",
            username = "owner-a",
            accessToken = "token-a",
            sessionId = "session-a",
            deviceId = "device-a",
            serverOrigin = "https://example.test",
            epoch = 7L,
            locale = LOCALE_ZH_CN,
        )

        // The first EN write reached the server. While it was slow, ZH was queued and then
        // replaced by a final EN choice. If that redundant final write fails, the discarded ZH
        // optimistic state must not be used as the rollback target.
        val serverLocaleAfterFirstSuccess = LOCALE_EN_US
        val rollback = resolveAccountLocaleRollback(
            requestSession = session,
            acknowledgedSession = session.copy(locale = serverLocaleAfterFirstSuccess),
            acknowledgedLocale = serverLocaleAfterFirstSuccess,
        )

        assertEquals(serverLocaleAfterFirstSuccess, rollback)
    }
}
