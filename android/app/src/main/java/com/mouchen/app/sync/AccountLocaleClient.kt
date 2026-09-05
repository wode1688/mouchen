package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.localization.normalizeMouchenLocale
import com.mouchen.app.network.MouchenDns
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

internal sealed interface AccountLocaleResult {
    data class Success(val locale: String) : AccountLocaleResult
    data class Failure(val reason: String) : AccountLocaleResult
}

/**
 * Implements the shared account contract; the server value is authoritative across devices.
 * This client deliberately never persists a response: the Activity must first verify its
 * generation/user/token fence so a response from a signed-out session cannot restore that session.
 */
internal class AccountLocaleClient(
    context: Context,
    private val sessionStore: AuthSessionStore = AuthSessionStore(context),
    private val connectionStore: BackendConnectionStore = BackendConnectionStore(context),
    private val client: OkHttpClient = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .build(),
) {
    fun fetch(): AccountLocaleResult = request(null)

    fun update(locale: String, expectedSession: AuthSession): AccountLocaleResult {
        val normalized = normalizeMouchenLocale(locale)
            ?: return AccountLocaleResult.Failure("unsupported_locale")
        return request(normalized, expectedSession)
    }

    private fun request(locale: String?, expectedSession: AuthSession? = null): AccountLocaleResult {
        val storedSession = sessionStore.load()
            ?: return AccountLocaleResult.Failure("authentication_required")
        val session = if (expectedSession == null) {
            storedSession
        } else {
            expectedSession.takeIf { accountLocaleSessionMatches(it, storedSession) }
                ?: return AccountLocaleResult.Failure("obsolete_session")
        }
        val connection = connectionStore.load()
        if (!connection.enabled || !sameHttpsOrigin(connection.baseUrl, session.serverOrigin)) {
            return AccountLocaleResult.Failure("secure_backend_required")
        }
        return runCatching {
            val builder = Request.Builder()
                .url(session.serverOrigin + "/v1/account/preferences")
                .header("Authorization", "Bearer ${session.accessToken}")
                .header("Accept", "application/json")
            if (locale == null) {
                builder.get()
            } else {
                builder.put(
                    encodeAccountLocaleRequest(locale)
                        .toRequestBody("application/json; charset=utf-8".toMediaType()),
                )
            }
            client.newCall(builder.build()).execute().use { response ->
                if (!response.isSuccessful) return@use AccountLocaleResult.Failure("http_${response.code}")
                val authoritative = parseAccountLocaleResponse(response.body?.string().orEmpty())
                    ?: return@use AccountLocaleResult.Failure("invalid_response")
                AccountLocaleResult.Success(authoritative)
            }
        }.getOrElse { AccountLocaleResult.Failure("network_unavailable") }
    }
}

internal fun accountLocaleSessionMatches(expected: AuthSession, current: AuthSession): Boolean =
    expected.userId == current.userId &&
        expected.username == current.username &&
        expected.accessToken == current.accessToken &&
        expected.sessionId == current.sessionId &&
        expected.deviceId == current.deviceId &&
        expected.legacy == current.legacy &&
        expected.serverOrigin == current.serverOrigin &&
        expected.epoch == current.epoch

internal fun resolveAccountLocaleRollback(
    requestSession: AuthSession,
    acknowledgedSession: AuthSession?,
    acknowledgedLocale: String?,
): String = acknowledgedLocale
    ?.takeIf { acknowledgedSession != null && accountLocaleSessionMatches(acknowledgedSession, requestSession) }
    ?.let(::normalizeMouchenLocale)
    ?: normalizeMouchenLocale(requestSession.locale)
    ?: error("Authenticated session has an unsupported locale")

internal fun encodeAccountLocaleRequest(locale: String): String = JSONObject()
    .put("locale", normalizeMouchenLocale(locale) ?: error("Unsupported locale"))
    .toString()

internal fun parseAccountLocaleResponse(raw: String): String? = runCatching {
    val root = JSONObject(raw)
    normalizeMouchenLocale(
        root.optString("locale").ifBlank { root.optJSONObject("preferences")?.optString("locale").orEmpty() },
    )
}.getOrNull()
