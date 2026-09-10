package com.mouchen.app

import android.content.Context
import android.content.Intent

internal const val AUDIO_CAPTURE_SERVICE_CLASS = "com.mouchen.app.collectors.AudioCaptureService"
internal const val SCREEN_CAPTURE_SERVICE_CLASS = "com.mouchen.app.collectors.ScreenCaptureService"
internal const val ACTION_CAPTURE_STATUS_CHANGED = "com.mouchen.app.action.CAPTURE_STATUS_CHANGED"

internal enum class CaptureKind {
    AUDIO,
    SCREEN,
}

internal data class CaptureStatusSnapshot(
    val audioRunning: Boolean = false,
    val screenRunning: Boolean = false,
    val screenAuthorizationRequired: Boolean = false,
    val screenStatusDetail: String = "",
)

/**
 * A service is shown as running only after it published a successful capture start and Android
 * still reports that service as alive. The second check removes stale state after process death.
 */
internal fun reconcileCaptureStatus(
    persisted: CaptureStatusSnapshot,
    runningServiceClassNames: Set<String>,
): CaptureStatusSnapshot {
    val screenStillAlive = SCREEN_CAPTURE_SERVICE_CLASS in runningServiceClassNames
    val screenWasLost = persisted.screenRunning && !screenStillAlive
    return CaptureStatusSnapshot(
        audioRunning = persisted.audioRunning && AUDIO_CAPTURE_SERVICE_CLASS in runningServiceClassNames,
        screenRunning = persisted.screenRunning && screenStillAlive,
        screenAuthorizationRequired = persisted.screenAuthorizationRequired || screenWasLost,
        screenStatusDetail = when {
            screenWasLost -> "capture_session_ended"
            else -> persisted.screenStatusDetail
        },
    )
}

internal class CaptureStatusStore(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = appContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun snapshot(): CaptureStatusSnapshot = CaptureStatusSnapshot(
        audioRunning = preferences.getBoolean(KEY_AUDIO_RUNNING, false),
        screenRunning = preferences.getBoolean(KEY_SCREEN_RUNNING, false),
        screenAuthorizationRequired = preferences.getBoolean(KEY_SCREEN_AUTH_REQUIRED, false),
        screenStatusDetail = preferences.getString(KEY_SCREEN_STATUS_DETAIL, null).orEmpty(),
    )

    fun setRunning(kind: CaptureKind, running: Boolean) {
        val key = when (kind) {
            CaptureKind.AUDIO -> KEY_AUDIO_RUNNING
            CaptureKind.SCREEN -> KEY_SCREEN_RUNNING
        }
        if (preferences.getBoolean(key, false) == running) return
        preferences.edit().putBoolean(key, running).apply()
        appContext.sendBroadcast(
            Intent(ACTION_CAPTURE_STATUS_CHANGED).setPackage(appContext.packageName),
        )
    }

    fun markScreenAuthorizationRequired(reason: String) {
        preferences.edit()
            .putBoolean(KEY_SCREEN_RUNNING, false)
            .putBoolean(KEY_SCREEN_AUTH_REQUIRED, true)
            .putString(KEY_SCREEN_STATUS_DETAIL, reason.take(120))
            .apply()
        notifyChanged()
    }

    fun clearScreenAuthorizationWarning() {
        preferences.edit()
            .putBoolean(KEY_SCREEN_AUTH_REQUIRED, false)
            .remove(KEY_SCREEN_STATUS_DETAIL)
            .apply()
        notifyChanged()
    }

    fun reconcile(runningServiceClassNames: Set<String>): CaptureStatusSnapshot {
        val current = snapshot()
        val reconciled = reconcileCaptureStatus(current, runningServiceClassNames)
        if (reconciled != current) {
            preferences.edit()
                .putBoolean(KEY_AUDIO_RUNNING, reconciled.audioRunning)
                .putBoolean(KEY_SCREEN_RUNNING, reconciled.screenRunning)
                .putBoolean(KEY_SCREEN_AUTH_REQUIRED, reconciled.screenAuthorizationRequired)
                .putString(KEY_SCREEN_STATUS_DETAIL, reconciled.screenStatusDetail)
                .apply()
        }
        return reconciled
    }

    private fun notifyChanged() {
        appContext.sendBroadcast(Intent(ACTION_CAPTURE_STATUS_CHANGED).setPackage(appContext.packageName))
    }

    private companion object {
        const val PREFERENCES = "capture_status"
        const val KEY_AUDIO_RUNNING = "audio_running"
        const val KEY_SCREEN_RUNNING = "screen_running"
        const val KEY_SCREEN_AUTH_REQUIRED = "screen_authorization_required"
        const val KEY_SCREEN_STATUS_DETAIL = "screen_status_detail"
    }
}
