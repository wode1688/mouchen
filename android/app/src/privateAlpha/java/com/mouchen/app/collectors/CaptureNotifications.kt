package com.mouchen.app.collectors

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.mouchen.app.R
import com.mouchen.app.localization.accountText

internal object CaptureNotifications {
    const val CHANNEL_ID = "mouchen_visible_capture"

    fun createChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                context.accountText("Local sensing service", "本地感知服务"),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = context.accountText(
                    "Shows user-authorized audio or screen collection",
                    "显示正在由用户授权运行的录音或屏幕采集",
                )
                setShowBadge(false)
            },
        )
    }

    fun <T : Service> ongoing(
        service: T,
        serviceClass: Class<T>,
        stopActionName: String,
        title: String,
        text: String,
        notificationId: Int,
    ): Notification {
        val stopIntent = Intent(service, serviceClass).setAction(stopActionName)
        val stopAction = PendingIntent.getService(
            service,
            notificationId,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(service, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_mouchen)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .addAction(0, service.accountText("Stop", "停止"), stopAction)
            .build()
    }
}
