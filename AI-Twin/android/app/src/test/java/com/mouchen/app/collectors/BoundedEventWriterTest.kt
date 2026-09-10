package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BoundedEventWriterTest {
    @Test
    fun urgentEventBypassesMemoryAndIsPersistedSynchronously() {
        var memoryOffers = 0
        var durableWrites = 0

        val accepted = offerDurably(
            event = "urgent",
            mustPersistNow = true,
            offerToMemory = {
                memoryOffers += 1
                true
            },
            persistNow = {
                durableWrites += 1
                true
            },
        )

        assertTrue(accepted)
        assertEquals(0, memoryOffers)
        assertEquals(1, durableWrites)
    }

    @Test
    fun fullOrClosedMemoryQueueFallsBackToDurablePersistence() {
        var durableWrites = 0

        val accepted = offerDurably(
            event = "normal",
            mustPersistNow = false,
            offerToMemory = { false },
            persistNow = {
                durableWrites += 1
                true
            },
        )

        assertTrue(accepted)
        assertEquals(1, durableWrites)
    }

    @Test
    fun successfulMemoryOfferDoesNotPerformASecondWrite() {
        var durableWrites = 0

        val accepted = offerDurably(
            event = "normal",
            mustPersistNow = false,
            offerToMemory = { true },
            persistNow = {
                durableWrites += 1
                false
            },
        )

        assertTrue(accepted)
        assertEquals(0, durableWrites)
        assertFalse(durableWrites > 0)
    }
}
