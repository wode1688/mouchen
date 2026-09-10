package com.mouchen.app.actions

import android.Manifest
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import com.mouchen.app.data.ActionDraftEntity
import com.mouchen.app.data.MouchenDao
import org.json.JSONObject

data class CalendarEventDraft(
    val calendarId: Long,
    val title: String,
    val startAt: Long,
    val endAt: Long,
    val timeZone: String,
    val description: String? = null,
    val location: String? = null,
    val allDay: Boolean = false,
)

sealed interface ActionResult {
    data class Drafted(val draft: ActionDraftEntity) : ActionResult
    data class Confirmed(val id: String) : ActionResult
    data class Executed(val id: String, val eventUri: Uri) : ActionResult
    data class Reverted(val id: String) : ActionResult
    data class Rejected(val reason: String) : ActionResult
}

/** Executes only a user-confirmed, reversible calendar insertion. */
class CalendarActionCoordinator(
    private val context: Context,
    private val dao: MouchenDao,
) {
    suspend fun createDraft(request: CalendarEventDraft): ActionResult {
        validate(request)?.let { return ActionResult.Rejected(it) }
        val draft = ActionDraftEntity(
            actionType = ACTION_TYPE,
            payloadJson = request.toJson().toString(),
            status = STATUS_DRAFT,
        )
        dao.insertActionDraft(draft)
        return ActionResult.Drafted(draft)
    }

    suspend fun confirm(id: String): ActionResult {
        val changed = dao.confirmActionDraft(id, System.currentTimeMillis())
        return if (changed == 1) ActionResult.Confirmed(id) else ActionResult.Rejected("draft_not_confirmable")
    }

    suspend fun executeConfirmed(id: String): ActionResult {
        if (!hasCalendarPermissions()) return ActionResult.Rejected("calendar_permission_missing")
        val draft = dao.actionDraft(id) ?: return ActionResult.Rejected("draft_not_found")
        if (draft.actionType != ACTION_TYPE || draft.status != STATUS_CONFIRMED) {
            return ActionResult.Rejected("action_not_confirmed")
        }
        val request = runCatching { JSONObject(draft.payloadJson).toCalendarDraft() }
            .getOrElse { return ActionResult.Rejected("invalid_calendar_payload") }
        validate(request)?.let { return ActionResult.Rejected(it) }
        if (!calendarIsWritable(request.calendarId)) return ActionResult.Rejected("calendar_not_writable")
        if (dao.transitionActionDraft(id, STATUS_CONFIRMED, STATUS_EXECUTING) != 1) {
            return ActionResult.Rejected("action_already_claimed")
        }

        var insertedUri: Uri? = null
        return try {
            val eventUri = context.contentResolver.insert(CalendarContract.Events.CONTENT_URI, request.toValues(id))
                ?: error("calendar_insert_failed")
            insertedUri = eventUri
            val updatedPayload = request.toJson()
                .put("event_uri", eventUri.toString())
                .put("executed_by", "mouchen.confirmed_action")
            check(dao.finishActionDraft(id, STATUS_EXECUTED, updatedPayload.toString(), System.currentTimeMillis()) == 1)
            ActionResult.Executed(id, eventUri)
        } catch (_: Exception) {
            // If local bookkeeping fails, undo the provider write before returning control.
            insertedUri?.let { runCatching { context.contentResolver.delete(it, null, null) } }
            dao.transitionActionDraft(id, STATUS_EXECUTING, STATUS_CONFIRMED)
            ActionResult.Rejected("calendar_execution_failed")
        }
    }

    suspend fun undo(id: String): ActionResult {
        if (!hasCalendarPermissions()) return ActionResult.Rejected("calendar_permission_missing")
        val draft = dao.actionDraft(id) ?: return ActionResult.Rejected("draft_not_found")
        if (draft.actionType != ACTION_TYPE || draft.status != STATUS_EXECUTED) {
            return ActionResult.Rejected("action_not_revertible")
        }
        val eventUri = runCatching { Uri.parse(JSONObject(draft.payloadJson).getString("event_uri")) }.getOrNull()
            ?: return ActionResult.Rejected("event_reference_missing")
        if (eventUri.authority != CalendarContract.AUTHORITY || !eventUri.path.orEmpty().startsWith("/events/")) {
            return ActionResult.Rejected("event_reference_invalid")
        }
        runCatching { ContentUris.parseId(eventUri) }.getOrElse { return ActionResult.Rejected("event_reference_invalid") }
        if (dao.transitionActionDraft(id, STATUS_EXECUTED, STATUS_REVERTING) != 1) {
            return ActionResult.Rejected("action_already_claimed")
        }
        return try {
            context.contentResolver.delete(eventUri, null, null)
            dao.finishActionDraft(id, STATUS_REVERTED, draft.payloadJson, draft.executedAt)
            ActionResult.Reverted(id)
        } catch (_: Exception) {
            dao.transitionActionDraft(id, STATUS_REVERTING, STATUS_EXECUTED)
            ActionResult.Rejected("calendar_revert_failed")
        }
    }

    suspend fun cancelUnconfirmed(id: String): ActionResult {
        return if (dao.transitionActionDraft(id, STATUS_DRAFT, STATUS_CANCELLED) == 1) {
            ActionResult.Reverted(id)
        } else {
            ActionResult.Rejected("draft_not_cancellable")
        }
    }

    private fun hasCalendarPermissions(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.WRITE_CALENDAR) == PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED

    private fun calendarIsWritable(calendarId: Long): Boolean {
        val projection = arrayOf(CalendarContract.Calendars._ID, CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL)
        val selection = "${CalendarContract.Calendars._ID} = ?"
        return runCatching {
            context.contentResolver.query(
                CalendarContract.Calendars.CONTENT_URI,
                projection,
                selection,
                arrayOf(calendarId.toString()),
                null,
            )?.use { cursor ->
                cursor.moveToFirst() && cursor.getInt(1) >= CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR
            } ?: false
        }.getOrDefault(false)
    }

    private fun validate(request: CalendarEventDraft): String? = when {
        request.calendarId < 0 -> "invalid_calendar_id"
        request.title.isBlank() -> "title_required"
        request.title.length > 1000 -> "title_too_long"
        request.startAt <= 0 || request.endAt <= request.startAt -> "invalid_time_range"
        request.timeZone.isBlank() -> "timezone_required"
        else -> null
    }

    private fun CalendarEventDraft.toValues(actionId: String) = ContentValues().apply {
        put(CalendarContract.Events.CALENDAR_ID, calendarId)
        put(CalendarContract.Events.TITLE, title)
        put(CalendarContract.Events.DTSTART, startAt)
        put(CalendarContract.Events.DTEND, endAt)
        put(CalendarContract.Events.EVENT_TIMEZONE, if (allDay) "UTC" else timeZone)
        put(CalendarContract.Events.ALL_DAY, if (allDay) 1 else 0)
        description?.let { put(CalendarContract.Events.DESCRIPTION, "$it\n\nMy AI Twin action: $actionId") }
            ?: put(CalendarContract.Events.DESCRIPTION, "My AI Twin action: $actionId")
        location?.let { put(CalendarContract.Events.EVENT_LOCATION, it) }
    }

    private fun CalendarEventDraft.toJson() = JSONObject()
        .put("calendar_id", calendarId)
        .put("title", title)
        .put("start_at", startAt)
        .put("end_at", endAt)
        .put("time_zone", timeZone)
        .put("description", description ?: JSONObject.NULL)
        .put("location", location ?: JSONObject.NULL)
        .put("all_day", allDay)

    private fun JSONObject.toCalendarDraft() = CalendarEventDraft(
        calendarId = getLong("calendar_id"),
        title = getString("title"),
        startAt = getLong("start_at"),
        endAt = getLong("end_at"),
        timeZone = getString("time_zone"),
        description = if (isNull("description")) null else optString("description").takeIf(String::isNotBlank),
        location = if (isNull("location")) null else optString("location").takeIf(String::isNotBlank),
        allDay = optBoolean("all_day", false),
    )

    private companion object {
        const val ACTION_TYPE = "calendar.create"
        const val STATUS_DRAFT = "draft"
        const val STATUS_CONFIRMED = "confirmed"
        const val STATUS_EXECUTING = "executing"
        const val STATUS_EXECUTED = "executed"
        const val STATUS_REVERTING = "reverting"
        const val STATUS_REVERTED = "reverted"
        const val STATUS_CANCELLED = "cancelled"
    }
}
