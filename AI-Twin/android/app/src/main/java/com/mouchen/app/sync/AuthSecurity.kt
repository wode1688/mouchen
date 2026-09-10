package com.mouchen.app.sync

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import androidx.work.WorkManager
import com.mouchen.app.AUDIO_CAPTURE_SERVICE_CLASS
import com.mouchen.app.SCREEN_CAPTURE_SERVICE_CLASS
import java.io.IOException
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.OkHttpClient
import okhttp3.Response

/** Stable authenticated tenant boundary used by workers and network requests. */
internal data class AuthFence(
    val epoch: Long,
    val userId: String,
    val origin: String,
) {
    fun isCurrent(context: Context): Boolean = captureAuthFence(context) == this
}

/** Non-secret monotonic generation. A durable increment invalidates every older in-flight task. */
internal class AuthEpochStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun current(): Long = preferences.getLong(KEY, 0L).coerceAtLeast(0L)

    fun advance(): Long = synchronized(LOCK) {
        val current = current()
        check(current < Long.MAX_VALUE) { "Authentication epoch exhausted" }
        val next = current + 1L
        check(preferences.edit().putLong(KEY, next).commit()) { "Unable to persist authentication epoch" }
        next
    }

    private companion object {
        const val PREFERENCES = "mouchen_auth_epoch"
        const val KEY = "epoch"
        val LOCK = Any()
    }
}

private val AUTH_STATE_TRANSITION_LOCK = Any()

/** Serializes login, restore, logout, account switch, and origin switch commits. */
internal fun <T> withAuthStateTransition(block: () -> T): T =
    synchronized(AUTH_STATE_TRANSITION_LOCK, block)

internal fun canonicalHttpsOrigin(value: String): String? {
    val url = value.trim().toHttpUrlOrNull() ?: return null
    if (url.scheme != "https" || url.username.isNotEmpty() || url.password.isNotEmpty()) return null
    return url.newBuilder()
        .encodedPath("/")
        .query(null)
        .fragment(null)
        .build()
        .toString()
        .removeSuffix("/")
}

internal fun sameHttpsOrigin(first: String, second: String): Boolean {
    val firstOrigin = canonicalHttpsOrigin(first) ?: return false
    return firstOrigin == canonicalHttpsOrigin(second)
}

internal fun captureAuthFence(context: Context): AuthFence? {
    val appContext = context.applicationContext
    val session = AuthSessionStore(appContext).load() ?: return null
    if (!LegacyMigrationRuntimeGate.allows(session)) return null
    val origin = canonicalHttpsOrigin(session.serverOrigin) ?: return null
    if (session.epoch <= 0L || session.epoch != AuthEpochStore(appContext).current()) return null
    return AuthFence(session.epoch, session.userId, origin)
}

/** Overlays server-owned identity and refuses a settings destination outside the login origin. */
internal fun authenticatedConnection(context: Context, stored: BackendConnection): BackendConnection? {
    val session = AuthSessionStore(context.applicationContext).load() ?: return null
    val fence = captureAuthFence(context) ?: return null
    if (fence.userId != session.userId || !sameHttpsOrigin(stored.baseUrl, fence.origin)) return null
    return stored.copy(
        userId = session.userId,
        bearerToken = session.accessToken,
        sessionEpoch = fence.epoch,
        sessionOrigin = fence.origin,
    )
}

internal fun authenticatedConnectionIsCurrent(context: Context, connection: BackendConnection): Boolean =
    connection.sessionEpoch > 0L &&
        AuthFence(connection.sessionEpoch, connection.userId, connection.sessionOrigin).isCurrent(context) &&
        sameHttpsOrigin(connection.baseUrl, connection.sessionOrigin)

/**
 * Invalidates in-flight work before an account/origin transition. Cancellation is defense in
 * depth; the durable epoch remains authoritative when Android cannot stop a running callback.
 */
internal fun invalidateAuthenticatedExecution(context: Context): Long {
    val appContext = context.applicationContext
    LegacyMigrationRuntimeGate.reset()
    val epoch = AuthEpochStore(appContext).advance()
    listOf(AUDIO_CAPTURE_SERVICE_CLASS, SCREEN_CAPTURE_SERVICE_CLASS).forEach { className ->
        runCatching {
            appContext.stopService(Intent().setComponent(ComponentName(appContext.packageName, className)))
        }
    }
    val workManager = WorkManager.getInstance(appContext)
    AUTH_BOUND_UNIQUE_WORK.forEach { name -> runCatching { workManager.cancelUniqueWork(name) } }
    return epoch
}

/** Rejects stale tokens, cross-origin bearer use, redirects, and responses from an old epoch. */
internal class SessionAuthorizationInterceptor(context: Context) : Interceptor {
    private val appContext = context.applicationContext

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val authorization = request.header("Authorization") ?: return chain.proceed(request)
        if (!authorization.startsWith(BEARER_PREFIX, ignoreCase = true)) return chain.proceed(request)
        val session = AuthSessionStore(appContext).load()
            ?: throw IOException("Authenticated session is unavailable")
        val fence = captureAuthFence(appContext)
            ?: throw IOException("Authenticated session generation is stale")
        val suppliedToken = authorization.substringAfter(' ', "")
        if (suppliedToken != session.accessToken || fence.userId != session.userId) {
            throw IOException("Bearer token does not belong to the current session")
        }
        if (canonicalHttpsOrigin(request.url.toString()) != fence.origin) {
            throw IOException("Bearer token cannot leave the authenticated origin")
        }
        val response = chain.proceed(request)
        if (!fence.isCurrent(appContext)) {
            response.close()
            throw IOException("Authenticated response belongs to an obsolete session")
        }
        return response
    }

    private companion object {
        const val BEARER_PREFIX = "Bearer "
    }
}

internal fun OkHttpClient.Builder.enforceSessionAuthorization(context: Context): OkHttpClient.Builder =
    followRedirects(false)
        .followSslRedirects(false)
        .addInterceptor(SessionAuthorizationInterceptor(context.applicationContext))

private val AUTH_BOUND_UNIQUE_WORK = setOf(
    "mouchen-immediate-collection",
    "mouchen-incremental-collection",
    "mouchen-realtime-sync",
    "mouchen-urgent-event-drain",
    "mouchen-advice-poll",
    "mouchen-advice-attention-poll",
    "mouchen-queued-event-advice-poll",
    "mouchen-goal-refresh",
    "mouchen-advice-attention-now",
    "mouchen-audio-transcription-recovery-periodic",
    "mouchen-audio-transcription-recovery-immediate",
)
