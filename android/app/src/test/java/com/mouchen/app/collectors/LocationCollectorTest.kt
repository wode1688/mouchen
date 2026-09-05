package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos

class LocationCollectorTest {
    @Test
    fun newestValidCandidateWinsAndInvalidCoordinatesAreIgnored() {
        val now = 1_000_000L
        val older = LocationCandidate(31.2304, 121.4737, 20.0, now - 20_000L)
        val newest = LocationCandidate(31.2200, 121.4800, 100.0, now - 5_000L)
        val invalid = LocationCandidate(95.0, 121.0, 1.0, now)

        assertEquals(newest, selectLocationCandidate(listOf(older, newest, invalid), now, 60_000L))
    }

    @Test
    fun staleOrFutureCandidatesAreRejected() {
        val now = 1_000_000L

        assertNull(
            selectLocationCandidate(
                listOf(LocationCandidate(31.0, 121.0, 20.0, now - 61_000L)),
                now,
                60_000L,
            ),
        )
        assertNull(
            selectLocationCandidate(
                listOf(LocationCandidate(31.0, 121.0, 20.0, now + 6 * 60_000L)),
                now,
                60_000L,
            ),
        )
    }

    @Test
    fun minimizationUsesCoarseGridAndNeverAdvertisesFineAccuracy() {
        val now = 2_000_000L
        val exact = LocationCandidate(31.230416, 121.473701, 8.0, now - 12_345L)

        val minimized = minimizeLocation(exact, now)

        assertNotEquals(exact.latitude, minimized.gridLatitude, 0.0)
        assertNotEquals(exact.longitude, minimized.gridLongitude, 0.0)
        assertEquals(0.05, minimized.gridSizeDegrees, 0.0)
        assertTrue(minimized.precisionMeters >= 5_500)
        val longitudeWidthMeters = minimized.gridLongitudeSizeDegrees *
            111_320.0 * cos(Math.toRadians(exact.latitude))
        assertTrue(longitudeWidthMeters >= 5_000.0)
        assertEquals(12_345L, minimized.ageMs)
    }

    @Test
    fun longitudeGridRemainsAtLeastFiveKilometersAtHighLatitude() {
        val latitude = 75.0
        val gridDegrees = longitudeGridSizeDegrees(latitude)
        val widthMeters = gridDegrees * 111_320.0 * cos(Math.toRadians(latitude))

        assertTrue(gridDegrees > 0.05)
        assertTrue(widthMeters >= 5_000.0)
    }

    @Test
    fun datelineCellIsNotANarrowRemainder() {
        val now = 2_000_000L
        val latitude = 31.230416
        val minimized = minimizeLocation(
            LocationCandidate(latitude, 179.9999, 8.0, now),
            now,
        )
        val cellCount = 360.0 / minimized.gridLongitudeSizeDegrees
        val widthMeters = minimized.gridLongitudeSizeDegrees *
            111_320.0 * cos(Math.toRadians(latitude))

        assertEquals(kotlin.math.round(cellCount), cellCount, 1e-9)
        assertTrue(minimized.gridLongitude < 180.0)
        assertTrue(widthMeters >= 5_000.0)
    }

    @Test
    fun eventIdIsStableForSameCoarseSnapshot() {
        val first = MinimizedLocation(31.225, 121.475, 0.05, 5_000, 1_000L)
        val second = first.copy(ageMs = 2_000L)

        assertEquals(locationEventId(first, 10L), locationEventId(second, 10L))
        assertNotEquals(locationEventId(first, 10L), locationEventId(first, 11L))
    }
}
