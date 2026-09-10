package com.mouchen.app.sync

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.mouchen.app.MainActivity
import com.mouchen.app.R
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.localization.uiText
import org.json.JSONObject

class AdviceNotifier(private val context: Context) {
    /**
     * Claiming consumes one of the server's two global deliveries, so every channel that an
     * immediate advice could use must be available before the app asks for a claim.
     */
    internal fun canNotifyAdvice(): Boolean = listOf(
        CHANNEL_BRIEF,
        CHANNEL_ADVICE,
        CHANNEL_URGENT,
    ).all(::canPostTo)

    @SuppressLint("MissingPermission")
    fun notify(advice: AdviceEntity, deliveryNumber: Int = 1): Boolean {
        require(deliveryNumber in 1..2) { "Only two central deliveries are supported" }
        val channel = when (advice.level) {
            1 -> CHANNEL_BRIEF
            2 -> CHANNEL_ADVICE
            else -> CHANNEL_URGENT
        }
        if (!canPostTo(channel)) return false
        val priority = when (advice.level) {
            1 -> NotificationCompat.PRIORITY_LOW
            2 -> NotificationCompat.PRIORITY_DEFAULT
            else -> NotificationCompat.PRIORITY_HIGH
        }
        val openApp = PendingIntent.getActivity(
            context,
            adviceNotificationId(advice.id, deliveryNumber),
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_mouchen)
            .setContentTitle(
                if (deliveryNumber == 2) {
                    context.uiText("再次提醒 · L${advice.level} · ${advice.domain}")
                } else {
                    context.uiText("L${advice.level} · ${advice.domain}")
                },
            )
            .setContentText(advice.action)
            .setStyle(NotificationCompat.BigTextStyle().bigText("${advice.action}\n${context.uiText("第一步：${advice.firstStep}")}"))
            .setPriority(priority)
            .setContentIntent(openApp)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()
        return runCatching {
            NotificationManagerCompat.from(context).notify(
                adviceNotificationId(advice.id, deliveryNumber),
                notification,
            )
        }.isSuccess
    }

    @SuppressLint("MissingPermission")
    fun notifyFollowUp(advice: AdviceEntity): Boolean {
        if (!canPostTo(CHANNEL_ADVICE)) return false
        val prediction = runCatching {
            val value = JSONObject(advice.predictionJson)
            value.optString("adopted_expected_result").trim().ifEmpty {
                value.optString("outcome").trim()
            }
        }.getOrDefault("")
        val openApp = PendingIntent.getActivity(
            context,
            adviceFollowUpNotificationId(advice.id),
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val detail = buildString {
            append(context.uiText("你已采纳：") + advice.action)
            if (prediction.isNotBlank()) append("\n" + context.uiText("现在核验：") + prediction)
            append("\n" + context.uiText("打开AI替身记录结果，后续建言会据此校准。"))
        }
        val notification = NotificationCompat.Builder(context, CHANNEL_ADVICE)
            .setSmallIcon(R.drawable.ic_mouchen)
            .setContentTitle(context.uiText("到核验时点 · ${advice.domain}"))
            .setContentText(if (prediction.isNotBlank()) prediction else advice.action)
            .setStyle(NotificationCompat.BigTextStyle().bigText(detail))
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setContentIntent(openApp)
            .setAutoCancel(true)
            .build()
        return runCatching {
            NotificationManagerCompat.from(context).notify(adviceFollowUpNotificationId(advice.id), notification)
        }.isSuccess
    }

    /** Removes either visible delivery without changing the durable advice history. */
    internal fun dismissAdvice(adviceId: String) {
        val notifications = NotificationManagerCompat.from(context)
        notifications.cancel(adviceNotificationId(adviceId, 1))
        notifications.cancel(adviceNotificationId(adviceId, 2))
    }

    private fun canPostTo(channel: String): Boolean {
        if (!createChannels()) return false
        val postPermissionGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED
        val notifications = NotificationManagerCompat.from(context)
        val appNotificationsEnabled = runCatching { notifications.areNotificationsEnabled() }
            .getOrDefault(false)
        val channelImportance = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            runCatching {
                context.getSystemService(NotificationManager::class.java)
                    ?.getNotificationChannel(channel)
                    ?.importance
            }.getOrNull()
        } else {
            null
        }
        return notificationEligibility(
            sdkInt = Build.VERSION.SDK_INT,
            postPermissionGranted = postPermissionGranted,
            appNotificationsEnabled = appNotificationsEnabled,
            channelImportance = channelImportance,
        )
    }

    private fun createChannels(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return true
        return runCatching {
            val manager = context.getSystemService(NotificationManager::class.java)
                ?: return@runCatching false
            manager.createNotificationChannels(
                listOf(
                    NotificationChannel(CHANNEL_BRIEF, context.uiText("AI替身简报"), NotificationManager.IMPORTANCE_LOW),
                    NotificationChannel(CHANNEL_ADVICE, context.uiText("AI替身进言"), NotificationManager.IMPORTANCE_DEFAULT),
                    NotificationChannel(CHANNEL_URGENT, context.uiText("AI替身警告"), NotificationManager.IMPORTANCE_HIGH),
                ),
            )
            true
        }.getOrDefault(false)
    }

    private companion object {
        const val CHANNEL_BRIEF = "mouchen_brief"
        const val CHANNEL_ADVICE = "mouchen_advice"
        const val CHANNEL_URGENT = "mouchen_urgent"
    }
}

internal fun notificationEligibility(
    sdkInt: Int,
    postPermissionGranted: Boolean,
    appNotificationsEnabled: Boolean,
    channelImportance: Int?,
): Boolean {
    if (sdkInt >= Build.VERSION_CODES.TIRAMISU && !postPermissionGranted) return false
    if (!appNotificationsEnabled) return false
    if (sdkInt >= Build.VERSION_CODES.O &&
        (channelImportance == null || channelImportance <= NotificationManager.IMPORTANCE_NONE)
    ) return false
    return true
}

/** Separate positive ID ranges guarantee that a checkpoint never replaces its original advice. */
internal fun adviceNotificationId(adviceId: String, deliveryNumber: Int = 1): Int {
    require(deliveryNumber in 1..2)
    val base = adviceId.hashCode() and NOTIFICATION_ID_MASK
    return if (deliveryNumber == 2) base or SECOND_DELIVERY_NOTIFICATION_ID_BIT else base
}

internal fun adviceFollowUpNotificationId(adviceId: String): Int =
    (adviceId.hashCode() and NOTIFICATION_ID_MASK) or FOLLOW_UP_NOTIFICATION_ID_BIT

private const val NOTIFICATION_ID_MASK = 0x1FFF_FFFF
private const val SECOND_DELIVERY_NOTIFICATION_ID_BIT = 0x2000_0000
private const val FOLLOW_UP_NOTIFICATION_ID_BIT = 0x4000_0000
