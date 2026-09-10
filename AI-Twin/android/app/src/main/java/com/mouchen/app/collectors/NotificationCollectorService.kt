package com.mouchen.app.collectors

import android.app.Notification
import android.content.ComponentName
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.sync.effectiveCollectionConsent
import java.security.MessageDigest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

class NotificationCollectorService : NotificationListenerService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var eventWriter: BoundedEventWriter

    override fun onCreate() {
        super.onCreate()
        eventWriter = BoundedEventWriter(applicationContext, scope, EVENT_QUEUE_CAPACITY)
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!effectiveCollectionConsent(applicationContext)) return
        toEvent(sbn, backfill = false)?.let(eventWriter::offer)
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        if (!effectiveCollectionConsent(applicationContext)) return
        val current = runCatching { activeNotifications.orEmpty().toList() }.getOrDefault(emptyList())
        scope.launch {
            // Initial notification access exposes only notifications that are currently active.
            // Write serially and route every row by its own content. A generic batch trigger would
            // strand an already-active health/security/deadline notification behind old evidence.
            writeNotificationBackfill(
                events = current.mapNotNull { toEvent(it, backfill = true) },
                writeNow = eventWriter::writeNow,
            )
        }
    }

    private fun toEvent(sbn: StatusBarNotification, backfill: Boolean): LocalEventEntity? {
        if (sbn.packageName == packageName || sbn.isOngoing) return null
        val notification = sbn.notification
        val extras = notification.extras
        val payload = JSONObject()
            .put("package", sbn.packageName)
            .put("category", notification.category ?: "unknown")
            .put("post_time", sbn.postTime)
            .put("group_summary", notification.flags and Notification.FLAG_GROUP_SUMMARY != 0)
        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
            payload.put("title", extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.take(500))
            payload.put("text", extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.take(4000))
            payload.put("big_text", extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.take(8000))
            payload.put("sub_text", extras.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString()?.take(1000))
            payload.put("summary_text", extras.getCharSequence(Notification.EXTRA_SUMMARY_TEXT)?.toString()?.take(1000))
            extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
                ?.map { it.toString().take(2000) }
                ?.filter(String::isNotBlank)
                ?.take(20)
                ?.takeIf { it.isNotEmpty() }
                ?.let { payload.put("text_lines", JSONArray(it)) }
        }
        // Include the visible revision in the identity. Android can update a notification in
        // place while retaining its key and post time; those body updates must remain observable.
        val contentRevision = payload.toString()
        payload.put("current_notification_backfill", backfill)
        return LocalEventEntity(
            id = notificationEventId(sbn.packageName, sbn.key, sbn.postTime, contentRevision),
            source = "android.notification",
            type = "notification.posted",
            occurredAt = sbn.postTime,
            payloadJson = payload.toString(),
        )
    }

    override fun onListenerDisconnected() {
        requestRebind(ComponentName(this, NotificationCollectorService::class.java))
    }

    override fun onDestroy() {
        if (::eventWriter.isInitialized) eventWriter.close()
        scope.cancel()
        super.onDestroy()
    }

    private companion object {
        const val EVENT_QUEUE_CAPACITY = 64
    }
}

internal suspend fun writeNotificationBackfill(
    events: List<LocalEventEntity>,
    writeNow: suspend (LocalEventEntity, Boolean) -> Boolean,
): Int {
    var captured = 0
    events.forEach { event ->
        if (writeNow(event, true)) captured += 1
    }
    return captured
}

internal fun notificationEventId(
    packageName: String,
    notificationKey: String,
    postTime: Long,
    contentRevision: String,
): String {
    val digest = MessageDigest.getInstance("SHA-256")
        .digest("$packageName|$notificationKey|$postTime|$contentRevision".toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
    return "notification-$digest"
}
