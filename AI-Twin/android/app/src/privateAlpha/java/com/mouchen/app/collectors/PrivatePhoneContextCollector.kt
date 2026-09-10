package com.mouchen.app.collectors

import android.Manifest
import android.content.ContentResolver
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.Uri
import android.os.Bundle
import android.provider.CallLog
import android.provider.ContactsContract
import android.provider.Telephony
import androidx.core.content.ContextCompat
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.security.KeyVault
import org.json.JSONObject

/**
 * Bounded private-alpha collectors for owner-authorized phone context.
 *
 * These collectors run only during an existing WorkManager scan. They do not register observers,
 * services, or continuous listeners. Raw phone numbers and email addresses are never persisted.
 */
internal class ContactContextCollector(
    private val context: Context,
    private val dao: MouchenDao,
) {
    private val state = PhoneContextWatermarks(context)
    private val secret by lazy { KeyVault(context).databasePassphrase() }

    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int {
        if (!sensitiveCollectionAllowed(context, Manifest.permission.READ_CONTACTS)) {
            dao.deleteEventsByType(CONTACT_EVENT_TYPE)
            state.clear(CONTACTS_WATERMARK)
            return 0
        }
        val cursorState = state.load(CONTACTS_WATERMARK, nowMs)
        val projection = arrayOf(
            ContactsContract.Data._ID,
            ContactsContract.Data.CONTACT_ID,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
            ContactsContract.Data.MIMETYPE,
            ContactsContract.Data.DATA1,
            ContactsContract.Contacts.CONTACT_LAST_UPDATED_TIMESTAMP,
        )
        val timestampColumn = ContactsContract.Contacts.CONTACT_LAST_UPDATED_TIMESTAMP
        val idColumn = ContactsContract.Data._ID
        val selection = "${ContactsContract.Data.MIMETYPE} IN (?, ?) AND " +
            timelineSelection(timestampColumn, idColumn)
        val selectionArgs = arrayOf(
            ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE,
            ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE,
            cursorState.timestampMs.toString(),
            cursorState.timestampMs.toString(),
            cursorState.rowId.toString(),
            nowMs.toString(),
        )
        val cursor = boundedQuery(
            context.contentResolver,
            ContactsContract.Data.CONTENT_URI,
            projection,
            selection,
            selectionArgs,
            "$timestampColumn ASC, $idColumn ASC",
        ) ?: return 0

        var seen = 0
        var inserted = 0
        var last = cursorState
        cursor.use {
            while (it.moveToNext() && seen < PHONE_CONTEXT_PAGE_SIZE) {
                seen += 1
                val rowId = it.getLong(0)
                val contactId = it.getLong(1)
                val displayName = it.getString(2).orEmpty().trim().take(MAX_DISPLAY_NAME_CHARS)
                val mimeType = it.getString(3).orEmpty()
                val identifier = it.getString(4).orEmpty()
                val updatedAt = it.getLong(5).coerceAtLeast(0L)
                last = TimelineCursor(updatedAt, rowId)

                val kind = if (mimeType == ContactsContract.CommonDataKinds.Email.CONTENT_ITEM_TYPE) {
                    ParticipantKind.EMAIL
                } else {
                    ParticipantKind.PHONE
                }
                val identifierHash = phoneContextIdentifierHash(secret, kind, identifier)
                if (identifierHash.isEmpty()) continue
                val contactRef = keyedDigestHex(secret, "contact:$contactId")
                val eventId = stablePhoneContextEventId("contact", secret, rowId, updatedAt)
                val result = dao.insertEventIfAbsent(
                    LocalEventEntity(
                        id = eventId,
                        source = "android.contacts",
                        type = CONTACT_EVENT_TYPE,
                        occurredAt = updatedAt.takeIf { value -> value > 0L } ?: nowMs,
                        sensitivity = "sensitive",
                        payloadJson = JSONObject()
                            .put("contact_ref", contactRef)
                            .put("display_name", displayName)
                            .put("identifier_kind", kind.wireName)
                            .put("identifier_hash", identifierHash)
                            .put("last_updated_ms", updatedAt)
                            .toString(),
                    ),
                )
                if (result != -1L) inserted += 1
            }
        }
        state.advance(CONTACTS_WATERMARK, nowMs, seen, last)
        return inserted
    }
}

internal class SmsContextCollector(
    private val context: Context,
    private val dao: MouchenDao,
) {
    private val state = PhoneContextWatermarks(context)
    private val secret by lazy { KeyVault(context).databasePassphrase() }

    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int {
        if (!sensitiveCollectionAllowed(context, Manifest.permission.READ_SMS)) {
            dao.deleteEventsByType(SMS_EVENT_TYPE)
            state.clear(SMS_WATERMARK)
            return 0
        }
        val cursorState = state.load(SMS_WATERMARK, nowMs)
        val projection = arrayOf(
            Telephony.Sms._ID,
            Telephony.Sms.ADDRESS,
            Telephony.Sms.BODY,
            Telephony.Sms.DATE,
            Telephony.Sms.DATE_SENT,
            Telephony.Sms.TYPE,
        )
        val selection = timelineSelection(Telephony.Sms.DATE, Telephony.Sms._ID)
        val selectionArgs = timelineSelectionArgs(cursorState, nowMs)
        val cursor = boundedQuery(
            context.contentResolver,
            Telephony.Sms.CONTENT_URI,
            projection,
            selection,
            selectionArgs,
            "${Telephony.Sms.DATE} ASC, ${Telephony.Sms._ID} ASC",
        ) ?: return 0
        val names = ContactDisplayLookup(context)

        var seen = 0
        var inserted = 0
        var last = cursorState
        cursor.use {
            while (it.moveToNext() && seen < PHONE_CONTEXT_PAGE_SIZE) {
                seen += 1
                val rowId = it.getLong(0)
                val address = it.getString(1).orEmpty()
                val body = it.getString(2).orEmpty()
                val recordedAt = it.getLong(3).coerceAtLeast(0L)
                val providerSentAt = it.getLong(4).coerceAtLeast(0L)
                val direction = smsDirection(it.getInt(5))
                last = TimelineCursor(recordedAt, rowId)

                val participant = phoneContextParticipant(secret, address)
                val display = names.displayName(address, participant.kind)
                    ?: participant.masked
                val payload = JSONObject()
                    .put("direction", direction)
                    .put("party_ref", participant.identifierHash)
                    .put("party_display", display.take(MAX_DISPLAY_NAME_CHARS))
                    .put("recorded_at_ms", recordedAt)
                    .put("body", body.take(MAX_SMS_BODY_CHARS))
                    .put("body_truncated", body.length > MAX_SMS_BODY_CHARS)
                if (direction == "received") {
                    payload.put("received_at_ms", recordedAt)
                    if (providerSentAt > 0L) payload.put("sent_at_ms", providerSentAt)
                } else {
                    payload.put("sent_at_ms", providerSentAt.takeIf { value -> value > 0L } ?: recordedAt)
                }
                val result = dao.insertEventIfAbsent(
                    LocalEventEntity(
                        id = stablePhoneContextEventId("sms", secret, rowId, recordedAt),
                        source = "android.sms",
                        type = SMS_EVENT_TYPE,
                        occurredAt = recordedAt.takeIf { value -> value > 0L } ?: nowMs,
                        sensitivity = "sensitive",
                        payloadJson = payload.toString(),
                    ),
                )
                if (result != -1L) inserted += 1
            }
        }
        state.advance(SMS_WATERMARK, nowMs, seen, last)
        return inserted
    }
}

internal class CallLogContextCollector(
    private val context: Context,
    private val dao: MouchenDao,
) {
    private val state = PhoneContextWatermarks(context)
    private val secret by lazy { KeyVault(context).databasePassphrase() }

    suspend fun collect(nowMs: Long = System.currentTimeMillis()): Int {
        if (!sensitiveCollectionAllowed(context, Manifest.permission.READ_CALL_LOG)) {
            dao.deleteEventsByType(CALL_EVENT_TYPE)
            state.clear(CALL_LOG_WATERMARK)
            return 0
        }
        val cursorState = state.load(CALL_LOG_WATERMARK, nowMs)
        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
            CallLog.Calls.TYPE,
        )
        val selection = timelineSelection(CallLog.Calls.DATE, CallLog.Calls._ID)
        val selectionArgs = timelineSelectionArgs(cursorState, nowMs)
        val cursor = boundedQuery(
            context.contentResolver,
            CallLog.Calls.CONTENT_URI,
            projection,
            selection,
            selectionArgs,
            "${CallLog.Calls.DATE} ASC, ${CallLog.Calls._ID} ASC",
        ) ?: return 0
        val names = ContactDisplayLookup(context)

        var seen = 0
        var inserted = 0
        var last = cursorState
        cursor.use {
            while (it.moveToNext() && seen < PHONE_CONTEXT_PAGE_SIZE) {
                seen += 1
                val rowId = it.getLong(0)
                val address = it.getString(1).orEmpty()
                val cachedName = it.getString(2).orEmpty().trim()
                val startedAt = it.getLong(3).coerceAtLeast(0L)
                val durationSeconds = it.getLong(4).coerceAtLeast(0L)
                val direction = callDirection(it.getInt(5))
                last = TimelineCursor(startedAt, rowId)

                val participant = phoneContextParticipant(secret, address)
                val display = cachedName.takeIf(String::isNotBlank)
                    ?: names.displayName(address, participant.kind)
                    ?: participant.masked
                val result = dao.insertEventIfAbsent(
                    LocalEventEntity(
                        id = stablePhoneContextEventId("call", secret, rowId, startedAt),
                        source = "android.call_log",
                        type = CALL_EVENT_TYPE,
                        occurredAt = startedAt.takeIf { value -> value > 0L } ?: nowMs,
                        sensitivity = "sensitive",
                        payloadJson = JSONObject()
                            .put("direction", direction)
                            .put("party_ref", participant.identifierHash)
                            .put("party_display", display.take(MAX_DISPLAY_NAME_CHARS))
                            .put("started_at_ms", startedAt)
                            .put("duration_seconds", durationSeconds)
                            .toString(),
                    ),
                )
                if (result != -1L) inserted += 1
            }
        }
        state.advance(CALL_LOG_WATERMARK, nowMs, seen, last)
        return inserted
    }
}

private class PhoneContextWatermarks(context: Context) {
    private val preferences = context.getSharedPreferences(PHONE_CONTEXT_STATE, Context.MODE_PRIVATE)

    fun load(key: String, nowMs: Long): TimelineCursor {
        if (!preferences.contains("$key.timestamp")) return initialTimelineCursor(nowMs)
        return clampTimelineCursor(
            TimelineCursor(
                timestampMs = preferences.getLong("$key.timestamp", 0L),
                rowId = preferences.getLong("$key.row_id", -1L),
            ),
            nowMs,
        )
    }

    fun advance(key: String, nowMs: Long, seenRows: Int, last: TimelineCursor) {
        val next = nextTimelineCursor(nowMs, seenRows, last)
        preferences.edit()
            .putLong("$key.timestamp", next.timestampMs)
            .putLong("$key.row_id", next.rowId)
            .apply()
    }

    fun clear(key: String) {
        preferences.edit()
            .remove("$key.timestamp")
            .remove("$key.row_id")
            .apply()
    }
}

private class ContactDisplayLookup(private val context: Context) {
    fun displayName(address: String, kind: ParticipantKind): String? {
        if (!sensitiveCollectionAllowed(context, Manifest.permission.READ_CONTACTS)) return null
        val trimmed = address.trim()
        if (trimmed.isEmpty()) return null
        return runCatching {
            when (kind) {
                ParticipantKind.PHONE -> queryPhone(trimmed)
                ParticipantKind.EMAIL -> queryEmail(trimmed)
                ParticipantKind.OTHER -> null
            }
        }.getOrNull()?.trim()?.takeIf(String::isNotEmpty)?.take(MAX_DISPLAY_NAME_CHARS)
    }

    private fun queryPhone(address: String): String? {
        val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(address))
        return context.contentResolver.query(
            uri,
            arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME),
            null,
            null,
            null,
        )?.use { cursor -> if (cursor.moveToFirst()) cursor.getString(0) else null }
    }

    private fun queryEmail(address: String): String? = context.contentResolver.query(
        ContactsContract.CommonDataKinds.Email.CONTENT_URI,
        arrayOf(ContactsContract.Contacts.DISPLAY_NAME_PRIMARY),
        "${ContactsContract.CommonDataKinds.Email.ADDRESS} = ?",
        arrayOf(address),
        null,
    )?.use { cursor -> if (cursor.moveToFirst()) cursor.getString(0) else null }
}

private fun sensitiveCollectionAllowed(context: Context, permission: String): Boolean =
    BuildConfig.ALLOW_SENSITIVE_CAPTURE &&
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

private fun timelineSelection(timestampColumn: String, idColumn: String): String =
    "(($timestampColumn > ?) OR ($timestampColumn = ? AND $idColumn > ?)) AND $timestampColumn <= ?"

private fun timelineSelectionArgs(cursor: TimelineCursor, nowMs: Long): Array<String> = arrayOf(
    cursor.timestampMs.toString(),
    cursor.timestampMs.toString(),
    cursor.rowId.toString(),
    nowMs.toString(),
)

private fun boundedQuery(
    resolver: ContentResolver,
    uri: Uri,
    projection: Array<String>,
    selection: String,
    selectionArgs: Array<String>,
    sortOrder: String,
): Cursor? {
    val queryArgs = Bundle().apply {
        putString(ContentResolver.QUERY_ARG_SQL_SELECTION, selection)
        putStringArray(ContentResolver.QUERY_ARG_SQL_SELECTION_ARGS, selectionArgs)
        putString(ContentResolver.QUERY_ARG_SQL_SORT_ORDER, sortOrder)
        putInt(ContentResolver.QUERY_ARG_LIMIT, PHONE_CONTEXT_PAGE_SIZE)
    }
    return try {
        resolver.query(uri, projection, queryArgs, null)
    } catch (_: IllegalArgumentException) {
        // A few OEM providers ignore structured query limits. Processing still stops at 500 rows.
        resolver.query(uri, projection, selection, selectionArgs, sortOrder)
    }
}

private const val PHONE_CONTEXT_STATE = "private_phone_context_watermarks"
private const val CONTACTS_WATERMARK = "contacts"
private const val SMS_WATERMARK = "sms"
private const val CALL_LOG_WATERMARK = "call_log"
private const val CONTACT_EVENT_TYPE = "contact.identifier"
private const val SMS_EVENT_TYPE = "message.sms"
private const val CALL_EVENT_TYPE = "call.observed"
private const val MAX_DISPLAY_NAME_CHARS = 300
private const val MAX_SMS_BODY_CHARS = 100_000
