package com.mouchen.app.relay

import android.content.Context
import com.mouchen.app.models.SecureSettingsStore
import java.util.UUID
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject

internal data class RelaySettings(
    val url: String = "",
    val device: String = "",
    val peer: String = "",
    val token: String = "",
    val enabled: Boolean = false,
    val revision: String = UUID.randomUUID().toString(),
) {
    val partition: String get() {
        val origin = url.toHttpUrlOrNull()?.toString()?.trimEnd('/') ?: url
        return sha256("$origin\n$device\n$peer".toByteArray())
    }
    override fun toString(): String = "RelaySettings(enabled=$enabled, credentials=<redacted>)"
}

internal fun validateSettings(settings: RelaySettings) {
    val url = settings.url.toHttpUrlOrNull() ?: error("服务器地址无效")
    require(url.scheme == "https" && url.username.isEmpty() && url.password.isEmpty() &&
        url.encodedPath == "/" && url.query == null && url.fragment == null) { "请输入不带路径的 HTTPS 地址" }
    val devicePattern = Regex("[A-Za-z0-9_-]{1,48}")
    require(devicePattern.matches(settings.device) && devicePattern.matches(settings.peer)) { "请填写手机和电脑的设备名称" }
    require(settings.device != settings.peer) { "手机与电脑必须使用不同的设备名称" }
    require(settings.token.length in 16..249 && settings.token.all { it in '!'..'~' }) { "请填写有效的设备令牌" }
}

internal fun decodeImportedSettings(text: String): RelaySettings {
    require(text.toByteArray().size <= 16 * 1024) { "配置文件过大" }
    val json = JSONObject(text)
    return RelaySettings(
        url = json.getString("url").trim().trimEnd('/'),
        device = json.optString("device_id", json.optString("device")).trim(),
        peer = json.optString("peer_device_id").ifBlank { json.optJSONArray("targets")?.optString(0).orEmpty() }.trim(),
        token = json.getString("token").trim(),
        enabled = false,
    )
}

internal class RelaySettingsStore(context: Context) {
    private val store = SecureSettingsStore(context, "mouchen_relay_mode")
    fun load(): RelaySettings? = store.get("settings")?.let {
        runCatching {
            val json = JSONObject(it)
            decodeImportedSettings(it).copy(enabled = json.optBoolean("enabled"), revision = json.getString("revision"))
        }.getOrNull()
    }
    fun save(settings: RelaySettings): RelaySettings = synchronized(LOCK) {
        validateSettings(settings)
        val next = settings.copy(revision = UUID.randomUUID().toString())
        val json = JSONObject().put("url", next.url).put("device_id", next.device)
            .put("peer_device_id", next.peer).put("token", next.token).put("enabled", next.enabled)
            .put("revision", next.revision)
        check(store.putDurably("settings", json.toString())) { "无法保存本机配置" }
        next
    }
    fun requireCurrent(settings: RelaySettings) {
        val current = load()
        check(current?.enabled == true && current.revision == settings.revision) { "中转已暂停或连接已改变" }
    }
    companion object { internal val LOCK = Any() }
}
