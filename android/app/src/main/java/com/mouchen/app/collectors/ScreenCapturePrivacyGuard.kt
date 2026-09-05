package com.mouchen.app.collectors

import android.content.Context
import android.provider.Settings

/**
 * Coordinates the Accessibility protection signal with explicit MediaProjection capture.
 * A projection grant alone is insufficient: every frame also needs an enabled Accessibility
 * service and a recent, non-sensitive foreground-package observation.
 */
internal class ScreenCapturePrivacyGuard(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = appContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun markPasswordFieldSeen(now: Long = System.currentTimeMillis()) {
        synchronized(SIGNAL_LOCK) {
            val revision = nextRevision(preferences.getLong(KEY_PASSWORD_REVISION, 0L))
            preferences.edit()
                .putLong(KEY_PASSWORD_SEEN_AT, now)
                .putLong(KEY_PASSWORD_REVISION, revision)
                .apply()
        }
    }

    fun markForegroundPackageSeen(packageName: String, now: Long = System.currentTimeMillis()) {
        val normalized = packageName.trim().lowercase().take(MAX_PACKAGE_NAME_LENGTH)
        if (normalized.isEmpty()) return
        synchronized(SIGNAL_LOCK) {
            val previous = preferences.getString(KEY_FOREGROUND_PACKAGE, null)
            val editor = preferences.edit()
                .putString(KEY_FOREGROUND_PACKAGE, normalized)
                .putLong(KEY_FOREGROUND_SEEN_AT, now)
            if (previous != normalized) {
                editor.putLong(
                    KEY_FOREGROUND_REVISION,
                    nextRevision(preferences.getLong(KEY_FOREGROUND_REVISION, 0L)),
                )
            }
            editor.apply()
        }
    }

    fun clearForegroundPackageSignal() {
        synchronized(SIGNAL_LOCK) {
            preferences.edit()
                .remove(KEY_FOREGROUND_PACKAGE)
                .remove(KEY_FOREGROUND_SEEN_AT)
                .putLong(
                    KEY_FOREGROUND_REVISION,
                    nextRevision(preferences.getLong(KEY_FOREGROUND_REVISION, 0L)),
                )
                .apply()
        }
    }

    fun safeCaptureContext(now: Long = System.currentTimeMillis()): ScreenCaptureContext? {
        val signals = signalSnapshot()
        return screenCaptureContext(
            accessibilityProtectionEnabled = accessibilityProtectionEnabled(),
            foregroundPackage = signals.foregroundPackage,
            foregroundSeenAt = signals.foregroundSeenAt,
            passwordSeenAt = signals.passwordSeenAt,
            now = now,
            ownPackage = appContext.packageName,
            foregroundRevision = signals.foregroundRevision,
            passwordRevision = signals.passwordRevision,
        )
    }

    private fun accessibilityProtectionEnabled(): Boolean {
        val enabledServices = runCatching {
            Settings.Secure.getString(
                appContext.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            )
        }.getOrNull()
        val accessibilityMasterEnabled = runCatching {
            Settings.Secure.getInt(
                appContext.contentResolver,
                Settings.Secure.ACCESSIBILITY_ENABLED,
                0,
            ) == 1
        }.getOrDefault(false)
        val accessibilityEnabled = accessibilityMasterEnabled &&
            isAccessibilityServiceListed(
                enabledServices = enabledServices,
                expectedPackage = appContext.packageName,
                expectedClass = MouchenAccessibilityService::class.java.name,
            )
        return accessibilityEnabled
    }

    fun shouldExcludeScreen(now: Long = System.currentTimeMillis()): Boolean =
        safeCaptureContext(now) == null

    /**
     * Validates only signals observed after an already-safe frame was captured.
     *
     * OCR completion can happen after the normal foreground recency window expires. Requiring a
     * still-recent signal would reject a static, unchanged screen. A newly observed password field
     * or a newly observed different foreground package still invalidates the pending result.
     */
    fun completionStillSafe(captured: ScreenCaptureContext): Boolean {
        val signals = signalSnapshot()
        return isScreenOcrCompletionSafe(
            captured = captured,
            accessibilityProtectionEnabled = accessibilityProtectionEnabled(),
            currentForegroundPackage = signals.foregroundPackage,
            currentForegroundSeenAt = signals.foregroundSeenAt,
            currentPasswordSeenAt = signals.passwordSeenAt,
            currentForegroundRevision = signals.foregroundRevision,
            currentPasswordRevision = signals.passwordRevision,
        )
    }

    private fun signalSnapshot(): ScreenCaptureSignalSnapshot = synchronized(SIGNAL_LOCK) {
        ScreenCaptureSignalSnapshot(
            foregroundPackage = preferences.getString(KEY_FOREGROUND_PACKAGE, null),
            foregroundSeenAt = preferences.getLong(KEY_FOREGROUND_SEEN_AT, Long.MIN_VALUE),
            passwordSeenAt = preferences.getLong(KEY_PASSWORD_SEEN_AT, Long.MIN_VALUE),
            foregroundRevision = preferences.getLong(KEY_FOREGROUND_REVISION, 0L),
            passwordRevision = preferences.getLong(KEY_PASSWORD_REVISION, 0L),
        )
    }

    private companion object {
        const val PREFERENCES = "screen_capture_privacy"
        const val KEY_PASSWORD_SEEN_AT = "password_seen_at"
        const val KEY_PASSWORD_REVISION = "password_revision"
        const val KEY_FOREGROUND_PACKAGE = "foreground_package"
        const val KEY_FOREGROUND_SEEN_AT = "foreground_seen_at"
        const val KEY_FOREGROUND_REVISION = "foreground_revision"
        const val MAX_PACKAGE_NAME_LENGTH = 255
        val SIGNAL_LOCK = Any()
    }
}

private data class ScreenCaptureSignalSnapshot(
    val foregroundPackage: String?,
    val foregroundSeenAt: Long,
    val passwordSeenAt: Long,
    val foregroundRevision: Long,
    val passwordRevision: Long,
)

private fun nextRevision(current: Long): Long =
    if (current == Long.MAX_VALUE) Long.MIN_VALUE else current + 1L

internal data class ScreenCaptureContext(
    val foregroundPackage: String,
    val foregroundSeenAt: Long,
    val passwordSeenAt: Long,
    val foregroundRevision: Long = 0L,
    val passwordRevision: Long = 0L,
)

internal fun screenCaptureContext(
    accessibilityProtectionEnabled: Boolean,
    foregroundPackage: String?,
    foregroundSeenAt: Long,
    passwordSeenAt: Long,
    now: Long,
    ownPackage: String,
    foregroundSignalWindowMs: Long = 10_000L,
    passwordExclusionWindowMs: Long = 30_000L,
    foregroundRevision: Long = 0L,
    passwordRevision: Long = 0L,
): ScreenCaptureContext? {
    if (!accessibilityProtectionEnabled) return null
    val normalizedPackage = foregroundPackage?.trim()?.lowercase().orEmpty()
    if (!isRecentForegroundPackage(foregroundSeenAt, now, foregroundSignalWindowMs)) return null
    if (isSensitiveCapturePackage(normalizedPackage, ownPackage)) return null
    if (isRecentPasswordScreen(passwordSeenAt, now, passwordExclusionWindowMs)) return null
    return ScreenCaptureContext(
        foregroundPackage = normalizedPackage,
        foregroundSeenAt = foregroundSeenAt,
        passwordSeenAt = passwordSeenAt,
        foregroundRevision = foregroundRevision,
        passwordRevision = passwordRevision,
    )
}

internal fun isScreenOcrCompletionSafe(
    captured: ScreenCaptureContext,
    accessibilityProtectionEnabled: Boolean,
    currentForegroundPackage: String?,
    currentForegroundSeenAt: Long,
    currentPasswordSeenAt: Long,
    currentForegroundRevision: Long,
    currentPasswordRevision: Long,
): Boolean {
    if (!accessibilityProtectionEnabled) return false
    val normalizedCurrentPackage = currentForegroundPackage?.trim()?.lowercase().orEmpty()
    if (normalizedCurrentPackage.isEmpty()) return false
    if (normalizedCurrentPackage != captured.foregroundPackage) return false
    if (currentForegroundSeenAt == Long.MIN_VALUE) return false
    if (currentForegroundSeenAt < captured.foregroundSeenAt) return false
    if (currentForegroundRevision != captured.foregroundRevision) return false
    if (currentPasswordRevision != captured.passwordRevision) return false
    if (currentPasswordSeenAt != captured.passwordSeenAt) return false
    return true
}

internal fun isRecentForegroundPackage(
    foregroundSeenAt: Long,
    now: Long,
    recencyWindowMs: Long = 10_000L,
): Boolean {
    if (recencyWindowMs < 0 || foregroundSeenAt == Long.MIN_VALUE || now < foregroundSeenAt) return false
    return now - foregroundSeenAt <= recencyWindowMs
}

internal fun isRecentPasswordScreen(
    passwordSeenAt: Long,
    now: Long,
    exclusionWindowMs: Long = 30_000L,
): Boolean {
    if (exclusionWindowMs < 0 || passwordSeenAt == Long.MIN_VALUE || now < passwordSeenAt) return false
    return now - passwordSeenAt <= exclusionWindowMs
}

internal fun isScreenCaptureGuardCheckDue(
    now: Long,
    lastCaptureAt: Long,
    lastGuardCheckAt: Long,
    sampleIntervalMs: Long,
    guardCheckIntervalMs: Long,
): Boolean {
    if (sampleIntervalMs < 0 || guardCheckIntervalMs < 0) return false
    if (now < lastCaptureAt || now < lastGuardCheckAt) return false
    return now - lastCaptureAt >= sampleIntervalMs &&
        now - lastGuardCheckAt >= guardCheckIntervalMs
}

internal fun isAccessibilityServiceListed(
    enabledServices: String?,
    expectedPackage: String,
    expectedClass: String,
): Boolean {
    val normalizedExpectedPackage = expectedPackage.trim().lowercase()
    val normalizedExpectedClass = expectedClass.trim().lowercase()
    if (normalizedExpectedPackage.isEmpty() || normalizedExpectedClass.isEmpty()) return false
    return enabledServices.orEmpty().split(':').any { flattened ->
        val separator = flattened.indexOf('/')
        if (separator <= 0 || separator == flattened.lastIndex) return@any false
        val packageName = flattened.substring(0, separator).trim().lowercase()
        val rawClass = flattened.substring(separator + 1).trim().lowercase()
        val className = if (rawClass.startsWith('.')) "$packageName$rawClass" else rawClass
        packageName == normalizedExpectedPackage && className == normalizedExpectedClass
    }
}

/** Conservative package gate; false positives only suppress a screen sample, never stop the app. */
internal fun isSensitiveCapturePackage(packageName: String?, ownPackage: String = ""): Boolean {
    val value = packageName?.trim()?.lowercase().orEmpty()
    if (value.isEmpty()) return true
    if (ownPackage.isNotBlank() && value == ownPackage.trim().lowercase()) return true
    if (value == "com.android.systemui") return true
    return SENSITIVE_PACKAGE_MARKERS.any(value::contains)
}

private val SENSITIVE_PACKAGE_MARKERS = listOf(
    "bank",
    "wallet",
    "authenticator",
    "password",
    "passwd",
    "passmanager",
    "bitwarden",
    "1password",
    "onepassword",
    "keepass",
    "lastpass",
    "dashlane",
    "authy",
    "freeotp",
    "andotp",
    "twofas",
    "aegis",
    ".otp",
    "otp.",
    "alipay",
    "paypal",
    "unionpay",
    "revolut",
    "venmo",
    "cashapp",
    "monzo",
    "icbc",
    "cmbchina",
    "abchina",
    "bankcomm",
    "chinamworld",
    "bocsoft",
)
