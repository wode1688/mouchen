package com.mouchen.app.sync

import android.content.Context
import android.content.Intent
import android.os.Build
import com.mouchen.app.collectors.MailConnectionStore
import com.mouchen.app.collectors.RssFeedStore
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.localization.LOCALE_ZH_CN
import com.mouchen.app.localization.MouchenLocaleStore
import com.mouchen.app.localization.normalizeMouchenLocale
import com.mouchen.app.models.SecureSettingsStore
import com.mouchen.app.models.OllamaConnectionStore
import com.mouchen.app.network.MouchenDns
import com.mouchen.app.security.EncryptedBlobStore
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

internal const val ACTION_AUTH_STATE_CHANGED = "com.mouchen.app.action.AUTH_STATE_CHANGED"

internal data class AuthSession(
    val userId: String,
    val username: String,
    val accessToken: String,
    val sessionId: String = "",
    val deviceId: String,
    val legacy: Boolean = false,
    val serverOrigin: String = "",
    val epoch: Long = 0L,
    val locale: String = LOCALE_ZH_CN,
) {
    override fun toString(): String =
        "AuthSession(userId=$userId, username=$username, accessToken=<redacted>, " +
            "sessionId=$sessionId, deviceId=$deviceId, legacy=$legacy, serverOrigin=$serverOrigin, epoch=$epoch, locale=$locale)"
}

internal class AuthSessionStore(context: Context) {
    private val appContext = context.applicationContext
    private val secureStore = SecureSettingsStore(appContext, NAMESPACE)

    fun load(): AuthSession? = synchronized(LOCK) { loadUnlocked() }

    fun save(session: AuthSession): Boolean = synchronized(LOCK) { saveUnlocked(session) }

    /** Atomically merges only locale when the request still belongs to the exact stored session. */
    fun updateLocaleIfSessionMatches(expected: AuthSession, locale: String): AuthSession? = synchronized(LOCK) {
        val current = loadUnlocked() ?: return@synchronized null
        val updated = mergeAccountLocaleIfSessionMatches(expected, current, locale)
            ?: return@synchronized null
        updated.takeIf(::saveUnlocked)
    }

    private fun loadUnlocked(): AuthSession? = secureStore.get(SESSION_KEY)?.let(::decodeAuthSession)

    private fun saveUnlocked(session: AuthSession): Boolean {
        require(session.userId.isNotBlank())
        require(session.accessToken.isNotBlank())
        require(canonicalHttpsOrigin(session.serverOrigin) == session.serverOrigin)
        require(session.epoch > 0L)
        val saved = secureStore.putDurably(SESSION_KEY, encodeAuthSession(session))
        if (saved) {
            secureStore.putDurably(LAST_USER_KEY, session.userId)
            notifyChanged()
        }
        return saved
    }

    fun lastUserId(): String? = secureStore.get(LAST_USER_KEY)?.takeIf(String::isNotBlank)

    /** Keeps last_user_id so a later login cannot silently expose another account's local rows. */
    fun clear(): Boolean = synchronized(LOCK) {
        val removed = secureStore.removeDurably(SESSION_KEY)
        notifyChanged()
        removed
    }

    fun credentialCandidate(connection: BackendConnection): StoredCredential? {
        load()?.let {
            return StoredCredential(
                it.accessToken,
                it.deviceId,
                legacy = it.legacy,
                legacyUserId = if (it.legacy) connection.userId.trim().takeIf(String::isNotEmpty) else null,
            )
        }
        val legacyToken = connection.bearerToken?.trim().takeIf { !it.isNullOrEmpty() } ?: return null
        return StoredCredential(
            legacyToken,
            DeviceIdentityStore(appContext).getOrCreate(),
            legacy = true,
            legacyUserId = connection.userId.trim().takeIf(String::isNotEmpty),
        )
    }

    private fun notifyChanged() {
        appContext.sendBroadcast(Intent(ACTION_AUTH_STATE_CHANGED).setPackage(appContext.packageName))
    }

    private companion object {
        val LOCK = Any()
        const val NAMESPACE = "mouchen_auth_session"
        const val SESSION_KEY = "session"
        const val LAST_USER_KEY = "last_user_id"
    }
}

internal fun mergeAccountLocaleIfSessionMatches(
    expected: AuthSession,
    current: AuthSession,
    locale: String,
): AuthSession? {
    val normalized = normalizeMouchenLocale(locale) ?: return null
    return current.takeIf { accountLocaleSessionMatches(expected, it) }?.copy(locale = normalized)
}

internal data class StoredCredential(
    val accessToken: String,
    val deviceId: String,
    val legacy: Boolean,
    val legacyUserId: String? = null,
)

internal class DeviceIdentityStore(context: Context) {
    private val appContext = context.applicationContext

    /** Reuses the existing push identity so authenticated device checks cannot diverge. */
    fun getOrCreate(): String = AndroidDeviceIdentity(appContext).deviceId()
}

internal enum class AuthMode(val path: String) {
    LOGIN("/v1/auth/login"),
    REGISTER("/v1/auth/register"),
}

internal sealed interface AuthResult {
    data class Success(val session: AuthSession) : AuthResult
    data class Failure(val message: String, val unauthorized: Boolean = false) : AuthResult
}

internal class AuthApiClient(
    context: Context,
    private val connectionStore: BackendConnectionStore = BackendConnectionStore(context),
    private val sessionStore: AuthSessionStore = AuthSessionStore(context),
    private val deviceStore: DeviceIdentityStore = DeviceIdentityStore(context),
    private val client: OkHttpClient = OkHttpClient.Builder()
        .dns(MouchenDns)
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build(),
) {
    private val appContext = context.applicationContext

    fun authenticate(
        mode: AuthMode,
        username: String,
        password: String,
        baseUrl: String? = null,
        registrationCode: String = "",
    ): AuthResult {
        val validation = validateCredentials(username, password, requireStrongPassword = mode == AuthMode.REGISTER)
        if (validation != null) return AuthResult.Failure(validation)
        val current = connectionStore.load()
        val requestEpoch = AuthEpochStore(appContext).current()
        val destination = (baseUrl ?: current.baseUrl).trim().trimEnd('/')
        if (!isSecureBackendUrl(destination)) return AuthResult.Failure("服务器地址必须使用 HTTPS")
        val deviceId = runCatching { deviceStore.getOrCreate() }
            .getOrElse { return AuthResult.Failure("无法建立本机安全标识，请重试") }
        return try {
            val body = buildAuthRequestJson(
                mode = mode,
                username = username,
                password = password,
                deviceId = deviceId,
                deviceName = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
                registrationCode = registrationCode,
                locale = MouchenLocaleStore(appContext).loadPreLogin(),
            )
            val request = Request.Builder()
                .url(destination + mode.path)
                .post(body.toRequestBody(JSON))
                .build()
            val token = client.newCall(request).execute().use { response ->
                val responseBody = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return AuthResult.Failure(authFailureMessage(response.code, mode), response.code in setOf(401, 403))
                }
                parseAccessToken(responseBody)
                    ?: return AuthResult.Failure("服务器没有返回有效登录凭据")
            }
            val canonical = fetchCanonicalSession(destination, token, deviceId)
                ?: return AuthResult.Failure("登录成功，但无法确认账号身份")
            AuthResult.Success(
                persistCanonical(
                    destination,
                    current,
                    canonical,
                    expectedEpoch = requestEpoch,
                    forceNewGeneration = true,
                ),
            )
        } catch (_: Exception) {
            AuthResult.Failure("无法连接AI替身服务器，请检查网络后重试")
        }
    }

    /**
     * Converts a verified private-alpha owner session into a password account without ever
     * accepting an editable destination or a different server principal. A token issued for the
     * wrong account is revoked as a detached session; the established legacy session, local queue,
     * and account consent are left untouched.
     */
    fun claimLegacyOwner(
        mode: AuthMode,
        username: String,
        password: String,
        expectedLegacy: AuthSession,
        registrationCode: String = "",
    ): AuthResult {
        val validation = validateCredentials(username, password, requireStrongPassword = mode == AuthMode.REGISTER)
        if (validation != null) return AuthResult.Failure(validation)
        val fixedOrigin = canonicalHttpsOrigin(expectedLegacy.serverOrigin)
            ?: return AuthResult.Failure("旧主人会话的服务器地址无效，请退出后重新连接")
        val currentConnection = connectionStore.load()
        val currentSession = sessionStore.load()
        val requestEpoch = AuthEpochStore(appContext).current()
        if (
            !legacyClaimStateIsCurrent(expectedLegacy, currentSession, requestEpoch) ||
            !sameHttpsOrigin(currentConnection.baseUrl, fixedOrigin)
        ) {
            return AuthResult.Failure("旧主人会话已经变化，请返回后重新确认")
        }

        var issuedToken: String? = null
        return try {
            val body = buildAuthRequestJson(
                mode = mode,
                username = username,
                password = password,
                deviceId = expectedLegacy.deviceId,
                deviceName = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
                registrationCode = registrationCode,
                locale = MouchenLocaleStore(appContext).loadPreLogin(),
            )
            val request = Request.Builder()
                .url(fixedOrigin + mode.path)
                .post(body.toRequestBody(JSON))
                .build()
            val token = client.newCall(request).execute().use { response ->
                val responseBody = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return AuthResult.Failure(authFailureMessage(response.code, mode))
                }
                parseAccessToken(responseBody)
                    ?: return AuthResult.Failure("服务器没有返回有效登录凭据")
            }
            issuedToken = token
            val canonical = fetchCanonicalSession(fixedOrigin, token, expectedLegacy.deviceId)
                ?: return revokeDetachedAndFail(fixedOrigin, token, "账号已验证，但无法确认服务器身份")
            if (!legacyClaimMatchesOwner(expectedLegacy, canonical)) {
                return revokeDetachedAndFail(
                    fixedOrigin,
                    token,
                    "这个账号不属于当前旧主人数据；新会话已撤销，本机旧数据未改动",
                )
            }
            if (!legacyClaimStateIsCurrent(expectedLegacy, sessionStore.load(), requestEpoch)) {
                return revokeDetachedAndFail(fixedOrigin, token, "旧主人会话已经变化；新会话已撤销，请重试")
            }
            val persisted = persistCanonical(
                baseUrl = fixedOrigin,
                previous = currentConnection,
                session = canonical.copy(legacy = false),
                expectedEpoch = requestEpoch,
                forceNewGeneration = true,
            )
            issuedToken = null
            AuthResult.Success(persisted)
        } catch (_: Exception) {
            issuedToken?.let { revokeDetachedToken(fixedOrigin, it) }
            AuthResult.Failure("无法领取主人账号；旧会话和本机数据保持不变，请检查网络后重试")
        }
    }

    fun restore(): AuthResult {
        val connection = connectionStore.load()
        val requestEpoch = AuthEpochStore(appContext).current()
        val candidate = sessionStore.credentialCandidate(connection)
            ?: return AuthResult.Failure("请先登录", unauthorized = true)
        if (!connection.enabled || !isSecureBackendUrl(connection.baseUrl)) {
            return AuthResult.Failure("服务器连接未启用或地址不安全")
        }
        sessionStore.load()?.serverOrigin?.takeIf(String::isNotBlank)?.let { boundOrigin ->
            if (!sameHttpsOrigin(boundOrigin, connection.baseUrl)) {
                return AuthResult.Failure("服务器地址已改变，请重新登录", unauthorized = true)
            }
        }
        return try {
            val request = sessionRequest(
                connection.baseUrl,
                candidate.accessToken,
                legacyUserId = candidate.legacyUserId,
            )
            client.newCall(request).execute().use { response ->
                val body = response.body?.string().orEmpty()
                when {
                    shouldInvalidateSessionStatus(response.code) ->
                        AuthResult.Failure("登录已失效，请重新登录", unauthorized = true)
                    !response.isSuccessful -> AuthResult.Failure("暂时无法确认登录状态")
                    else -> {
                        val parsed = parseCanonicalSession(body, candidate.accessToken, candidate.deviceId)
                            ?: return AuthResult.Failure("服务器返回的账号信息无效")
                        val canonical = if (candidate.legacy) {
                            // Legacy principals use a server placeholder device. Keep this
                            // installation's established push identity during migration.
                            parsed.copy(deviceId = candidate.deviceId, legacy = true)
                        } else {
                            parsed.takeIf { it.deviceId == candidate.deviceId }
                                ?: return AuthResult.Failure("登录设备与服务器会话不一致", unauthorized = true)
                        }
                        AuthResult.Success(
                            persistCanonical(
                                connection.baseUrl,
                                connection,
                                canonical,
                                expectedEpoch = requestEpoch,
                            ),
                        )
                    }
                }
            }
        } catch (_: Exception) {
            AuthResult.Failure("暂时无法连接服务器")
        }
    }

    fun logout(): Boolean {
        data class LogoutState(
            val token: String?,
            val origin: String?,
            val sessionCleared: Boolean,
            val connectionCleared: Boolean,
        )
        val captured = withAuthStateTransition {
            val connection = connectionStore.load()
            val oldSession = sessionStore.load()
            val token = oldSession?.accessToken ?: connection.bearerToken
            val origin = oldSession?.serverOrigin?.let(::canonicalHttpsOrigin)
                ?: connection.baseUrl.takeIf(::isSecureBackendUrl)?.let(::canonicalHttpsOrigin)
            // Clear before the slow revoke request. A concurrent later login can no longer be
            // erased by this logout when its HTTP response eventually arrives.
            runCatching { invalidateAuthenticatedExecution(appContext) }
            LogoutState(
                token = token,
                origin = origin,
                sessionCleared = sessionStore.clear(),
                connectionCleared = runCatching { connectionStore.clearAuthentication() }.getOrDefault(false),
            )
        }
        val token = captured.token
        val revokeOrigin = captured.origin
        val revoked = if (!token.isNullOrBlank() && revokeOrigin != null) {
            runCatching {
                val request = Request.Builder()
                    .url(revokeOrigin + "/v1/auth/logout")
                    .header("Authorization", "Bearer $token")
                    .post("{}".toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { it.isSuccessful || it.code in setOf(401, 403) }
            }.getOrDefault(false)
        } else true
        return revoked && captured.sessionCleared && captured.connectionCleared
    }

    private fun fetchCanonicalSession(baseUrl: String, token: String, deviceId: String): AuthSession? =
        client.newCall(sessionRequest(baseUrl, token)).execute().use { response ->
            if (!response.isSuccessful) return null
            parseCanonicalSession(response.body?.string().orEmpty(), token, deviceId)
                ?.takeIf { it.deviceId == deviceId }
        }

    private fun revokeDetachedAndFail(origin: String, token: String, message: String): AuthResult.Failure {
        val revoked = revokeDetachedToken(origin, token)
        return AuthResult.Failure(
            if (revoked) message
            else "$message；本机未保存该账号，但服务器撤销尚未确认，请联系管理员关闭那次会话",
        )
    }

    private fun revokeDetachedToken(origin: String, token: String): Boolean {
        repeat(2) {
            val revoked = runCatching {
                val request = Request.Builder()
                    .url(origin + "/v1/auth/logout")
                    .header("Authorization", "Bearer $token")
                    .post("{}".toRequestBody(JSON))
                    .build()
                client.newCall(request).execute().use { it.isSuccessful || it.code in setOf(401, 403) }
            }.getOrDefault(false)
            if (revoked) return true
        }
        return false
    }

    private fun persistCanonical(
        baseUrl: String,
        previous: BackendConnection,
        session: AuthSession,
        expectedEpoch: Long,
        forceNewGeneration: Boolean = false,
    ): AuthSession = withAuthStateTransition {
        check(AuthEpochStore(appContext).current() == expectedEpoch) {
            "Authentication response belongs to an obsolete generation"
        }
        val newOrigin = checkNotNull(canonicalHttpsOrigin(baseUrl)) { "Invalid authenticated origin" }
        val previousSession = sessionStore.load()
        val previousUser = previousAccountUserId(sessionStore.lastUserId(), previous)
        val previousOrigin = previousSession?.serverOrigin?.takeIf(String::isNotBlank)
            ?: previous.baseUrl.takeIf { !previous.bearerToken.isNullOrBlank() }
        val existingFence = captureAuthFence(appContext)
        val canKeepGeneration = !forceNewGeneration &&
            previousSession != null &&
            previousSession.userId == session.userId &&
            previousSession.accessToken == session.accessToken &&
            existingFence?.userId == session.userId &&
            existingFence.origin == newOrigin
        val epoch = if (canKeepGeneration) {
            checkNotNull(existingFence).epoch
        } else {
            invalidateAuthenticatedExecution(appContext)
        }
        if (shouldClearAccountLocalData(previousUser, session.userId, previousOrigin, newOrigin)) {
            clearAccountLocalData(appContext)
        }
        val bound = session.copy(serverOrigin = newOrigin, epoch = epoch)
        val consentStored = if (bound.legacy) {
            AccountConsentStore(appContext).migrateLegacyOwner(bound.userId, newOrigin, previous)
        } else {
            AccountConsentStore(appContext).ensureCommercialDefaults(bound.userId, newOrigin)
        }
        check(consentStored) { "Unable to persist account consent defaults" }
        if (previous.baseUrl.trimEnd('/') != baseUrl.trimEnd('/')) {
            check(
                connectionStore.saveDurably(
                    previous.copy(
                        baseUrl = baseUrl,
                        userId = "",
                        bearerToken = null,
                        enabled = true,
                        sttBaseUrl = previous.sttBaseUrl?.takeIf { sameHttpsOrigin(it, baseUrl) },
                    ),
                ),
            ) { "Unable to persist authenticated origin" }
        }
        check(sessionStore.save(bound)) { "Unable to persist login" }
        if (!connectionStore.saveAuthenticatedIdentity(bound.userId, bound.accessToken)) {
            sessionStore.clear()
            error("Unable to persist authenticated identity")
        }
        bound
    }

    private fun sessionRequest(baseUrl: String, token: String, legacyUserId: String? = null): Request =
        Request.Builder()
            .url(baseUrl.trimEnd('/') + "/v1/session")
            .header("Authorization", "Bearer $token")
            .apply {
                // New sessions never trust or transmit editable user IDs. This compatibility
                // header is limited to a pre-existing private-alpha token migration.
                if (!legacyUserId.isNullOrBlank()) header("X-User-Id", legacyUserId)
            }
            .get()
            .build()

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}

/** Removes account-owned local rows and integrations so another login cannot inherit them. */
internal fun clearAccountLocalData(context: Context) {
    val appContext = context.applicationContext
    MouchenDatabase.get(appContext).clearAllTables()
    check(EncryptedBlobStore(appContext).clearDurably()) { "Unable to clear encrypted capture data" }
    check(MailConnectionStore(appContext).clearDurably()) { "Unable to clear mail credentials" }
    check(RssFeedStore(appContext).clearDurably()) { "Unable to clear RSS settings" }
    check(OllamaConnectionStore(appContext).clearDurably()) { "Unable to clear private-model credentials" }
    listOf(
        "collector_watermarks",
        "mouchen_calendar_state",
        "mouchen_imap_watermarks",
        "private_phone_context_watermarks",
        "mouchen_ime_learning",
        "mouchen_ime_preferences",
        "mouchen_timing_settings",
    ).forEach { name ->
        check(appContext.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().commit()) {
            "Unable to clear account preference: $name"
        }
    }
    check(SyncDemandStore(appContext).release()) { "Unable to release sync demand" }
}

internal fun validateCredentials(
    username: String,
    password: String,
    requireStrongPassword: Boolean = true,
): String? = when {
    username.trim().length !in 3..64 -> "用户名需为 3–64 个字符"
    username.trim().any { it.isWhitespace() || it.isISOControl() } -> "用户名不能包含空格或控制字符"
    password.isEmpty() -> "请输入密码"
    requireStrongPassword && password.length < 10 -> "密码至少需要 10 个字符"
    password.length > 1024 -> "密码过长"
    else -> null
}

internal fun encodeAuthSession(session: AuthSession): String = JSONObject()
    .put("schema_version", 2)
    .put("user_id", session.userId)
    .put("username", session.username)
    .put("access_token", session.accessToken)
    .put("session_id", session.sessionId)
    .put("device_id", session.deviceId)
    .put("legacy", session.legacy)
    .put("server_origin", session.serverOrigin)
    .put("epoch", session.epoch)
    .put("locale", normalizeMouchenLocale(session.locale) ?: LOCALE_ZH_CN)
    .toString()

internal fun decodeAuthSession(raw: String): AuthSession? = runCatching {
    val json = JSONObject(raw)
    AuthSession(
        userId = json.getString("user_id").trim(),
        username = json.optString("username").trim(),
        accessToken = json.getString("access_token").trim(),
        sessionId = json.optString("session_id").trim(),
        deviceId = json.getString("device_id").trim(),
        legacy = json.optBoolean("legacy", false),
        serverOrigin = json.optString("server_origin").trim(),
        epoch = json.optLong("epoch", 0L),
        locale = normalizeMouchenLocale(json.optString("locale")) ?: LOCALE_ZH_CN,
    ).takeIf { it.userId.isNotEmpty() && it.accessToken.isNotEmpty() && isValidDeviceId(it.deviceId) }
}.getOrNull()

internal fun parseAccessToken(raw: String): String? = runCatching {
    val root = JSONObject(raw)
    root.optString("access_token").trim().takeIf(String::isNotEmpty)
        ?: root.optJSONObject("session")?.optString("access_token")?.trim()?.takeIf(String::isNotEmpty)
}.getOrNull()

internal fun parseCanonicalSession(raw: String, accessToken: String, fallbackDeviceId: String): AuthSession? = runCatching {
    val root = JSONObject(raw)
    val session = root.optJSONObject("session") ?: root
    val user = root.optJSONObject("user") ?: session.optJSONObject("user")
    val userId = sequenceOf(
        root.optString("user_id"),
        session.optString("user_id"),
        user?.optString("id"),
    ).map { it?.trim().orEmpty() }.firstOrNull(String::isNotEmpty).orEmpty()
    val username = sequenceOf(
        root.optString("username"),
        session.optString("username"),
        user?.optString("username"),
    ).map { it?.trim().orEmpty() }.firstOrNull(String::isNotEmpty).orEmpty()
    val deviceId = sequenceOf(root.optString("device_id"), session.optString("device_id"), fallbackDeviceId)
        .map { it.trim() }.firstOrNull(String::isNotEmpty).orEmpty()
    val sessionId = sequenceOf(root.optString("session_id"), session.optString("session_id"), session.optString("id"))
        .map { it.trim() }.firstOrNull(String::isNotEmpty).orEmpty()
    val locale = sequenceOf(
        root.optString("locale"),
        session.optString("locale"),
        user?.optString("locale"),
        root.optJSONObject("preferences")?.optString("locale"),
    ).mapNotNull(::normalizeMouchenLocale).firstOrNull() ?: LOCALE_ZH_CN
    AuthSession(userId, username, accessToken, sessionId, deviceId, locale = locale)
        .takeIf { it.userId.isNotEmpty() && it.accessToken.isNotEmpty() && isValidDeviceId(it.deviceId) }
}.getOrNull()

internal fun isValidDeviceId(value: String): Boolean = value.isNotBlank() && value.length <= 160 &&
    value.none { it.isWhitespace() || it.isISOControl() }

internal fun buildAuthRequestJson(
    mode: AuthMode,
    username: String,
    password: String,
    deviceId: String,
    deviceName: String,
    registrationCode: String = "",
    locale: String = LOCALE_ZH_CN,
): String = JSONObject()
    .put("username", username.trim())
    .put("password", password)
    .put("device_id", deviceId)
    .put("device_name", deviceName.trim().take(120))
    .apply {
        if (mode == AuthMode.REGISTER) {
            put("locale", normalizeMouchenLocale(locale) ?: LOCALE_ZH_CN)
            if (registrationCode.isNotBlank()) {
                put("registration_code", registrationCode.trim().take(512))
            }
        }
    }
    .toString()

internal fun shouldInvalidateSessionStatus(httpStatus: Int): Boolean = httpStatus in setOf(401, 403)

internal fun shouldClearAccountLocalData(previousUserId: String?, canonicalUserId: String): Boolean =
    !previousUserId.isNullOrBlank() && previousUserId != canonicalUserId

internal fun shouldClearAccountLocalData(
    previousUserId: String?,
    canonicalUserId: String,
    previousOrigin: String?,
    canonicalOrigin: String,
): Boolean = shouldClearAccountLocalData(previousUserId, canonicalUserId) ||
    (!previousUserId.isNullOrBlank() && previousOrigin?.let { !sameHttpsOrigin(it, canonicalOrigin) } == true)

internal fun previousAccountUserId(
    rememberedSessionUserId: String?,
    connection: BackendConnection,
): String? = rememberedSessionUserId?.trim()?.takeIf(String::isNotEmpty)
    ?: connection.userId.trim().takeIf {
        it.isNotEmpty() && !connection.bearerToken.isNullOrBlank()
    }

internal fun legacyClaimStateIsCurrent(
    expected: AuthSession,
    current: AuthSession?,
    currentEpoch: Long,
): Boolean = expected.legacy && current?.legacy == true &&
    expected.userId == current.userId &&
    expected.accessToken == current.accessToken &&
    expected.deviceId == current.deviceId &&
    expected.epoch > 0L && expected.epoch == current.epoch && expected.epoch == currentEpoch &&
    sameHttpsOrigin(expected.serverOrigin, current.serverOrigin)

private fun authFailureMessage(code: Int, mode: AuthMode): String = when (code) {
    400, 422 -> "用户名或密码格式不符合要求"
    401 -> "用户名或密码不正确"
    403 -> if (mode == AuthMode.REGISTER) "邀请码无效、已使用或注册尚未开放" else "账号暂时无法登录"
    409 -> if (mode == AuthMode.REGISTER) "用户名已使用、邀请码已使用或注册状态冲突" else "账号状态冲突"
    429 -> "尝试次数过多，请稍后再试"
    in 500..599 -> "服务器暂时不可用，请稍后再试"
    else -> "请求失败（HTTP $code）"
}
