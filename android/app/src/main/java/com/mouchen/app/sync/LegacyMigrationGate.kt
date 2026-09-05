package com.mouchen.app.sync

import android.content.Context

/**
 * Process-local acknowledgement for a restored private-alpha owner session.
 *
 * Nothing is persisted deliberately: choosing the compatibility path grants the old token access
 * only until this Android process dies. A later process must ask again, while a successful owner
 * claim replaces the legacy session with a normal commercial session and no longer needs a gate.
 */
internal object LegacyMigrationRuntimeGate {
    @Volatile
    private var allowedLegacySession: LegacySessionIdentity? = null

    fun allows(session: AuthSession): Boolean {
        if (!session.legacy) return true
        val identity = legacySessionIdentity(session) ?: return false
        return allowedLegacySession == identity
    }

    fun requiresDecision(session: AuthSession): Boolean = session.legacy && !allows(session)

    fun allowForThisProcess(session: AuthSession): Boolean {
        val identity = legacySessionIdentity(session) ?: return false
        allowedLegacySession = identity
        return true
    }

    fun reset() {
        allowedLegacySession = null
    }
}

internal data class LegacySessionIdentity(
    val epoch: Long,
    val userId: String,
    val origin: String,
    val deviceId: String,
    val sessionId: String,
)

internal fun legacySessionIdentity(session: AuthSession): LegacySessionIdentity? {
    if (!session.legacy || session.epoch <= 0L || session.userId.isBlank() || session.deviceId.isBlank()) return null
    val origin = canonicalHttpsOrigin(session.serverOrigin) ?: return null
    return LegacySessionIdentity(
        epoch = session.epoch,
        userId = session.userId,
        origin = origin,
        deviceId = session.deviceId,
        sessionId = session.sessionId,
    )
}

internal fun legacyClaimMatchesOwner(expectedLegacy: AuthSession, canonical: AuthSession): Boolean {
    val expectedOrigin = canonicalHttpsOrigin(expectedLegacy.serverOrigin) ?: return false
    val canonicalOrigin = canonical.serverOrigin.takeIf(String::isNotBlank)
        ?.let(::canonicalHttpsOrigin)
        ?: expectedOrigin
    return expectedLegacy.legacy &&
        expectedLegacy.userId.isNotBlank() &&
        canonical.userId == expectedLegacy.userId &&
        canonicalOrigin == expectedOrigin
}

internal fun legacyMigrationAllowsAuthenticatedWork(context: Context): Boolean {
    return captureAuthFence(context.applicationContext) != null
}
