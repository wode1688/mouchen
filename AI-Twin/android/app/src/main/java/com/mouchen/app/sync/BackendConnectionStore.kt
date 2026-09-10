package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.BuildConfig
import com.mouchen.app.models.SecureSettingsStore
import java.net.Inet6Address
import java.net.InetAddress
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject

enum class AudioProcessingLocation(val wireValue: String) {
    ON_DEVICE("on_device"),
    TRUSTED_LAN("trusted_lan"),
    PRIVATE_VPS("private_vps"),
    PUBLIC_CLOUD("public_cloud"),
    ;

    companion object {
        fun fromWireValue(value: String): AudioProcessingLocation? =
            entries.firstOrNull { it.wireValue == value }
    }
}

data class BackendConnection(
    val baseUrl: String,
    val userId: String = "demo-user",
    val bearerToken: String? = null,
    val enabled: Boolean = true,
    val minimizedContextOnly: Boolean = true,
    val proactiveCloudEnabled: Boolean = false,
    /** Empty means raw microphone audio is never uploaded for transcription. */
    val sttBaseUrl: String? = null,
    val sttProcessingLocation: AudioProcessingLocation = AudioProcessingLocation.PRIVATE_VPS,
    /** Runtime-only authenticated fence; never serialized into editable connection settings. */
    val sessionEpoch: Long = 0L,
    val sessionOrigin: String = "",
) {
    override fun toString(): String =
        "BackendConnection(baseUrl=$baseUrl, userId=$userId, bearerToken=<redacted>, enabled=$enabled, " +
            "minimizedContextOnly=$minimizedContextOnly, proactiveCloudEnabled=$proactiveCloudEnabled, " +
            "sttBaseUrl=$sttBaseUrl, sttProcessingLocation=${sttProcessingLocation.wireValue})"
}

class BackendConnectionStore(context: Context) {
    private val secureStore = SecureSettingsStore(context, "mouchen_backend_connection")

    fun save(connection: BackendConnection) {
        require(isSecureBackendUrl(connection.baseUrl)) { "Backend URL must use HTTPS" }
        require(connection.bearerToken.isNullOrBlank() || connection.userId.isNotBlank()) {
            "Authenticated connections require a server user ID"
        }
        require(connection.sttProcessingLocation != AudioProcessingLocation.ON_DEVICE) {
            "On-device transcription is not available in this build"
        }
        connection.sttBaseUrl?.let { sttUrl ->
            require(isSecureBackendUrl(sttUrl)) { "Speech-to-text URL must use HTTPS" }
            require(sameHttpsOrigin(connection.baseUrl, sttUrl)) {
                "Speech-to-text must use the authenticated backend origin"
            }
            if (connection.sttProcessingLocation == AudioProcessingLocation.TRUSTED_LAN) {
                require(isPrivateNodeUrl(sttUrl)) { "Trusted network speech-to-text must use a private address" }
            }
        }
        secureStore.put(
            KEY,
            encodeBackendConnection(connection),
        )
    }

    fun saveDurably(connection: BackendConnection): Boolean {
        require(isSecureBackendUrl(connection.baseUrl)) { "Backend URL must use HTTPS" }
        require(connection.bearerToken.isNullOrBlank() || connection.userId.isNotBlank()) {
            "Authenticated connections require a server user ID"
        }
        connection.sttBaseUrl?.let { sttUrl ->
            require(isSecureBackendUrl(sttUrl) && sameHttpsOrigin(connection.baseUrl, sttUrl)) {
                "Speech-to-text must use the authenticated backend origin"
            }
        }
        return secureStore.putDurably(KEY, encodeBackendConnection(connection))
    }

    fun load(): BackendConnection {
        val stored = secureStore.get(KEY) ?: return BackendConnection(BuildConfig.DEFAULT_BACKEND_URL)
        return decodeBackendConnection(stored, BuildConfig.DEFAULT_BACKEND_URL)
    }

    fun clear() = secureStore.remove(KEY)

    /** Updates only server-authenticated identity; the settings page cannot edit these fields. */
    fun saveAuthenticatedIdentity(userId: String, accessToken: String): Boolean {
        require(userId.isNotBlank()) { "Server user ID is required" }
        require(accessToken.isNotBlank()) { "Access token is required" }
        return saveDurably(load().copy(userId = userId, bearerToken = accessToken, enabled = true))
    }

    /** Logout preserves the destination/privacy choices but removes all usable credentials. */
    fun clearAuthentication(): Boolean {
        val current = load()
        val cleared = current.copy(userId = "", bearerToken = null)
        validate(cleared)
        if (secureStore.putDurably(KEY, encodeBackendConnection(cleared))) return true
        // A failed settings-preserving write must never leave a usable token behind. Removing the
        // whole encrypted record loses only reconnectable preferences and is the fail-closed path.
        return secureStore.removeDurably(KEY)
    }

    private fun validate(connection: BackendConnection) {
        require(isSecureBackendUrl(connection.baseUrl)) { "Backend URL must use HTTPS" }
        require(connection.bearerToken.isNullOrBlank() || connection.userId.isNotBlank()) {
            "Authenticated connections require a server user ID"
        }
    }

    private companion object {
        const val KEY = "connection"
    }
}

internal fun encodeBackendConnection(connection: BackendConnection): String = JSONObject()
    .put("schema_version", 2)
    .put("base_url", connection.baseUrl.trimEnd('/'))
    .put("user_id", connection.userId)
    .put("bearer_token", connection.bearerToken ?: JSONObject.NULL)
    .put("enabled", connection.enabled)
    .put("minimized_context_only", connection.minimizedContextOnly)
    .put("proactive_cloud_enabled", connection.proactiveCloudEnabled)
    .put("stt_base_url", connection.sttBaseUrl?.trimEnd('/') ?: JSONObject.NULL)
    .put("stt_processing_location", connection.sttProcessingLocation.wireValue)
    .toString()

/**
 * Legacy private-node configurations used the main backend for STT. Keep that behavior only when
 * the old destination passes the previous private-HTTPS rule, and conservatively classify it as a
 * private VPS so an unknown VPN endpoint is never described as on-device or local-only.
 */
internal fun decodeBackendConnection(stored: String, fallbackBaseUrl: String): BackendConnection = runCatching {
    val json = JSONObject(stored)
    val baseUrl = json.getString("base_url").trimEnd('/')
    val hasCompleteSttSchema = json.has("stt_base_url") && json.has("stt_processing_location")
    val requestedLocation = if (hasCompleteSttSchema) {
        AudioProcessingLocation.fromWireValue(json.optString("stt_processing_location"))
    } else {
        AudioProcessingLocation.PRIVATE_VPS
    }
    val requestedSttUrl = when {
        hasCompleteSttSchema && !json.isNull("stt_base_url") ->
            json.optString("stt_base_url").trim().trimEnd('/').takeIf(String::isNotEmpty)
        hasCompleteSttSchema -> null
        isPrivateNodeUrl(baseUrl) -> baseUrl
        else -> null
    }
    val validSttConfiguration = requestedLocation != null &&
        requestedLocation != AudioProcessingLocation.ON_DEVICE &&
        (requestedSttUrl == null || isSecureBackendUrl(requestedSttUrl)) &&
        (requestedSttUrl == null || sameHttpsOrigin(baseUrl, requestedSttUrl)) &&
        (requestedLocation != AudioProcessingLocation.TRUSTED_LAN ||
            requestedSttUrl == null || isPrivateNodeUrl(requestedSttUrl))

    BackendConnection(
        baseUrl = baseUrl,
        userId = json.optString("user_id", "demo-user"),
        bearerToken = if (json.isNull("bearer_token")) null else json.optString("bearer_token").takeIf { it.isNotBlank() },
        // Disable legacy cleartext configurations instead of silently transmitting credentials
        // and owner context over the LAN after an upgrade.
        enabled = json.optBoolean("enabled", true) && isSecureBackendUrl(baseUrl),
        minimizedContextOnly = json.optBoolean("minimized_context_only", true),
        proactiveCloudEnabled = json.optBoolean("proactive_cloud_enabled", false),
        sttBaseUrl = requestedSttUrl.takeIf { validSttConfiguration },
        sttProcessingLocation = requestedLocation ?: AudioProcessingLocation.PRIVATE_VPS,
    )
}.getOrElse {
    BackendConnection(baseUrl = fallbackBaseUrl, enabled = false)
}

internal fun isSecureBackendUrl(value: String): Boolean =
    value.trim().toHttpUrlOrNull()?.scheme == "https"

internal fun isPrivateNodeUrl(value: String): Boolean {
    val url = value.toHttpUrlOrNull() ?: return false
    if (url.scheme != "https") return false
    val host = url.host.lowercase()
    if (host == "localhost" || host == "::1" || host.endsWith(".local") ||
        host.endsWith(".lan") || host.endsWith(".home")
    ) return true
    val labels = host.split('.')
    val octets = if (labels.size == 4) labels.map(String::toIntOrNull) else emptyList()
    if (octets.size == 4 && octets.all { it != null && it in 0..255 }) {
        val first = octets[0]!!
        val second = octets[1]!!
        return first == 10 || first == 127 ||
            (first == 172 && second in 16..31) ||
            (first == 192 && second == 168) ||
            (first == 169 && second == 254) ||
            (first == 100 && second in 64..127)
    }
    return isPrivateIpv6Literal(host)
}

private fun isPrivateIpv6Literal(host: String): Boolean {
    // A colon is mandatory before parsing so a public DNS name can never reach InetAddress and be
    // accepted merely because it starts with the letters "fc" or "fd".
    if (!host.contains(':')) return false
    val address = runCatching { InetAddress.getByName(host) }.getOrNull() as? Inet6Address
        ?: return false
    val bytes = address.address
    val first = bytes[0].toInt() and 0xff
    val second = bytes[1].toInt() and 0xff
    val uniqueLocal = (first and 0xfe) == 0xfc // fc00::/7
    val linkLocal = first == 0xfe && (second and 0xc0) == 0x80 // fe80::/10
    return uniqueLocal || linkLocal
}
