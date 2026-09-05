package com.mouchen.app.connections

import android.content.Context
import com.mouchen.app.collectors.MailAuthMode
import com.mouchen.app.collectors.MailConnectionConfig
import com.mouchen.app.collectors.MailConnectionStore
import com.mouchen.app.collectors.RssFeedConfig
import com.mouchen.app.collectors.RssFeedStore
import com.mouchen.app.models.DeviceModelCapability
import com.mouchen.app.models.DeviceModelCapabilityProbe
import com.mouchen.app.models.OllamaCapability
import com.mouchen.app.models.OllamaConnection
import com.mouchen.app.models.OllamaConnectionStore
import com.mouchen.app.models.PrivateOllamaClient
import com.mouchen.app.sync.AudioProcessingLocation
import com.mouchen.app.sync.AuthSessionStore
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.UrgentEventSyncWorker
import com.mouchen.app.sync.clearAccountLocalData
import com.mouchen.app.sync.invalidateAuthenticatedExecution
import com.mouchen.app.sync.isSecureBackendUrl
import com.mouchen.app.sync.sameHttpsOrigin
import com.mouchen.app.sync.withAuthStateTransition
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

data class ConnectionSettingsForm(
    val backendUrl: String,
    val backendUser: String,
    val backendToken: String,
    val backendEnabled: Boolean,
    val backendMinimizedContextOnly: Boolean,
    val backendProactiveCloud: Boolean,
    val sttBaseUrl: String,
    val sttProcessingLocation: AudioProcessingLocation,
    val ollamaUrl: String,
    val ollamaModel: String,
    val ollamaToken: String,
    val ollamaEnabled: Boolean,
    val rssUrl: String,
    val rssDomain: String,
    val rssKeywords: String,
    val rssThreats: Boolean,
    val rssEnabled: Boolean,
    val imapHost: String,
    val imapPort: String,
    val imapEmail: String,
    val imapDomain: String,
    val imapUsername: String,
    val imapSecret: String,
    val imapAuthMode: MailAuthMode,
    val imapTls: Boolean,
    val imapEnabled: Boolean,
    val deviceCapability: DeviceModelCapability,
)

class ConnectionSettingsRepository(context: Context) {
    private val appContext = context.applicationContext
    private val backendStore = BackendConnectionStore(appContext)
    private val ollamaStore = OllamaConnectionStore(appContext)
    private val rssStore = RssFeedStore(appContext)
    private val mailStore = MailConnectionStore(appContext)

    suspend fun load(): ConnectionSettingsForm = withContext(Dispatchers.IO) {
        val backend = backendStore.load()
        val ollama = ollamaStore.load()
        val rss = rssStore.load().firstOrNull()
        val mail = mailStore.load().firstOrNull()
        ConnectionSettingsForm(
            backendUrl = backend.baseUrl,
            backendUser = backend.userId,
            backendToken = backend.bearerToken.orEmpty(),
            backendEnabled = backend.enabled,
            backendMinimizedContextOnly = backend.minimizedContextOnly,
            backendProactiveCloud = backend.proactiveCloudEnabled,
            sttBaseUrl = backend.sttBaseUrl.orEmpty(),
            sttProcessingLocation = backend.sttProcessingLocation,
            ollamaUrl = ollama?.baseUrl ?: "http://10.0.2.2:11434",
            ollamaModel = ollama?.selectedModel ?: "qwen2.5:7b",
            ollamaToken = ollama?.bearerToken.orEmpty(),
            ollamaEnabled = ollama?.enabled ?: false,
            rssUrl = rss?.url.orEmpty(),
            rssDomain = rss?.domain ?: "事业",
            rssKeywords = rss?.keywords?.joinToString("，").orEmpty(),
            rssThreats = rss?.treatMatchesAsThreats ?: false,
            rssEnabled = rss?.enabled ?: true,
            imapHost = mail?.host.orEmpty(),
            imapPort = (mail?.port ?: 993).toString(),
            imapEmail = mail?.emailAddress.orEmpty(),
            imapDomain = mail?.domain ?: "事业",
            imapUsername = mail?.username.orEmpty(),
            imapSecret = mail?.secret.orEmpty(),
            imapAuthMode = mail?.authMode ?: MailAuthMode.APP_PASSWORD,
            imapTls = mail?.useTls ?: true,
            imapEnabled = mail?.enabled ?: true,
            deviceCapability = DeviceModelCapabilityProbe(appContext).probe(),
        )
    }

    suspend fun saveBackend(form: ConnectionSettingsForm) = withContext(Dispatchers.IO) {
        val requestedUrl = form.backendUrl.trim()
        val requestedSttUrl = form.sttBaseUrl.trim().takeIf(String::isNotEmpty)
        require(isSecureBackendUrl(requestedUrl)) { "Backend URL must use HTTPS" }
        requestedSttUrl?.let {
            require(sameHttpsOrigin(requestedUrl, it)) {
                "Speech-to-text must use the authenticated backend origin"
            }
        }
        withAuthStateTransition {
            val authenticated = backendStore.load()
            val originChanged = !sameHttpsOrigin(authenticated.baseUrl, requestedUrl)
            if (originChanged && (
                    AuthSessionStore(appContext).load() != null || !authenticated.bearerToken.isNullOrBlank()
                )
            ) {
                // Never transplant a bearer token into an editable HTTPS destination. Stop producers
                // and invalidate every in-flight callback before clearing account-owned local state.
                invalidateAuthenticatedExecution(appContext)
                clearAccountLocalData(appContext)
                check(AuthSessionStore(appContext).clear()) { "Unable to clear authenticated session" }
                check(backendStore.clearAuthentication()) { "Unable to clear backend credential" }
            }
            val keepAuthenticatedIdentity = !originChanged
            check(
                backendStore.saveDurably(
                    BackendConnection(
                        baseUrl = requestedUrl,
                        // Account identity is server-owned and may never come from editable form state.
                        userId = authenticated.userId.takeIf { keepAuthenticatedIdentity }.orEmpty(),
                        bearerToken = authenticated.bearerToken.takeIf { keepAuthenticatedIdentity },
                        enabled = form.backendEnabled,
                        minimizedContextOnly = form.backendMinimizedContextOnly,
                        proactiveCloudEnabled = form.backendProactiveCloud,
                        sttBaseUrl = requestedSttUrl,
                        sttProcessingLocation = form.sttProcessingLocation,
                    ),
                ),
            ) { "Unable to persist backend settings" }
        }
        // A corrected URL/token must not wait behind an old authorization failure's backoff.
        RealtimeSyncWorker.enqueueImmediate(appContext, "backend_settings_saved")
        UrgentEventSyncWorker.recover(appContext)
        Unit
    }

    suspend fun saveOllama(form: ConnectionSettingsForm) = withContext(Dispatchers.IO) {
        ollamaStore.save(
            OllamaConnection(
                baseUrl = form.ollamaUrl.trim(),
                selectedModel = form.ollamaModel.trim(),
                bearerToken = form.ollamaToken.trim().takeIf { it.isNotEmpty() },
                enabled = form.ollamaEnabled,
            ),
        )
    }

    suspend fun probeOllama(): OllamaCapability =
        PrivateOllamaClient(ollamaStore).probe()

    suspend fun saveRss(form: ConnectionSettingsForm) = withContext(Dispatchers.IO) {
        val keywords = form.rssKeywords
            .split(Regex("[,，\\n]+"))
            .map(String::trim)
            .filter(String::isNotEmpty)
            .distinct()
        rssStore.save(
            listOf(
                RssFeedConfig(
                    id = "primary",
                    url = form.rssUrl.trim(),
                    domain = form.rssDomain.trim(),
                    keywords = keywords,
                    treatMatchesAsThreats = form.rssThreats,
                    enabled = form.rssEnabled,
                ),
            ),
        )
    }

    suspend fun saveImap(form: ConnectionSettingsForm) = withContext(Dispatchers.IO) {
        val port = form.imapPort.toIntOrNull() ?: error("端口必须是数字")
        mailStore.save(
            listOf(
                MailConnectionConfig(
                    id = "primary",
                    emailAddress = form.imapEmail.trim(),
                    domain = form.imapDomain.trim(),
                    host = form.imapHost.trim(),
                    port = port,
                    username = form.imapUsername.trim(),
                    secret = form.imapSecret,
                    authMode = form.imapAuthMode,
                    useTls = form.imapTls,
                    enabled = form.imapEnabled,
                ),
            ),
        )
    }
}
