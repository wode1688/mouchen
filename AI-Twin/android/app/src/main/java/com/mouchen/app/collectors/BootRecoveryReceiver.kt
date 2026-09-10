package com.mouchen.app.collectors

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.service.notification.NotificationListenerService
import com.mouchen.app.sync.AdviceAttentionScheduler
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.SessionHealthWorker
import com.mouchen.app.sync.UrgentEventSyncWorker
import com.mouchen.app.sync.legacyMigrationAllowsAuthenticatedWork

class BootRecoveryReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val trigger = recoveryTrigger(intent?.action) ?: return
        if (!legacyMigrationAllowsAuthenticatedWork(context.applicationContext)) return
        CollectionScheduler.ensurePeriodic(context)
        SessionHealthWorker.enqueueNow(context, replace = true)
        AdviceAttentionScheduler.enqueueNow(context)
        RealtimeSyncWorker.recover(context, trigger)
        UrgentEventSyncWorker.recover(context)
        CollectionWorker.enqueueStartup(context, trigger)
        runCatching {
            NotificationListenerService.requestRebind(
                ComponentName(context, NotificationCollectorService::class.java),
            )
        }
    }
}

internal fun recoveryTrigger(action: String?): String? = when (action) {
    Intent.ACTION_BOOT_COMPLETED -> "boot_completed"
    Intent.ACTION_MY_PACKAGE_REPLACED -> "package_replaced"
    Intent.ACTION_USER_UNLOCKED -> "user_unlocked"
    Intent.ACTION_USER_PRESENT -> "user_present"
    else -> null
}
