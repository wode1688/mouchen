package com.mouchen.app.sync

import android.Manifest
import android.app.NotificationManager
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import java.time.Instant
import java.time.ZoneId

data class TimingContextSnapshot(
    val sleeping: Boolean = false,
    val driving: Boolean = false,
    val inMeeting: Boolean = false,
    val quietHours: Boolean = false,
)

data class TimingSettings(
    val quietHoursEnabled: Boolean = false,
    val quietStartMinute: Int = DEFAULT_QUIET_START_MINUTE,
    val quietEndMinute: Int = DEFAULT_QUIET_END_MINUTE,
    val drivingUntilMillis: Long = 0L,
) {
    fun isDriving(nowMillis: Long): Boolean = drivingUntilMillis > nowMillis
}

internal data class CalendarBusyInterval(
    val beginMillis: Long,
    val endMillis: Long,
    val allDay: Boolean,
    val availability: Int,
)

fun interface TimingContextSource {
    fun snapshot(): TimingContextSnapshot
}

class TimingSettingsStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun load(): TimingSettings = TimingSettings(
        quietHoursEnabled = preferences.getBoolean(KEY_QUIET_ENABLED, false),
        quietStartMinute = preferences.getInt(KEY_QUIET_START, DEFAULT_QUIET_START_MINUTE)
            .takeIf { it in MINUTE_RANGE } ?: DEFAULT_QUIET_START_MINUTE,
        quietEndMinute = preferences.getInt(KEY_QUIET_END, DEFAULT_QUIET_END_MINUTE)
            .takeIf { it in MINUTE_RANGE } ?: DEFAULT_QUIET_END_MINUTE,
        drivingUntilMillis = preferences.getLong(KEY_DRIVING_UNTIL, 0L).coerceAtLeast(0L),
    )

    fun saveQuietHours(enabled: Boolean, startMinute: Int, endMinute: Int): TimingSettings {
        require(startMinute in MINUTE_RANGE) { "quiet_start_out_of_range" }
        require(endMinute in MINUTE_RANGE) { "quiet_end_out_of_range" }
        require(!enabled || startMinute != endMinute) { "quiet_hours_cannot_span_zero_minutes" }
        preferences.edit()
            .putBoolean(KEY_QUIET_ENABLED, enabled)
            .putInt(KEY_QUIET_START, startMinute)
            .putInt(KEY_QUIET_END, endMinute)
            .apply()
        return load()
    }

    fun setTemporaryDriving(enabled: Boolean, nowMillis: Long = System.currentTimeMillis()): TimingSettings {
        val until = if (enabled) nowMillis + TEMPORARY_DRIVING_MILLIS else 0L
        preferences.edit().putLong(KEY_DRIVING_UNTIL, until).apply()
        return load()
    }

    private companion object {
        const val PREFERENCES = "mouchen_timing_settings"
        const val KEY_QUIET_ENABLED = "quiet.enabled"
        const val KEY_QUIET_START = "quiet.start_minute"
        const val KEY_QUIET_END = "quiet.end_minute"
        const val KEY_DRIVING_UNTIL = "driving.until_millis"
        const val TEMPORARY_DRIVING_MILLIS = 2L * 60 * 60 * 1000
    }
}

class AndroidTimingContextProvider(
    private val context: Context,
    private val settingsStore: TimingSettingsStore = TimingSettingsStore(context),
    private val nowMillis: () -> Long = System::currentTimeMillis,
    private val zoneId: () -> ZoneId = ZoneId::systemDefault,
) : TimingContextSource {
    override fun snapshot(): TimingContextSnapshot {
        val now = nowMillis()
        val settings = settingsStore.load()
        val localMinute = Instant.ofEpochMilli(now).atZone(zoneId()).let { it.hour * 60 + it.minute }
        return TimingContextSnapshot(
            // Android exposes no trustworthy general-purpose sleep signal without a separate
            // health/sleep integration. Quiet hours and DND therefore remain a distinct fact.
            sleeping = false,
            driving = settings.isDriving(now),
            inMeeting = readCurrentMeeting(context, now),
            quietHours = isWithinQuietHours(settings, localMinute) || isSystemDoNotDisturbActive(context),
        )
    }
}

internal fun isWithinQuietHours(settings: TimingSettings, minuteOfDay: Int): Boolean {
    if (!settings.quietHoursEnabled || minuteOfDay !in MINUTE_RANGE) return false
    val start = settings.quietStartMinute
    val end = settings.quietEndMinute
    if (start !in MINUTE_RANGE || end !in MINUTE_RANGE || start == end) return false
    return if (start < end) {
        minuteOfDay in start until end
    } else {
        minuteOfDay >= start || minuteOfDay < end
    }
}

internal fun isInterruptionFilterQuiet(interruptionFilter: Int): Boolean = when (interruptionFilter) {
    NotificationManager.INTERRUPTION_FILTER_PRIORITY,
    NotificationManager.INTERRUPTION_FILTER_ALARMS,
    NotificationManager.INTERRUPTION_FILTER_NONE,
    -> true
    else -> false
}

internal fun hasCurrentMeeting(intervals: List<CalendarBusyInterval>, nowMillis: Long): Boolean =
    intervals.any { interval ->
        !interval.allDay &&
            interval.availability != CalendarContract.Events.AVAILABILITY_FREE &&
            interval.beginMillis <= nowMillis &&
            interval.endMillis > nowMillis
    }

internal fun parseClockMinute(value: String): Int? {
    val parts = value.trim().split(':')
    if (parts.size != 2) return null
    val hour = parts[0].toIntOrNull() ?: return null
    val minute = parts[1].toIntOrNull() ?: return null
    if (hour !in 0..23 || minute !in 0..59) return null
    return hour * 60 + minute
}

internal fun formatClockMinute(minuteOfDay: Int): String {
    val normalized = minuteOfDay.takeIf { it in MINUTE_RANGE } ?: 0
    val hour = (normalized / 60).toString().padStart(2, '0')
    val minute = (normalized % 60).toString().padStart(2, '0')
    return "$hour:$minute"
}

private fun isSystemDoNotDisturbActive(context: Context): Boolean = runCatching {
    val manager = context.getSystemService(NotificationManager::class.java)
    manager != null && isInterruptionFilterQuiet(manager.currentInterruptionFilter)
}.getOrDefault(false)

private fun readCurrentMeeting(context: Context, nowMillis: Long): Boolean {
    if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) != PackageManager.PERMISSION_GRANTED) {
        return false
    }
    val builder = CalendarContract.Instances.CONTENT_URI.buildUpon()
    ContentUris.appendId(builder, nowMillis)
    ContentUris.appendId(builder, nowMillis + 1L)
    val projection = arrayOf(
        CalendarContract.Instances.BEGIN,
        CalendarContract.Instances.END,
        CalendarContract.Instances.ALL_DAY,
        CalendarContract.Events.AVAILABILITY,
    )
    return runCatching {
        val intervals = mutableListOf<CalendarBusyInterval>()
        context.contentResolver.query(builder.build(), projection, null, null, null)?.use { cursor ->
            while (cursor.moveToNext()) {
                intervals += CalendarBusyInterval(
                    beginMillis = cursor.getLong(0),
                    endMillis = cursor.getLong(1),
                    allDay = cursor.getInt(2) == 1,
                    availability = cursor.getInt(3),
                )
            }
        }
        hasCurrentMeeting(intervals, nowMillis)
    }.getOrDefault(false)
}

internal const val DEFAULT_QUIET_START_MINUTE = 23 * 60
internal const val DEFAULT_QUIET_END_MINUTE = 7 * 60
private val MINUTE_RANGE = 0 until 24 * 60
