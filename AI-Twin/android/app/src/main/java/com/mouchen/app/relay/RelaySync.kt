package com.mouchen.app.relay

import android.content.Context
import androidx.room.withTransaction
import java.io.ByteArrayOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

internal class RelayHttp(private val settings: RelaySettings, private val current: () -> Unit) {
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(20, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS).callTimeout(90, TimeUnit.SECONDS).build()

    fun bytes(method: String, path: String, body: ByteArray? = null, claim: String? = null): ByteArray {
        current()
        val request = Request.Builder().url(settings.url + path)
            .header("Authorization", "Bearer ${settings.token}")
            .apply { if (claim != null) header("X-Claim-Token", claim) }
            .method(method, if (method in setOf("POST", "PUT")) (body ?: ByteArray(0)).toRequestBody("application/octet-stream".toMediaType()) else null)
            .build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw RelayHttpFailure(response.code)
            val output = ByteArrayOutputStream()
            response.body?.byteStream()?.use { stream ->
                val buffer = ByteArray(8192)
                while (true) {
                    val count = stream.read(buffer)
                    if (count < 0) break
                    check(output.size() + count <= MAX_MESSAGE_BYTES) { "中转响应过大" }
                    output.write(buffer, 0, count)
                }
            }
            current()
            output.toByteArray()
        }
    }

    fun json(method: String, path: String, value: JSONObject? = null): JSONObject {
        current()
        val request = Request.Builder().url(settings.url + path)
            .header("Authorization", "Bearer ${settings.token}")
            .method(method, if (method in setOf("POST", "PUT")) (value?.toString() ?: "{}").toRequestBody("application/json".toMediaType()) else null)
            .build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw RelayHttpFailure(response.code)
            val source = response.body?.source() ?: error("中转响应为空")
            check(!source.request(MAX_MESSAGE_BYTES.toLong() + 1)) { "中转响应过大" }
            current()
            JSONObject(source.readUtf8())
        }
    }
}

internal class RelayHttpFailure(val status: Int) : IllegalStateException("中转请求失败（$status）")

internal class RelaySync(context: Context, private val database: RelayDatabase = RelayDatabase.get(context)) {
    private val app = context.applicationContext
    private val store = RelaySettingsStore(app)
    private val dao = database.dao()
    private var deferredResponses = 0

    suspend fun enqueue(operation: String, body: JSONObject): String = withContext(Dispatchers.IO) {
        val settings = store.load() ?: error("请先保存连接配置")
        validateSettings(settings)
        val envelope = requestEnvelope(settings.device, operation, body)
        val id = envelope.getString("message_id")
        dao.insertRequest(RelayRequest(id, settings.partition, operation, envelope.toString()))
        // The durable request is authoritative even when the OS scheduler is temporarily unavailable.
        runCatching { RelaySyncWorker.enqueue(app) }
        id
    }

    suspend fun sync(): String = MUTEX.withLock {
        withContext(Dispatchers.IO) {
            val settings = store.load() ?: return@withContext "请先保存连接配置"
            if (!settings.enabled) return@withContext "同步已暂停，待发内容保留在手机"
            validateSettings(settings)
            deferredResponses = 0
            val http = RelayHttp(settings) { store.requireCurrent(settings) }
            val status = http.json("GET", "/v1/status")
            check(status.getString("device") == settings.device) { "令牌与手机设备名称不一致" }
            val devices = status.getJSONArray("devices")
            check((0 until devices.length()).any { devices.getString(it) == settings.peer }) { "电脑设备尚未登记在中转服务" }
            var received = receive(settings, http)
            var uploaded = 0
            for (pending in dao.pending(settings.partition, System.currentTimeMillis() - TRANSPORT_TTL_MS)) {
                store.requireCurrent(settings)
                if (needsTransportRetry(pending, System.currentTimeMillis())) {
                    if (send(settings, http, pending)) uploaded++
                }
            }
            received += receive(settings, http)
            "本轮暂存 $uploaded 条，收到电脑回执 $received 条；电脑处理后才算完成" +
                if (deferredResponses > 0) "。另有 $deferredResponses 条回执格式不符，保留云端待排查" else ""
        }
    }

    private suspend fun send(settings: RelaySettings, http: RelayHttp, original: RelayRequest): Boolean {
        var request = original
        val now = System.currentTimeMillis()
        if (request.transportStartedAt == 0L || now - request.transportStartedAt >= TRANSPORT_TTL_MS) {
            request = request.copy(transportKey = UUID.randomUUID().toString(), transportStartedAt = now, uploadedAt = 0)
            dao.updateRequest(request)
        }
        val payload = request.payload.toByteArray(Charsets.UTF_8)
        val spec = JSONObject().put("filename", "${request.id}.json")
            .put("project", RELAY_PROJECT).put("category", "sync-request")
            .put("size", payload.size).put("sha256", sha256(payload))
            .put("targets", JSONArray().put(settings.peer)).put("ttl_hours", 24)
            .put("idempotency_key", request.transportKey).put("metadata", JSONObject())
        var transfer = http.json("POST", "/v1/transfers", spec)
        val id = transfer.getString("id")
        require(Regex("[0-9a-f]{32}").matches(id))
        if (transfer.getString("state") == "uploading") {
            http.bytes("PUT", "/v1/transfers/$id/content", payload)
            transfer = http.json("GET", "/v1/transfers/$id")
        }
        if (transfer.getString("state") in setOf("ready", "delivered")) {
            store.requireCurrent(settings)
            dao.updateRequest(request.copy(uploadedAt = request.transportStartedAt))
            return true
        }
        // An expired upload reservation needs a new transport key; the application ID is stable.
        if (transfer.getString("state") in setOf("expired", "failed")) {
            dao.updateRequest(request.copy(transportStartedAt = 0, uploadedAt = 0))
        }
        return false
    }

    private suspend fun receive(settings: RelaySettings, http: RelayHttp): Int {
        val items = http.json("GET", "/v1/inbox").getJSONArray("items")
        var received = 0
        for (index in 0 until items.length()) {
            val item = items.getJSONObject(index)
            // Other applications can use this device token. Leave their messages untouched.
            if (item.optString("project") != RELAY_PROJECT || item.optString("category") != "sync-response" ||
                item.optString("source") != settings.peer) continue
            val id = item.getString("id")
            if (!Regex("[0-9a-f]{32}").matches(id) || item.getLong("size") !in 1..MAX_MESSAGE_BYTES.toLong()) {
                deferredResponses++
                continue
            }
            val claim = try { http.json("POST", "/v1/transfers/$id/claim") }
                catch (error: RelayHttpFailure) {
                    if (error.status in setOf(409, 410)) continue
                    throw error
                }
            val receipt = JSONObject().put("lease_token", claim.getString("lease_token")).put("sha256", claim.getString("sha256"))
            try {
                require(claim.getString("source") == settings.peer && claim.getString("project") == RELAY_PROJECT &&
                    claim.getString("category") == "sync-response")
                val bytes = http.bytes("GET", "/v1/transfers/$id/content", claim = claim.getString("lease_token"))
                require(bytes.size.toLong() == claim.getLong("size") && sha256(bytes) == claim.getString("sha256"))
                val envelope = JSONObject(bytes.toString(Charsets.UTF_8))
                val request = dao.request(settings.partition, envelope.getString("in_reply_to"))
                    ?: throw IllegalArgumentException("电脑回执不属于当前本机请求")
                val response = validateResponse(bytes, settings.peer, request)
                store.requireCurrent(settings)
                persistResponse(settings.partition, response, sha256(bytes))
                // The transaction has committed before this ACK allows the cloud copy to vanish.
                for (attempt in 0..3) {
                    try {
                        http.json("POST", "/v1/transfers/$id/ack", receipt)
                        break
                    } catch (error: RelayHttpFailure) {
                        // The server may finish its streamed-download receipt just after EOF.
                        if (error.status != 409 || attempt == 3) throw error
                        delay(150L * (attempt + 1))
                    }
                }
                received++
            } catch (error: Exception) {
                runCatching { http.json("POST", "/v1/transfers/$id/release", receipt) }
                if (error is IllegalArgumentException || error is org.json.JSONException || error is java.time.DateTimeException ||
                    error is java.nio.charset.CharacterCodingException) {
                    store.requireCurrent(settings)
                    deferredResponses++
                    continue
                }
                throw error
            }
        }
        return received
    }

    internal suspend fun persistResponse(partition: String, response: ValidatedResponse, digest: String) {
        database.withTransaction {
            val previous = dao.receipt(partition, response.id)
            if (previous != null) {
                require(previous.digest == digest) { "回执内容发生变化" }
                return@withTransaction
            }
            val request = dao.request(partition, response.requestId) ?: error("本机请求不存在")
            // A retry from the computer cannot re-open or overwrite an already applied request.
            if (request.state == "pending") {
                for ((kind, records) in listOf("goal" to response.goals, "advice" to response.advice)) {
                    for (index in 0 until records.length()) {
                        val record = records.getJSONObject(index)
                        val id = record.getString("id")
                        val old = dao.record(partition, kind, id)
                        if (old == null || old.responseAt <= response.createdAt) {
                            dao.putRecord(RelayRecord(partition, kind, id, record.toString(), response.createdAt))
                        }
                    }
                }
                dao.updateRequest(request.copy(state = if (response.ok) "completed" else "rejected", errorCode = response.errorCode))
            }
            dao.insertReceipt(RelayReceipt(partition, response.id, digest))
        }
    }

    companion object { private val MUTEX = Mutex() }
}
