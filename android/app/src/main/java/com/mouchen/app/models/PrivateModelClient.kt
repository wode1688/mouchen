package com.mouchen.app.models

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import com.mouchen.app.network.MouchenDns
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.authenticatedConnection
import com.mouchen.app.sync.enforceSessionAuthorization
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

data class OllamaConnection(
    val baseUrl: String,
    val selectedModel: String,
    val bearerToken: String? = null,
    val enabled: Boolean = true,
) {
    override fun toString(): String =
        "OllamaConnection(baseUrl=$baseUrl, selectedModel=$selectedModel, bearerToken=<redacted>, enabled=$enabled)"
}

class OllamaConnectionStore(context: Context) {
    private val secureStore = SecureSettingsStore(context, "mouchen_ollama_connection")

    fun save(connection: OllamaConnection) {
        require(connection.baseUrl.startsWith("http://") || connection.baseUrl.startsWith("https://")) {
            "Ollama URL must use HTTP or HTTPS"
        }
        require(connection.selectedModel.isNotBlank()) { "An Ollama model must be selected" }
        secureStore.put(
            KEY,
            JSONObject()
                .put("base_url", connection.baseUrl.trimEnd('/'))
                .put("selected_model", connection.selectedModel)
                .put("bearer_token", connection.bearerToken ?: JSONObject.NULL)
                .put("enabled", connection.enabled)
                .toString(),
        )
    }

    fun load(): OllamaConnection? = secureStore.get(KEY)?.let { stored ->
        runCatching {
            val json = JSONObject(stored)
            OllamaConnection(
                baseUrl = json.getString("base_url").trimEnd('/'),
                selectedModel = json.getString("selected_model"),
                bearerToken = if (json.isNull("bearer_token")) null else json.optString("bearer_token").takeIf(String::isNotBlank),
                enabled = json.optBoolean("enabled", true),
            )
        }.getOrNull()
    }

    fun clear() = secureStore.remove(KEY)

    fun clearDurably(): Boolean = secureStore.removeDurably(KEY)

    private companion object {
        const val KEY = "connection"
    }
}

data class OllamaCapability(
    val reachable: Boolean,
    val availableModels: List<String>,
    val sevenBOrLargerModels: List<String>,
    val selectedModelAvailable: Boolean,
    val reason: String? = null,
)

data class DeviceModelCapability(
    val arm64: Boolean,
    val totalMemoryBytes: Long,
    val suitableForOptional7B: Boolean,
)

class DeviceModelCapabilityProbe(private val context: Context) {
    fun probe(): DeviceModelCapability {
        val memory = ActivityManager.MemoryInfo()
        context.getSystemService(ActivityManager::class.java).getMemoryInfo(memory)
        val arm64 = Build.SUPPORTED_ABIS.any { it.equals("arm64-v8a", ignoreCase = true) }
        return DeviceModelCapability(
            arm64 = arm64,
            totalMemoryBytes = memory.totalMem,
            suitableForOptional7B = arm64 && memory.totalMem >= MIN_7B_MEMORY,
        )
    }

    private companion object {
        const val MIN_7B_MEMORY = 10L * 1024 * 1024 * 1024
    }
}

data class ModelChatRequest(
    val prompt: String,
    val context: JSONObject = JSONObject(),
    val adviceLevel: Int = 2,
    val purpose: String = "proactive_advice",
    val preferPrivateNode: Boolean = true,
    val outboundApproved: Boolean = false,
)

data class ModelReply(
    val provider: String,
    val model: String,
    val content: String,
    val degraded: Boolean,
    val privateFailure: String? = null,
)

class PrivateOllamaClient(
    private val connectionStore: OllamaConnectionStore,
    private val client: OkHttpClient = modelHttpClient(),
) {
    suspend fun probe(): OllamaCapability = withContext(Dispatchers.IO) {
        val connection = connectionStore.load()
            ?: return@withContext OllamaCapability(false, emptyList(), emptyList(), false, "not_configured")
        if (!connection.enabled) return@withContext OllamaCapability(false, emptyList(), emptyList(), false, "disabled")
        runCatching {
            val request = requestBuilder(connection, "${connection.baseUrl}/api/tags").get().build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) error("HTTP ${response.code}")
                val models = JSONObject(response.body?.string().orEmpty()).optJSONArray("models") ?: JSONArray()
                val names = mutableListOf<String>()
                val largeModels = mutableListOf<String>()
                for (index in 0 until models.length()) {
                    val model = models.getJSONObject(index)
                    val name = model.optString("name", model.optString("model"))
                    if (name.isBlank()) continue
                    names += name
                    val parameterSize = model.optJSONObject("details")?.optString("parameter_size").orEmpty()
                    if (isSevenBOrLarger(name, parameterSize)) largeModels += name
                }
                OllamaCapability(true, names, largeModels, connection.selectedModel in names)
            }
        }.getOrElse { OllamaCapability(false, emptyList(), emptyList(), false, it.message ?: "probe_failed") }
    }

    suspend fun chat(request: ModelChatRequest): ModelReply = withContext(Dispatchers.IO) {
        val connection = connectionStore.load() ?: error("Ollama is not configured")
        check(connection.enabled) { "Ollama is disabled" }
        val payload = JSONObject()
            .put("model", connection.selectedModel)
            .put("stream", false)
            .put(
                "messages",
                JSONArray().put(
                    JSONObject()
                        .put("role", "user")
                        .put("content", "${request.prompt}\n\nContext:\n${request.context}"),
                ),
            )
        val httpRequest = requestBuilder(connection, "${connection.baseUrl}/api/chat")
            .post(payload.toString().toRequestBody(JSON))
            .build()
        client.newCall(httpRequest).execute().use { response ->
            if (!response.isSuccessful) error("Ollama returned HTTP ${response.code}")
            val json = JSONObject(response.body?.string().orEmpty())
            val content = json.optJSONObject("message")?.optString("content").orEmpty()
            check(content.isNotBlank()) { "Ollama returned an empty response" }
            ModelReply("ollama", connection.selectedModel, content, degraded = false)
        }
    }

    private fun requestBuilder(connection: OllamaConnection, url: String): Request.Builder =
        Request.Builder().url(url).apply {
            connection.bearerToken?.let { header("Authorization", "Bearer $it") }
        }

    private fun isSevenBOrLarger(name: String, parameterSize: String): Boolean {
        val source = if (parameterSize.isNotBlank()) parameterSize else name
        val match = Regex("(\\d+(?:\\.\\d+)?)\\s*[bB]").find(source) ?: return false
        return (match.groupValues[1].toDoubleOrNull() ?: 0.0) >= 6.5
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()

        fun modelHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .build()
    }
}

class BackendModelClient(
    context: Context,
    private val connectionStore: BackendConnectionStore,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .dns(MouchenDns)
        .enforceSessionAuthorization(context.applicationContext)
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build(),
) {
    private val appContext = context.applicationContext

    suspend fun chat(request: ModelChatRequest, forceLevelOne: Boolean = false): ModelReply = withContext(Dispatchers.IO) {
        val connection = authenticatedConnection(appContext, connectionStore.load())
            ?: error("Authenticated backend session is unavailable")
        check(connection.enabled) { "Backend is disabled" }
        val payload = JSONObject()
            .put("level", "L${if (forceLevelOne) 1 else request.adviceLevel.coerceIn(1, 4)}")
            .put("purpose", request.purpose)
            .put("prompt", request.prompt)
            .put("redacted_context", request.context)
            .put("outbound_approved", if (forceLevelOne) false else request.outboundApproved)
            .put("force_private_7b", false)
        val httpRequest = Request.Builder()
            .url("${connection.baseUrl}/v1/model/analyze")
            .header("X-User-Id", connection.userId)
            .apply { connection.bearerToken?.let { header("Authorization", "Bearer $it") } }
            .post(payload.toString().toRequestBody(JSON))
            .build()
        client.newCall(httpRequest).execute().use { response ->
            if (!response.isSuccessful) error("Backend model route returned HTTP ${response.code}")
            val json = JSONObject(response.body?.string().orEmpty())
            ModelReply(
                provider = json.optString("provider", "template"),
                model = json.optString("model", "deterministic-v1"),
                content = json.optString("content"),
                degraded = json.optBoolean("degraded", forceLevelOne),
            )
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}

/** Private 7B is preferred when configured; failure falls back without making an unapproved cloud call. */
class ModelFallbackRouter(
    private val privateClient: PrivateOllamaClient,
    private val backendClient: BackendModelClient,
) {
    suspend fun chat(request: ModelChatRequest): ModelReply {
        var privateFailure: String? = null
        if (request.preferPrivateNode) {
            runCatching { privateClient.chat(request) }
                .onSuccess { return it }
                .onFailure { privateFailure = it.message ?: "private_model_failed" }
        }
        return runCatching {
            backendClient.chat(request, forceLevelOne = !request.outboundApproved)
                .copy(privateFailure = privateFailure)
        }.getOrElse {
            ModelReply(
                provider = "unavailable",
                model = "none",
                content = "",
                degraded = true,
                privateFailure = privateFailure ?: it.message,
            )
        }
    }
}
