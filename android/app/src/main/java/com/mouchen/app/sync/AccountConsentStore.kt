package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.models.SecureSettingsStore
import java.security.MessageDigest
import org.json.JSONObject

/** Application-layer consent, scoped to the authenticated server principal. */
internal data class AccountConsent(
    val collectionEnabled: Boolean = false,
    val cloudAnalysisEnabled: Boolean = false,
    val fullContextCloudEnabled: Boolean = false,
)

internal class AccountConsentStore(context: Context) {
    private val appContext = context.applicationContext
    private val secureStore = SecureSettingsStore(appContext, NAMESPACE)

    fun load(userId: String, serverOrigin: String = currentOrigin(userId)): AccountConsent = secureStore.get(key(userId, serverOrigin))
        ?.let(::decodeAccountConsent)
        ?: AccountConsent()

    fun save(userId: String, consent: AccountConsent, serverOrigin: String = currentOrigin(userId)): Boolean {
        require(userId.isNotBlank())
        require(canonicalHttpsOrigin(serverOrigin) == serverOrigin)
        return secureStore.putDurably(key(userId, serverOrigin), encodeAccountConsent(consent))
    }

    fun ensureCommercialDefaults(userId: String, serverOrigin: String): Boolean {
        require(userId.isNotBlank())
        require(canonicalHttpsOrigin(serverOrigin) == serverOrigin)
        val accountKey = key(userId, serverOrigin)
        if (secureStore.get(accountKey) != null) return true
        return secureStore.putDurably(accountKey, encodeAccountConsent(commercialAccountConsentDefault()))
    }

    /**
     * Legacy private-alpha was a single owner deployment. Preserve its established collection and
     * cloud choices once under the server-confirmed legacy principal, rather than silently turning
     * those choices off during migration.
     */
    fun migrateLegacyOwner(userId: String, serverOrigin: String, connection: BackendConnection): Boolean {
        require(userId.isNotBlank())
        require(canonicalHttpsOrigin(serverOrigin) == serverOrigin)
        val accountKey = key(userId, serverOrigin)
        if (secureStore.get(accountKey) != null) return true
        val established = secureStore.get(accountConsentKey(userId))?.let(::decodeAccountConsent)
            ?: legacyOwnerConsent(connection)
        return secureStore.putDurably(
            accountKey,
            encodeAccountConsent(established),
        )
    }

    private fun currentOrigin(userId: String): String = AuthSessionStore(appContext).load()
        ?.takeIf { it.userId == userId }
        ?.serverOrigin
        .orEmpty()

    private fun key(userId: String, serverOrigin: String): String = accountConsentKey(userId, serverOrigin)

    private companion object {
        const val NAMESPACE = "mouchen_account_consent"
    }
}

internal fun encodeAccountConsent(consent: AccountConsent): String = JSONObject()
    .put("schema_version", 1)
    .put("collection_enabled", consent.collectionEnabled)
    .put("cloud_analysis_enabled", consent.cloudAnalysisEnabled)
    .put("full_context_cloud_enabled", consent.fullContextCloudEnabled)
    .toString()

internal fun decodeAccountConsent(raw: String): AccountConsent = runCatching {
    val json = JSONObject(raw)
    AccountConsent(
        collectionEnabled = json.optBoolean("collection_enabled", false),
        cloudAnalysisEnabled = json.optBoolean("cloud_analysis_enabled", false),
        fullContextCloudEnabled = json.optBoolean("full_context_cloud_enabled", false),
    )
}.getOrDefault(AccountConsent())

internal fun commercialAccountConsentDefault(): AccountConsent = AccountConsent()

internal fun legacyOwnerConsent(connection: BackendConnection): AccountConsent = AccountConsent(
    collectionEnabled = connection.enabled,
    cloudAnalysisEnabled = connection.proactiveCloudEnabled,
    fullContextCloudEnabled = connection.proactiveCloudEnabled && !connection.minimizedContextOnly,
)

internal fun accountConsentKey(userId: String, serverOrigin: String = ""): String {
    val normalizedUser = userId.trim()
    val normalizedOrigin = canonicalHttpsOrigin(serverOrigin).orEmpty()
    return if (normalizedOrigin.isEmpty()) {
        "account:${sha256(normalizedUser)}"
    } else {
        "account_v2:${sha256("$normalizedOrigin\u0000$normalizedUser")}"
    }
}

internal fun applyAccountCloudConsent(
    connection: BackendConnection,
    consent: AccountConsent,
): BackendConnection = connection.copy(
    proactiveCloudEnabled = connection.proactiveCloudEnabled && consent.cloudAnalysisEnabled,
    minimizedContextOnly = connection.minimizedContextOnly || !consent.fullContextCloudEnabled,
)

internal fun effectiveCollectionConsent(context: Context): Boolean {
    val connection = authenticatedConnection(context, BackendConnectionStore(context).load()) ?: return false
    return connection.enabled &&
        AccountConsentStore(context).load(connection.userId, connection.sessionOrigin).collectionEnabled
}

private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
    .digest(value.toByteArray(Charsets.UTF_8))
    .joinToString("") { "%02x".format(it) }
