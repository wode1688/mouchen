package com.mouchen.app.collectors

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Build
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import java.security.MessageDigest
import java.util.TimeZone
import org.json.JSONObject

class UsageStatsCollector(private val context: Context, private val dao: MouchenDao) {
    private val preferences = context.getSharedPreferences("collector_watermarks", Context.MODE_PRIVATE)

    suspend fun collect(): Int {
        // Do not advance the watermark before access is granted. The first authorized run must
        // still be able to backfill the full rolling seven-day window.
        if (!hasUsageStatsAccess(context)) return 0

        val end = System.currentTimeMillis()
        val watermark = if (preferences.contains(USAGE_WATERMARK)) {
            preferences.getLong(USAGE_WATERMARK, end)
        } else {
            null
        }
        val start = usageCollectionStart(end, watermark)
        val manager = context.getSystemService(UsageStatsManager::class.java)
        val packageManager = context.packageManager
        val stream = manager.queryEvents(start, end)
        val event = UsageEvents.Event()
        val foregroundStarts = loadOpenSessions(end)
        var inserted = 0
        while (stream.hasNextEvent()) {
            stream.getNextEvent(event)
            val packageName = event.packageName ?: continue
            when (event.eventType) {
                UsageEvents.Event.ACTIVITY_RESUMED -> foregroundStarts.putIfAbsent(packageName, event.timeStamp)
                UsageEvents.Event.ACTIVITY_PAUSED,
                UsageEvents.Event.ACTIVITY_STOPPED -> {
                    val startedAt = foregroundStarts.remove(packageName) ?: continue
                    val finishedAt = event.timeStamp.coerceAtLeast(startedAt)
                    val duration = finishedAt - startedAt
                    val rowId = dao.insertEventIfAbsent(
                        LocalEventEntity(
                            id = usageSessionId(packageName, startedAt, finishedAt),
                            source = "android.usage",
                            type = "app.foreground_session",
                            occurredAt = startedAt,
                            payloadJson = JSONObject()
                                .put("package", packageName)
                                .put(
                                    "app_label",
                                    runCatching {
                                        packageManager.getApplicationLabel(
                                            packageManager.getApplicationInfo(packageName, 0),
                                        ).toString().take(200)
                                    }.getOrDefault(packageName),
                                )
                                .put("duration_ms", duration)
                                .put(
                                    "timezone_offset_minutes",
                                    TimeZone.getDefault().getOffset(startedAt) / 60_000,
                                )
                                .toString(),
                        ),
                    )
                    if (rowId != -1L) inserted += 1
                }
            }
        }
        preferences.edit()
            .putLong(USAGE_WATERMARK, end)
            .putString(OPEN_SESSIONS, encodeOpenSessions(foregroundStarts))
            .apply()
        return inserted
    }

    private fun loadOpenSessions(end: Long): MutableMap<String, Long> = runCatching {
        val json = JSONObject(preferences.getString(OPEN_SESSIONS, "{}") ?: "{}")
        buildMap {
            json.keys().forEach { packageName ->
                val startedAt = json.optLong(packageName, -1L)
                if (shouldRetainOpenUsageSession(startedAt, end)) put(packageName, startedAt)
            }
        }.toMutableMap()
    }.getOrDefault(mutableMapOf())

    private fun encodeOpenSessions(sessions: Map<String, Long>): String = JSONObject().apply {
        sessions.forEach { (packageName, startedAt) -> put(packageName, startedAt) }
    }.toString()

    private companion object {
        const val USAGE_WATERMARK = "usage"
        const val OPEN_SESSIONS = "usage_open_sessions"
    }
}

internal fun usageCollectionStart(
    end: Long,
    watermark: Long?,
): Long {
    if (watermark != null) return watermark.coerceIn(end - MAX_USAGE_LOOKBACK_MS, end)
    return end - MAX_USAGE_LOOKBACK_MS
}

internal fun shouldRetainOpenUsageSession(startedAt: Long, end: Long): Boolean =
    startedAt in (end - MAX_USAGE_LOOKBACK_MS)..end

@Suppress("DEPRECATION")
internal fun hasUsageStatsAccess(context: Context): Boolean {
    val appOps = context.getSystemService(AppOpsManager::class.java)
    val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        appOps.unsafeCheckOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            context.applicationInfo.uid,
            context.packageName,
        )
    } else {
        appOps.checkOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            context.applicationInfo.uid,
            context.packageName,
        )
    }
    return mode == AppOpsManager.MODE_ALLOWED
}

internal fun usageSessionId(packageName: String, startedAt: Long, finishedAt: Long): String {
    val digest = MessageDigest.getInstance("SHA-256")
        .digest("$packageName|$startedAt|$finishedAt".toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
    return "usage-$digest"
}

private const val MAX_USAGE_LOOKBACK_MS = 7L * 24 * 60 * 60 * 1000
