package com.mouchen.app.collectors

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.sync.RealtimeSyncWorker
import org.json.JSONObject

class CalendarCollector(private val context: Context, private val dao: MouchenDao) {
    private val state = context.getSharedPreferences("mouchen_calendar_state", Context.MODE_PRIVATE)

    suspend fun collect() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) != PackageManager.PERMISSION_GRANTED) return
        val now = System.currentTimeMillis()
        val end = now + 14L * 24 * 60 * 60 * 1000
        val builder = CalendarContract.Instances.CONTENT_URI.buildUpon()
        ContentUris.appendId(builder, now)
        ContentUris.appendId(builder, end)
        val projection = arrayOf(
            CalendarContract.Instances.EVENT_ID,
            CalendarContract.Instances.TITLE,
            CalendarContract.Instances.BEGIN,
            CalendarContract.Instances.END,
            CalendarContract.Instances.ALL_DAY,
            CalendarContract.Instances.DESCRIPTION,
            CalendarContract.Instances.EVENT_LOCATION,
            CalendarContract.Instances.ORGANIZER,
            CalendarContract.Events.RRULE,
        )
        context.contentResolver.query(builder.build(), projection, null, null, CalendarContract.Instances.BEGIN)?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val title = cursor.getString(1).orEmpty().take(500)
                val begin = cursor.getLong(2)
                val finish = cursor.getLong(3)
                val recurring = cursor.getString(8).orEmpty().isNotBlank()
                val previousBegin = state.getLong("begin.$id", Long.MIN_VALUE)
                val previousCount = state.getInt("reschedules.$id", 0)
                val rescheduleCount = if (!recurring && previousBegin != Long.MIN_VALUE && previousBegin != begin) {
                    previousCount + 1
                } else {
                    if (recurring) 0 else previousCount
                }
                if (!recurring) {
                    state.edit()
                        .putLong("begin.$id", begin)
                        .putInt("reschedules.$id", rescheduleCount)
                        .apply()
                }
                val event = LocalEventEntity(
                    id = "calendar-$id-$begin",
                    source = "android.calendar",
                    type = "calendar.scheduled",
                    occurredAt = begin,
                    payloadJson = JSONObject()
                        .put("event_id", id)
                        .put("title", title)
                        .put("begin", begin)
                        .put("end", finish)
                        .put("all_day", cursor.getInt(4) == 1)
                        .put("description", cursor.getString(5).orEmpty().take(4000))
                        .put("location", cursor.getString(6).orEmpty().take(1000))
                        .put("organizer", cursor.getString(7).orEmpty().take(500))
                        .put("reschedule_count", rescheduleCount)
                        .put("recurring", recurring)
                        .toString(),
                )
                if (dao.insertEventIfAbsent(event) != -1L) {
                    RealtimeSyncWorker.enqueueForEvent(context, event)
                }
            }
        }
    }
}
