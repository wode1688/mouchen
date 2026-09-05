package com.mouchen.app.sync

import android.app.NotificationManager
import android.provider.CalendarContract
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TimingContextProviderTest {
    @Test
    fun overnightQuietHoursCoverBothSidesOfMidnight() {
        val settings = TimingSettings(
            quietHoursEnabled = true,
            quietStartMinute = 23 * 60,
            quietEndMinute = 7 * 60,
        )

        assertTrue(isWithinQuietHours(settings, 23 * 60 + 30))
        assertTrue(isWithinQuietHours(settings, 6 * 60 + 59))
        assertFalse(isWithinQuietHours(settings, 7 * 60))
        assertFalse(isWithinQuietHours(settings, 12 * 60))
    }

    @Test
    fun quietHoursRequireExplicitEnableAndNonZeroWindow() {
        assertFalse(
            isWithinQuietHours(
                TimingSettings(quietHoursEnabled = false, quietStartMinute = 60, quietEndMinute = 120),
                90,
            ),
        )
        assertFalse(
            isWithinQuietHours(
                TimingSettings(quietHoursEnabled = true, quietStartMinute = 60, quietEndMinute = 60),
                60,
            ),
        )
    }

    @Test
    fun systemDndFiltersAreQuietButUnknownAndAllAreNot() {
        assertTrue(isInterruptionFilterQuiet(NotificationManager.INTERRUPTION_FILTER_PRIORITY))
        assertTrue(isInterruptionFilterQuiet(NotificationManager.INTERRUPTION_FILTER_ALARMS))
        assertTrue(isInterruptionFilterQuiet(NotificationManager.INTERRUPTION_FILTER_NONE))
        assertFalse(isInterruptionFilterQuiet(NotificationManager.INTERRUPTION_FILTER_ALL))
        assertFalse(isInterruptionFilterQuiet(NotificationManager.INTERRUPTION_FILTER_UNKNOWN))
    }

    @Test
    fun meetingRequiresCurrentNonAllDayNonFreeInterval() {
        val now = 10_000L
        val busy = CalendarBusyInterval(
            beginMillis = 9_000L,
            endMillis = 11_000L,
            allDay = false,
            availability = CalendarContract.Events.AVAILABILITY_BUSY,
        )
        val allDay = busy.copy(allDay = true)
        val free = busy.copy(availability = CalendarContract.Events.AVAILABILITY_FREE)
        val ended = busy.copy(endMillis = now)

        assertTrue(hasCurrentMeeting(listOf(busy), now))
        assertFalse(hasCurrentMeeting(listOf(allDay, free, ended), now))
    }

    @Test
    fun temporaryDrivingExpiresAtItsDeadline() {
        val settings = TimingSettings(drivingUntilMillis = 2_000L)

        assertTrue(settings.isDriving(1_999L))
        assertFalse(settings.isDriving(2_000L))
        assertFalse(settings.isDriving(2_001L))
    }

    @Test
    fun clockTextParsesAndFormatsStrictly() {
        assertEquals(23 * 60 + 5, parseClockMinute("23:05"))
        assertEquals("23:05", formatClockMinute(23 * 60 + 5))
        assertEquals(null, parseClockMinute("24:00"))
        assertEquals(null, parseClockMinute("9"))
    }

    @Test
    fun timingQueryCarriesEveryIndependentSignal() {
        val url = appendTimingContextQuery(
            "https://example.test/v1/events?sleeping=false",
            TimingContextSnapshot(
                sleeping = true,
                driving = false,
                inMeeting = true,
                quietHours = true,
            ),
        )

        assertTrue(url.contains("sleeping=true"))
        assertTrue(url.contains("driving=false"))
        assertTrue(url.contains("in_meeting=true"))
        assertTrue(url.contains("quiet_hours=true"))
        assertEquals(1, "sleeping=true".toRegex().findAll(url).count())
    }
}
