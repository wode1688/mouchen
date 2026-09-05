package com.mouchen.app

import android.Manifest
import android.app.Activity
import android.app.ActivityManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.ChecksSdkIntAtLeast
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text as RawText
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.mouchen.app.connections.ConnectionsScreen
import com.mouchen.app.collectors.CollectionWorker
import com.mouchen.app.collectors.CollectionScheduler
import com.mouchen.app.collectors.hasUsageStatsAccess
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.GoalEntity
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.data.MouchenDatabase
import com.mouchen.app.intake.ConversationImportResult
import com.mouchen.app.intake.ConversationTextImporter
import com.mouchen.app.intake.SharedImageFailure
import com.mouchen.app.intake.SharedImageIntake
import com.mouchen.app.intake.SharedImageIntakeResult
import com.mouchen.app.localization.LOCALE_EN_US
import com.mouchen.app.localization.LOCALE_ZH_CN
import com.mouchen.app.localization.MouchenLocaleProvider
import com.mouchen.app.localization.MouchenLocaleStore
import com.mouchen.app.localization.MouchenText as Text
import com.mouchen.app.localization.normalizeMouchenLocale
import com.mouchen.app.localization.uiText
import com.mouchen.app.models.BackendModelClient
import com.mouchen.app.models.ModelChatRequest
import com.mouchen.app.models.ModelFallbackRouter
import com.mouchen.app.models.ModelReply
import com.mouchen.app.models.OllamaConnectionStore
import com.mouchen.app.models.PrivateOllamaClient
import com.mouchen.app.sync.AdviceFeedbackClient
import com.mouchen.app.sync.AdviceFeedbackKind
import com.mouchen.app.sync.AdviceFeedbackResult
import com.mouchen.app.sync.AdviceAttentionScheduler
import com.mouchen.app.sync.AdviceOutcomeKind
import com.mouchen.app.sync.ACTION_AUTH_STATE_CHANGED
import com.mouchen.app.sync.AccountConsent
import com.mouchen.app.sync.AccountConsentStore
import com.mouchen.app.sync.AccountLocaleClient
import com.mouchen.app.sync.AccountLocaleRequestFence
import com.mouchen.app.sync.AccountLocaleResult
import com.mouchen.app.sync.resolveAccountLocaleRollback
import com.mouchen.app.sync.AuthApiClient
import com.mouchen.app.sync.AuthMode
import com.mouchen.app.sync.AuthResult
import com.mouchen.app.sync.AuthSession
import com.mouchen.app.sync.AuthSessionStore
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.BackendConnectionStore
import com.mouchen.app.sync.LegacyMigrationRuntimeGate
import com.mouchen.app.sync.LatestWinsSerialQueue
import com.mouchen.app.sync.RealtimeSyncWorker
import com.mouchen.app.sync.SessionHealthWorker
import com.mouchen.app.sync.TimingSettings
import com.mouchen.app.sync.TimingSettingsStore
import com.mouchen.app.sync.formatClockMinute
import com.mouchen.app.sync.parseClockMinute
import com.mouchen.app.status.PipelinePhase
import com.mouchen.app.status.PipelineStageStatus
import com.mouchen.app.status.PipelineStatusSnapshot
import com.mouchen.app.status.PipelineStatusStore
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

private const val MOUCHEN_ACCESSIBILITY_SERVICE_CLASS = "com.mouchen.app.collectors.MouchenAccessibilityService"
private const val MOUCHEN_IME_SERVICE_CLASS = "com.mouchen.app.collectors.MouchenImeService"
private const val ACTION_START_AUDIO = "com.mouchen.app.action.START_AUDIO_CAPTURE"
private const val ACTION_STOP_AUDIO = "com.mouchen.app.action.STOP_AUDIO_CAPTURE"
private const val ACTION_START_SCREEN = "com.mouchen.app.action.START_SCREEN_CAPTURE"
private const val ACTION_STOP_SCREEN = "com.mouchen.app.action.STOP_SCREEN_CAPTURE"
private const val EXTRA_RESULT_CODE = "result_code"
private const val EXTRA_RESULT_DATA = "result_data"

private val Ink = Color(0xFF1E2422)
private val Paper = Color(0xFFF5F6F3)
private val Vermilion = Color(0xFF9F2D35)
private val Pine = Color(0xFF356859)
private val Amber = Color(0xFF9A641A)

class MainActivity : ComponentActivity() {
    private lateinit var dao: MouchenDao
    private lateinit var timingSettingsStore: TimingSettingsStore
    private lateinit var captureStatusStore: CaptureStatusStore
    private lateinit var sharedImageIntake: SharedImageIntake
    private lateinit var conversationTextImporter: ConversationTextImporter
    private var permissionRefresh by mutableIntStateOf(0)
    private var audioRunning by mutableStateOf(false)
    private var screenRunning by mutableStateOf(false)
    private var screenCaptureWarning by mutableStateOf("")
    private var timingSettings by mutableStateOf(TimingSettings())
    private var authUiState by mutableStateOf<AuthUiState>(AuthUiState.Checking)
    private var localeTag by mutableStateOf(LOCALE_ZH_CN)
    private var localeRequestGeneration = 0L
    private val localeUpdateQueue = LatestWinsSerialQueue<PendingLocaleUpdate>()
    private var acknowledgedAccountLocale: AcknowledgedAccountLocale? = null
    private var accountConsent by mutableStateOf(AccountConsent())
    private var lastPermissionMask: Int? = null
    private var captureStatusReceiverRegistered = false
    private var authStateReceiverRegistered = false

    private val captureStatusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action == ACTION_CAPTURE_STATUS_CHANGED) refreshCaptureStatus()
        }
    }

    private val authStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != ACTION_AUTH_STATE_CHANGED) return
            if (AuthSessionStore(this@MainActivity).load() == null) {
                invalidateLocaleRequests()
                authUiState = AuthUiState.SignedOut("登录状态已失效，请重新登录")
            }
        }
    }

    private val runtimePermissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { results ->
        permissionRefresh++
        if (results.values.any { it }) {
            CollectionWorker.enqueueImmediate(this, "runtime_permission_granted")
        }
        if (results[Manifest.permission.POST_NOTIFICATIONS] == true) {
            AdviceAttentionScheduler.enqueueNow(this)
        }
        lastPermissionMask = permissionSnapshot(permissionRefresh).backfillMask
    }

    private val projectionPermission = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val data = result.data
        if (!accountConsent.collectionEnabled) {
            captureStatusStore.markScreenAuthorizationRequired("account_collection_consent_required")
            refreshCaptureStatus()
        } else if (result.resultCode == Activity.RESULT_OK && data != null) {
            val service = serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_START_SCREEN)
                .putExtra(EXTRA_RESULT_CODE, result.resultCode)
                .putExtra(EXTRA_RESULT_DATA, data)
            runCatching { ContextCompat.startForegroundService(this, service) }
                .onFailure { refreshCaptureStatus() }
        } else if (::captureStatusStore.isInitialized) {
            captureStatusStore.markScreenAuthorizationRequired("capture_authorization_denied")
        }
        permissionRefresh++
    }

    private val conversationDocument = registerForActivityResult(
        ActivityResultContracts.OpenDocument(),
    ) { uri ->
        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE && uri != null) importConversationDocument(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        dao = MouchenDatabase.get(this).dao()
        timingSettingsStore = TimingSettingsStore(this)
        captureStatusStore = CaptureStatusStore(this)
        sharedImageIntake = SharedImageIntake(this)
        conversationTextImporter = ConversationTextImporter(this)
        localeTag = MouchenLocaleStore(this).loadPreLogin()
        timingSettings = timingSettingsStore.load()
        refreshCaptureStatus()
        val feedbackClient = AdviceFeedbackClient(this, dao)
        val pipelineStatus = PipelineStatusStore(this)
        setContent {
            MouchenLocaleProvider(localeTag) {
              MouchenTheme {
                when (val state = authUiState) {
                    AuthUiState.Checking -> AuthenticationCheckingScreen()
                    is AuthUiState.SignedOut -> AuthenticationScreen(
                        initialBaseUrl = BackendConnectionStore(this@MainActivity).load().baseUrl,
                        initialMessage = state.message,
                        busy = false,
                        onSubmit = ::authenticate,
                        locale = localeTag,
                        onLocaleChange = ::changeLocale,
                    )
                    is AuthUiState.Busy -> AuthenticationScreen(
                        initialBaseUrl = state.baseUrl,
                        initialMessage = state.message,
                        busy = true,
                        onSubmit = ::authenticate,
                        locale = localeTag,
                        onLocaleChange = ::changeLocale,
                    )
                    is AuthUiState.LegacyDecision -> LegacyMigrationDecisionScreen(
                        session = state.session,
                        onClaim = { authUiState = AuthUiState.LegacyClaim(state.session) },
                        onContinueOnce = { continueLegacyForThisSession(state.session) },
                        onExit = ::exitLegacyMigration,
                    )
                    is AuthUiState.LegacyClaim -> LegacyOwnerClaimScreen(
                        session = state.session,
                        message = state.message,
                        busy = state.busy,
                        onBack = { authUiState = AuthUiState.LegacyDecision(state.session) },
                        onSubmit = { mode, username, password, registrationCode ->
                            claimLegacyOwner(state.session, mode, username, password, registrationCode)
                        },
                    )
                    is AuthUiState.SignedIn -> MouchenApp(
                    adviceFlow = dao.observeAdvice(),
                    goalsFlow = dao.observeGoals(),
                    eventFlow = dao.observeRecentEvents(),
                    pipelineStatusFlow = pipelineStatus.observe(),
                    pendingEventsFlow = dao.observeUnsyncedEventCount(),
                    pendingGoalsFlow = dao.observeUnsyncedGoalCount(),
                    permissionSnapshot = permissionSnapshot(permissionRefresh),
                    audioRunning = audioRunning,
                    screenRunning = screenRunning,
                    screenCaptureWarning = screenCaptureWarning,
                    timingSettings = timingSettings,
                    onPermissionAction = ::handlePermissionAction,
                    onAddGoal = { goal ->
                        lifecycleScope.launch {
                            dao.insertGoal(goal)
                            RealtimeSyncWorker.enqueue(this@MainActivity, "goal_created")
                        }
                    },
                    onShareContext = { text ->
                        enqueueOwnerContext(text, "android.self_report", "owner_self_report", true)
                    },
                    onImportConversation = {
                        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
                            conversationDocument.launch(arrayOf("text/plain", "application/octet-stream"))
                        }
                    },
                    onRecordQuestion = { text ->
                        enqueueOwnerContext(text, "android.question", "owner_question", false)
                    },
                    onScanNow = { CollectionWorker.enqueueImmediate(this@MainActivity, "manual_scan") },
                    onSaveQuietHours = { enabled, startMinute, endMinute ->
                        timingSettings = timingSettingsStore.saveQuietHours(enabled, startMinute, endMinute)
                        RealtimeSyncWorker.enqueue(this@MainActivity, "quiet_hours_changed")
                    },
                    onTemporaryDriving = { enabled ->
                        timingSettings = timingSettingsStore.setTemporaryDriving(enabled)
                        RealtimeSyncWorker.enqueue(this@MainActivity, "driving_mode_changed")
                    },
                    onAdviceFeedback = feedbackClient::submit,
                    onAdviceGuidance = feedbackClient::submitGuidance,
                    onAdviceOutcome = feedbackClient::submitOutcome,
                    backendConnectionProvider = {
                        BackendConnectionStore(this@MainActivity).load()
                    },
                    account = state.session,
                    accountConsent = accountConsent,
                    onAccountConsentChange = ::saveAccountConsent,
                    onLogout = ::logout,
                    locale = localeTag,
                    onLocaleChange = ::changeLocale,
                    )
                }
              }
            }
        }
        restoreSession()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        // Preserve the one-shot URI/text grant in the Activity intent until a server session is
        // verified. Signed-out content must never enter an unidentified local tenant.
        if (authUiState is AuthUiState.SignedIn) acceptSharedContent(intent)
    }

    override fun onStart() {
        super.onStart()
        if (!captureStatusReceiverRegistered) {
            ContextCompat.registerReceiver(
                this,
                captureStatusReceiver,
                IntentFilter(ACTION_CAPTURE_STATUS_CHANGED),
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
            captureStatusReceiverRegistered = true
        }
        if (!authStateReceiverRegistered) {
            ContextCompat.registerReceiver(
                this,
                authStateReceiver,
                IntentFilter(ACTION_AUTH_STATE_CHANGED),
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
            authStateReceiverRegistered = true
        }
        refreshCaptureStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshAccountLocale()
        if (::timingSettingsStore.isInitialized) timingSettings = timingSettingsStore.load()
        refreshCaptureStatus()
        permissionRefresh++
        val currentMask = permissionSnapshot(permissionRefresh).backfillMask
        if (shouldCollectAfterPermissionRefresh(lastPermissionMask, currentMask)) {
            CollectionWorker.enqueueImmediate(this, "settings_permission_changed")
        }
        // Returning from system notification settings may have enabled delivery. The worker
        // performs its own permission/channel preflight before consuming a server claim.
        if (authUiState is AuthUiState.SignedIn) {
            AdviceAttentionScheduler.enqueueNow(this)
            SessionHealthWorker.enqueueNow(this)
        }
        lastPermissionMask = currentMask
    }

    override fun onStop() {
        if (captureStatusReceiverRegistered) {
            unregisterReceiver(captureStatusReceiver)
            captureStatusReceiverRegistered = false
        }
        if (authStateReceiverRegistered) {
            unregisterReceiver(authStateReceiver)
            authStateReceiverRegistered = false
        }
        super.onStop()
    }

    private fun handlePermissionAction(action: PermissionAction) {
        when (action) {
            PermissionAction.Calendar -> runtimePermissions.launch(
                arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR),
            )
            PermissionAction.Location -> runtimePermissions.launch(
                arrayOf(Manifest.permission.ACCESS_COARSE_LOCATION),
            )
            PermissionAction.Notifications -> if (Build.VERSION.SDK_INT >= 33) {
                runtimePermissions.launch(arrayOf(Manifest.permission.POST_NOTIFICATIONS))
            } else Unit
            PermissionAction.Microphone -> runtimePermissions.launch(arrayOf(Manifest.permission.RECORD_AUDIO))
            PermissionAction.Contacts -> requestSensitivePermission(Manifest.permission.READ_CONTACTS)
            PermissionAction.Sms -> requestSensitivePermission(Manifest.permission.READ_SMS)
            PermissionAction.CallLog -> requestSensitivePermission(Manifest.permission.READ_CALL_LOG)
            PermissionAction.NotificationAccess -> openSettings(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            PermissionAction.UsageAccess -> openSettings(Settings.ACTION_USAGE_ACCESS_SETTINGS)
            PermissionAction.Accessibility -> openSettings(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            PermissionAction.InputMethod -> openSettings(Settings.ACTION_INPUT_METHOD_SETTINGS)
            PermissionAction.BatteryOptimization -> openBatteryOptimizationSettings()
            PermissionAction.StartAudio -> {
                if (!accountConsent.collectionEnabled) {
                    Toast.makeText(this, this.uiText("请先在权限页开启当前账号的“允许AI替身采集”"), Toast.LENGTH_LONG).show()
                    return
                }
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    runtimePermissions.launch(arrayOf(Manifest.permission.RECORD_AUDIO))
                    return
                }
                runCatching {
                    ContextCompat.startForegroundService(
                        this,
                        serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_START_AUDIO),
                    )
                }.onFailure { refreshCaptureStatus() }
            }
            PermissionAction.StopAudio -> {
                runCatching {
                    startService(serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_STOP_AUDIO))
                }.onFailure { refreshCaptureStatus() }
            }
            PermissionAction.StartScreen -> {
                if (!accountConsent.collectionEnabled) {
                    Toast.makeText(this, this.uiText("请先在权限页开启当前账号的“允许AI替身采集”"), Toast.LENGTH_LONG).show()
                    return
                }
                val manager = getSystemService(MediaProjectionManager::class.java)
                val captureIntent = if (usesDefaultDisplayProjectionConfig(Build.VERSION.SDK_INT)) {
                    manager.createScreenCaptureIntent(
                        MediaProjectionConfig.createConfigForDefaultDisplay(),
                    )
                } else {
                    manager.createScreenCaptureIntent()
                }
                projectionPermission.launch(captureIntent)
            }
            PermissionAction.StopScreen -> {
                runCatching {
                    startService(serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_STOP_SCREEN))
                }.onFailure { refreshCaptureStatus() }
            }
        }
    }

    private fun restoreSession() {
        invalidateLocaleRequests()
        authUiState = AuthUiState.Checking
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { AuthApiClient(this@MainActivity).restore() }
            when (result) {
                is AuthResult.Success -> {
                    if (LegacyMigrationRuntimeGate.requiresDecision(result.session)) {
                        authUiState = AuthUiState.LegacyDecision(result.session)
                    } else {
                        completeAuthentication(result.session, migrated = true)
                    }
                }
                is AuthResult.Failure -> {
                    if (result.unauthorized) {
                        runCatching { stopService(serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_STOP_AUDIO)) }
                        runCatching { stopService(serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_STOP_SCREEN)) }
                        withContext(Dispatchers.IO) {
                            AuthSessionStore(this@MainActivity).clear()
                            BackendConnectionStore(this@MainActivity).clearAuthentication()
                        }
                    }
                    authUiState = AuthUiState.SignedOut(result.message)
                }
            }
        }
    }

    private fun continueLegacyForThisSession(expected: AuthSession) {
        val current = AuthSessionStore(this).load()
        if (current == null || current != expected || !LegacyMigrationRuntimeGate.allowForThisProcess(current)) {
            authUiState = AuthUiState.SignedOut("旧主人会话已经变化，请重新打开应用确认")
            return
        }
        completeAuthentication(current, migrated = true)
    }

    private fun exitLegacyMigration() {
        invalidateLocaleRequests()
        LegacyMigrationRuntimeGate.reset()
        runCatching { stopService(serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_STOP_AUDIO)) }
        runCatching { stopService(serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_STOP_SCREEN)) }
        finishAndRemoveTask()
    }

    private fun claimLegacyOwner(
        expected: AuthSession,
        mode: AuthMode,
        username: String,
        password: String,
        registrationCode: String,
    ) {
        val currentState = authUiState as? AuthUiState.LegacyClaim ?: return
        if (currentState.busy || currentState.session != expected) return
        invalidateLocaleRequests()
        authUiState = currentState.copy(message = "正在安全领取主人账号…", busy = true)
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                AuthApiClient(this@MainActivity).claimLegacyOwner(
                    mode = mode,
                    username = username,
                    password = password,
                    expectedLegacy = expected,
                    registrationCode = registrationCode,
                )
            }
            when (result) {
                is AuthResult.Success -> completeAuthentication(result.session, migrated = false)
                is AuthResult.Failure -> authUiState = AuthUiState.LegacyClaim(
                    session = expected,
                    message = result.message,
                    busy = false,
                )
            }
        }
    }

    private fun authenticate(
        mode: AuthMode,
        username: String,
        password: String,
        baseUrl: String,
        registrationCode: String,
    ) {
        if (authUiState is AuthUiState.Busy) return
        invalidateLocaleRequests()
        authUiState = AuthUiState.Busy(baseUrl, if (mode == AuthMode.LOGIN) "正在登录…" else "正在注册…")
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) {
                AuthApiClient(this@MainActivity).authenticate(
                    mode,
                    username,
                    password,
                    baseUrl,
                    registrationCode,
                )
            }
            when (result) {
                is AuthResult.Success -> completeAuthentication(result.session, migrated = false)
                is AuthResult.Failure -> authUiState = AuthUiState.SignedOut(result.message)
            }
        }
    }

    private fun completeAuthentication(session: AuthSession, migrated: Boolean) {
        invalidateLocaleRequests()
        val authoritativeLocale = normalizeMouchenLocale(session.locale) ?: LOCALE_ZH_CN
        MouchenLocaleStore(this).saveAccount(session.userId, authoritativeLocale)
        localeTag = authoritativeLocale
        acknowledgedAccountLocale = AcknowledgedAccountLocale(session, authoritativeLocale)
        accountConsent = AccountConsentStore(this).load(session.userId, session.serverOrigin)
        authUiState = AuthUiState.SignedIn(session)
        acceptSharedContent(intent)
        CollectionScheduler.ensurePeriodic(this)
        CollectionWorker.enqueueImmediate(this, if (migrated) "session_restored" else "account_login")
        RealtimeSyncWorker.enqueueImmediate(this, if (migrated) "session_restored" else "account_login")
        AdviceAttentionScheduler.enqueueNow(this)
        refreshAccountLocale()
    }

    private fun refreshAccountLocale() {
        val signedIn = authUiState as? AuthUiState.SignedIn ?: return
        if (localeUpdateQueue.hasWork()) return
        val fence = AccountLocaleRequestFence.capture(localeRequestGeneration, signedIn.session)
        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { AccountLocaleClient(this@MainActivity).fetch() }
            if (result !is AccountLocaleResult.Success) return@launch
            val current = currentLocaleSession(fence) ?: return@launch
            val updated = AuthSessionStore(this@MainActivity)
                .updateLocaleIfSessionMatches(signedIn.session, result.locale)
                ?: return@launch
            if (currentLocaleSession(fence) == null) return@launch
            if (!MouchenLocaleStore(this@MainActivity).saveAccount(current.userId, result.locale)) return@launch
            if (currentLocaleSession(fence) == null) return@launch
            acknowledgedAccountLocale = AcknowledgedAccountLocale(updated, result.locale)
            localeTag = result.locale
            authUiState = AuthUiState.SignedIn(updated)
        }
    }

    private fun changeLocale(requested: String) {
        val normalized = normalizeMouchenLocale(requested) ?: return
        if (normalized == localeTag) return
        val signedIn = authUiState as? AuthUiState.SignedIn
        if (signedIn == null) {
            if (MouchenLocaleStore(this).savePreLogin(normalized)) localeTag = normalized
            return
        }
        localeRequestGeneration++
        val fence = AccountLocaleRequestFence.capture(localeRequestGeneration, signedIn.session)
        localeTag = normalized
        MouchenLocaleStore(this).saveAccount(signedIn.session.userId, normalized)
        localeUpdateQueue.enqueue(
            PendingLocaleUpdate(
                locale = normalized,
                session = signedIn.session,
                fence = fence,
            ),
        )?.let(::launchLocaleUpdate)
    }

    private fun launchLocaleUpdate(request: PendingLocaleUpdate) {
        lifecycleScope.launch {
            try {
                when (val result = withContext(Dispatchers.IO) {
                    AccountLocaleClient(this@MainActivity).update(request.locale, request.session)
                }) {
                    is AccountLocaleResult.Success -> {
                        val authoritative = result.locale
                        val updated = AuthSessionStore(this@MainActivity)
                            .updateLocaleIfSessionMatches(request.session, authoritative)
                            ?: return@launch
                        acknowledgedAccountLocale = AcknowledgedAccountLocale(updated, authoritative)
                        val current = currentLocaleSession(request.fence) ?: return@launch
                        if (currentLocaleSession(request.fence) == null) return@launch
                        if (!MouchenLocaleStore(this@MainActivity).saveAccount(current.userId, authoritative)) {
                            return@launch
                        }
                        if (currentLocaleSession(request.fence) == null) return@launch
                        localeTag = authoritative
                        authUiState = AuthUiState.SignedIn(updated)
                    }
                    is AccountLocaleResult.Failure -> {
                        val current = currentLocaleSession(request.fence) ?: return@launch
                        val rollbackLocale = resolveAccountLocaleRollback(
                            requestSession = request.session,
                            acknowledgedSession = acknowledgedAccountLocale?.session,
                            acknowledgedLocale = acknowledgedAccountLocale?.locale,
                        )
                        val updated = AuthSessionStore(this@MainActivity)
                            .updateLocaleIfSessionMatches(request.session, rollbackLocale)
                            ?: return@launch
                        localeTag = rollbackLocale
                        MouchenLocaleStore(this@MainActivity).saveAccount(current.userId, rollbackLocale)
                        if (currentLocaleSession(request.fence) == null) return@launch
                        authUiState = AuthUiState.SignedIn(updated)
                        Toast.makeText(
                            this@MainActivity,
                            this@MainActivity.uiText("语言未能同步到账号，请检查网络后重试"),
                            Toast.LENGTH_LONG,
                        ).show()
                    }
                }
            } finally {
                localeUpdateQueue.complete()?.let(::launchLocaleUpdate)
            }
        }
    }

    private fun saveAccountConsent(next: AccountConsent) {
        val session = (authUiState as? AuthUiState.SignedIn)?.session ?: return
        if (!AccountConsentStore(this).save(session.userId, next, session.serverOrigin)) return
        accountConsent = next
        if (next.collectionEnabled) {
            CollectionWorker.enqueueImmediate(this, "account_collection_consent_enabled")
        } else {
            runCatching { startService(serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_STOP_AUDIO)) }
            runCatching { startService(serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_STOP_SCREEN)) }
        }
        RealtimeSyncWorker.enqueueImmediate(this, "account_consent_changed")
    }

    private fun logout() {
        if (authUiState !is AuthUiState.SignedIn) return
        invalidateLocaleRequests()
        authUiState = AuthUiState.Checking
        runCatching { startService(serviceIntent(AUDIO_CAPTURE_SERVICE_CLASS, ACTION_STOP_AUDIO)) }
        runCatching { startService(serviceIntent(SCREEN_CAPTURE_SERVICE_CLASS, ACTION_STOP_SCREEN)) }
        lifecycleScope.launch {
            val remoteRevoked = withContext(Dispatchers.IO) { AuthApiClient(this@MainActivity).logout() }
            localeTag = MouchenLocaleStore(this@MainActivity).loadPreLogin()
            authUiState = AuthUiState.SignedOut(
                if (remoteRevoked) "已安全退出" else "安全退出未完全确认；请联网后重新打开应用再试",
            )
        }
    }

    private fun invalidateLocaleRequests() {
        localeRequestGeneration++
        localeUpdateQueue.discardPending()
        acknowledgedAccountLocale = null
    }

    private fun currentLocaleSession(fence: AccountLocaleRequestFence): AuthSession? {
        val current = (authUiState as? AuthUiState.SignedIn)?.session
        return current?.takeIf { fence.matches(localeRequestGeneration, it) }
    }

    private fun refreshCaptureStatus() {
        if (!::captureStatusStore.isInitialized) return
        val activeServices = runningOwnServiceClassNames()
        val status = if (activeServices == null) {
            captureStatusStore.snapshot()
        } else {
            captureStatusStore.reconcile(activeServices)
        }
        audioRunning = status.audioRunning
        screenRunning = status.screenRunning
        screenCaptureWarning = if (status.screenAuthorizationRequired) status.screenStatusDetail else ""
    }

    @Suppress("DEPRECATION")
    private fun runningOwnServiceClassNames(): Set<String>? = runCatching {
        getSystemService(ActivityManager::class.java)
            .getRunningServices(Int.MAX_VALUE)
            .asSequence()
            .map { it.service }
            .filter { it.packageName == packageName }
            .map { it.className }
            .toSet()
    }.getOrNull()

    private fun acceptSharedContent(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        when {
            intent.type?.substringBefore(';') == "text/plain" -> {
                val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()?.trim().orEmpty()
                intent.removeExtra(Intent.EXTRA_TEXT)
                if (text.isNotEmpty()) {
                    enqueueOwnerContext(text, "android.shared_text", "owner_self_report", true)
                }
            }
            BuildConfig.ALLOW_SENSITIVE_CAPTURE && intent.type?.startsWith("image/") == true -> {
                acceptSharedImage(intent)
            }
        }
    }

    private fun acceptSharedImage(intent: Intent) {
        val mimeType = intent.type
        val uri = intent.sharedStreamUri() ?: intent.clipData?.getItemAt(0)?.uri
        // ACTION_SEND grants are intentionally consumed only for this immediate read. The app
        // never asks to persist the grant and never stores the source URI.
        intent.removeExtra(Intent.EXTRA_STREAM)
        intent.clipData = null
        if (uri == null) {
            showIntakeMessage("没有收到可读取的图片")
            return
        }
        (application as MouchenApplication).intakeScope.launch {
            when (val result = sharedImageIntake.process(uri, mimeType)) {
                is SharedImageIntakeResult.Success -> {
                    val event = LocalEventEntity(
                        source = "android.shared_image",
                        type = "ui.visible_text",
                        sensitivity = "restricted",
                        payloadJson = JSONObject()
                            .put("visible_text", JSONArray(result.lines))
                            .put("context", "shared_image")
                            .put("analysis_requested", true)
                            .put("ocr_engine", result.ocrEngine)
                            .put("ocr_local", true)
                            .put("character_count", result.characterCount)
                            .put("source_bytes", result.byteCount)
                            .put("source_mime", result.mimeType ?: "image/*")
                            .toString(),
                    )
                    try {
                        dao.insertEvent(event)
                        val queued = RealtimeSyncWorker.enqueueForEvent(applicationContext, event)
                        showIntakeMessage(
                            if (queued) "图片文字已在手机识别并请求同步"
                            else "图片文字已保存，当前未能排队同步",
                        )
                    } catch (_: Exception) {
                        showIntakeMessage("图片文字未能保存，请重试")
                    }
                }
                is SharedImageIntakeResult.Failure -> showIntakeMessage(
                    sharedImageFailureMessage(result.reason),
                )
            }
        }
    }

    private fun importConversationDocument(uri: Uri) {
        if (!BuildConfig.ALLOW_SENSITIVE_CAPTURE ||
            !::conversationTextImporter.isInitialized || !::dao.isInitialized
        ) return
        (application as MouchenApplication).intakeScope.launch {
            when (val result = conversationTextImporter.read(uri)) {
                is ConversationImportResult.Success -> {
                    val importId = UUID.randomUUID().toString()
                    val events = result.chunks.mapIndexed { index, chunk ->
                        LocalEventEntity(
                            source = "android.conversation_import",
                            type = "ui.visible_text",
                            sensitivity = "restricted",
                            payloadJson = JSONObject()
                                .put("visible_text", JSONArray().put(chunk))
                                .put("context", "conversation_import")
                                .put("analysis_requested", true)
                                .put("import_id", importId)
                                .put("chunk_index", index + 1)
                                .put("chunk_count", result.chunks.size)
                                .put("source_bytes", result.byteCount)
                                .put("source_mime", result.mimeType ?: "text/plain")
                                .toString(),
                        )
                    }
                    try {
                        dao.insertEvents(events)
                        val queued = RealtimeSyncWorker.enqueueImmediate(applicationContext, "conversation_import")
                        showIntakeMessage(
                            if (queued) "已导入 ${events.size} 段对话文本并请求同步"
                            else "已导入 ${events.size} 段对话文本，当前未能排队同步",
                        )
                    } catch (_: Exception) {
                        showIntakeMessage("对话文本未能保存，请重试")
                    }
                }
                ConversationImportResult.UnsupportedUri -> showIntakeMessage("只支持从系统文件选择器导入")
                ConversationImportResult.UnsupportedDocument -> showIntakeMessage("请选择 text/plain 或 .txt 文件")
                ConversationImportResult.TooLarge -> showIntakeMessage("文件过大；最多 512 KB、8 万字、24 段")
                ConversationImportResult.Empty -> showIntakeMessage("文件中没有可导入的文字")
                ConversationImportResult.InvalidUtf8 -> showIntakeMessage("文件不是有效的 UTF-8 文本")
                ConversationImportResult.ReadDenied -> showIntakeMessage("系统没有授予读取权限，请重新选择")
                ConversationImportResult.ReadFailed -> showIntakeMessage("文件读取失败，请重试")
            }
        }
    }

    private fun enqueueOwnerContext(
        text: String,
        source: String,
        contextKind: String,
        analysisRequested: Boolean,
    ) {
        val value = text.trim().take(4_000)
        if (value.isEmpty() || !::dao.isInitialized) return
        lifecycleScope.launch {
            val event = LocalEventEntity(
                source = source,
                type = "ui.visible_text",
                sensitivity = "sensitive",
                payloadJson = JSONObject()
                    .put("visible_text", JSONArray().put(value))
                    .put("context", contextKind)
                    .put("analysis_requested", analysisRequested)
                    .toString(),
            )
            dao.insertEvent(event)
            RealtimeSyncWorker.enqueueForEvent(this@MainActivity, event)
        }
    }

    private fun serviceIntent(className: String, action: String) =
        Intent().setComponent(ComponentName(this, className)).setAction(action)

    private fun openSettings(action: String) {
        runCatching { startActivity(Intent(action)) }.onFailure {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).setData(Uri.parse("package:$packageName")),
            )
        }
    }

    private fun openBatteryOptimizationSettings() {
        runCatching { startActivity(batteryOptimizationSettingsIntent()) }.onFailure {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).setData(Uri.parse("package:$packageName")),
            )
        }
    }

    private fun showIntakeMessage(message: String) {
        runOnUiThread { Toast.makeText(applicationContext, applicationContext.uiText(message), Toast.LENGTH_LONG).show() }
    }

    @Suppress("DEPRECATION")
    private fun Intent.sharedStreamUri(): Uri? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
    } else {
        getParcelableExtra(Intent.EXTRA_STREAM)
    }

    private fun requestSensitivePermission(permission: String) {
        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
            runtimePermissions.launch(arrayOf(permission))
        }
    }

    private fun permissionSnapshot(refresh: Int): PermissionSnapshot {
        refresh.hashCode()
        val calendar = hasPermission(Manifest.permission.READ_CALENDAR)
        val location = hasPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
        val notifications = Build.VERSION.SDK_INT < 33 || hasPermission(Manifest.permission.POST_NOTIFICATIONS)
        return PermissionSnapshot(
            calendar = calendar,
            location = location,
            notifications = notifications,
            notificationAccess = NotificationManagerCompat.getEnabledListenerPackages(this).contains(packageName),
            usageAccess = hasUsageStatsAccess(this),
            microphone = hasPermission(Manifest.permission.RECORD_AUDIO),
            contacts = BuildConfig.ALLOW_SENSITIVE_CAPTURE && hasPermission(Manifest.permission.READ_CONTACTS),
            sms = BuildConfig.ALLOW_SENSITIVE_CAPTURE && hasPermission(Manifest.permission.READ_SMS),
            callLog = BuildConfig.ALLOW_SENSITIVE_CAPTURE && hasPermission(Manifest.permission.READ_CALL_LOG),
            accessibility = enabledAccessibilityServices().contains(
                ComponentName(this, MOUCHEN_ACCESSIBILITY_SERVICE_CLASS).flattenToString(),
            ),
            inputMethod = enabledInputMethods().any {
                it.serviceInfo.packageName == packageName &&
                    it.serviceInfo.name == MOUCHEN_IME_SERVICE_CLASS
            },
            batteryOptimizationIgnored = runCatching {
                getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
            }.getOrDefault(false),
        )
    }

    private fun hasPermission(permission: String) =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun enabledAccessibilityServices(): Set<String> =
        Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            ?.split(':')
            ?.toSet()
            .orEmpty()

    private fun enabledInputMethods() =
        getSystemService(InputMethodManager::class.java).enabledInputMethodList
}

private data class PendingLocaleUpdate(
    val locale: String,
    val session: AuthSession,
    val fence: AccountLocaleRequestFence,
)

private data class AcknowledgedAccountLocale(
    val session: AuthSession,
    val locale: String,
)

private enum class Screen(val label: String) {
    Situation("局势"), Advice("谏言"), Goals("目标"), Connections("连接"), Permissions("权限"),
}

private sealed interface AuthUiState {
    data object Checking : AuthUiState
    data class Busy(val baseUrl: String, val message: String) : AuthUiState
    data class SignedOut(val message: String = "") : AuthUiState
    data class LegacyDecision(val session: AuthSession) : AuthUiState
    data class LegacyClaim(
        val session: AuthSession,
        val message: String = "",
        val busy: Boolean = false,
    ) : AuthUiState
    data class SignedIn(val session: AuthSession) : AuthUiState
}

@Composable
private fun AuthenticationCheckingScreen() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            CircularProgressIndicator(color = Vermilion)
            Text("正在确认安全登录…", color = Color(0xFF68706C))
        }
    }
}

@Composable
private fun LegacyMigrationDecisionScreen(
    session: AuthSession,
    onClaim: () -> Unit,
    onContinueOnce: () -> Unit,
    onExit: () -> Unit,
) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Surface(
            modifier = Modifier.fillMaxWidth(),
            color = Color.White,
            shape = RoundedCornerShape(12.dp),
            border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
        ) {
            Column(
                Modifier.padding(22.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(13.dp),
            ) {
                Text("领取AI替身主人账号", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                Text(
                    "已找到旧版主人数据。领取后可用账号密码在 Windows、Android 和 iPhone 共享同一份云端数据。",
                    color = Color(0xFF68706C),
                )
                Text(
                    "选择前，AI替身不会采集、同步或弹出建言。服务器已固定为 ${session.serverOrigin}，领取时不能更改。",
                    color = Vermilion,
                    style = MaterialTheme.typography.bodySmall,
                )
                Button(onClick = onClaim, modifier = Modifier.fillMaxWidth()) {
                    Text("领取主人账号")
                }
                OutlinedButton(onClick = onContinueOnce, modifier = Modifier.fillMaxWidth()) {
                    Text("本次暂时继续旧模式")
                }
                Text(
                    "暂时继续只在本次应用进程有效；手机重启或应用进程结束后会再次询问。",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = onExit, modifier = Modifier.fillMaxWidth()) { Text("退出") }
            }
        }
    }
}

@Composable
private fun LegacyOwnerClaimScreen(
    session: AuthSession,
    message: String,
    busy: Boolean,
    onBack: () -> Unit,
    onSubmit: (AuthMode, String, String, String) -> Unit,
) {
    var mode by remember { mutableStateOf(AuthMode.LOGIN) }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var registrationCode by remember { mutableStateOf("") }
    val validation = com.mouchen.app.sync.validateCredentials(
        username,
        password,
        requireStrongPassword = mode == AuthMode.REGISTER,
    )
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Surface(
            modifier = Modifier.fillMaxWidth(),
            color = Color.White,
            shape = RoundedCornerShape(12.dp),
            border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
        ) {
            Column(
                Modifier.padding(22.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(13.dp),
            ) {
                Text("领取主人账号", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
                Text(
                    if (mode == AuthMode.LOGIN) "主人账号已经在其他设备领取：直接登录。"
                    else "首次领取：使用主人邀请码注册。",
                    color = Color(0xFF68706C),
                )
                if (message.isNotBlank()) {
                    Text(message, color = if (busy) Amber else Vermilion, style = MaterialTheme.typography.bodySmall)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (mode == AuthMode.LOGIN) {
                        Button(enabled = !busy, onClick = {}, modifier = Modifier.weight(1f)) { Text("登录已领取账号") }
                        OutlinedButton(
                            enabled = !busy,
                            onClick = { mode = AuthMode.REGISTER },
                            modifier = Modifier.weight(1f),
                        ) { Text("首次领取") }
                    } else {
                        OutlinedButton(
                            enabled = !busy,
                            onClick = { mode = AuthMode.LOGIN },
                            modifier = Modifier.weight(1f),
                        ) { Text("登录已领取账号") }
                        Button(enabled = !busy, onClick = {}, modifier = Modifier.weight(1f)) { Text("首次领取") }
                    }
                }
                OutlinedTextField(
                    username,
                    { username = it.take(64) },
                    label = { Text("用户名") },
                    enabled = !busy,
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    password,
                    { password = it.take(1024) },
                    label = { Text(if (mode == AuthMode.LOGIN) "密码" else "密码（至少 10 个字符）") },
                    enabled = !busy,
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )
                if (mode == AuthMode.REGISTER) {
                    OutlinedTextField(
                        registrationCode,
                        { registrationCode = it.take(512) },
                        label = { Text("主人邀请码") },
                        enabled = !busy,
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                Text("固定服务器：${session.serverOrigin}", color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
                Button(
                    onClick = { onSubmit(mode, username.trim(), password, registrationCode.trim()) },
                    enabled = !busy && validation == null,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (busy) {
                        CircularProgressIndicator(modifier = Modifier.width(18.dp).height(18.dp), strokeWidth = 2.dp)
                    } else {
                        Text(if (mode == AuthMode.LOGIN) "登录并领取" else "注册并领取")
                    }
                }
                Text(
                    "只有服务器返回的账号身份与旧主人身份完全一致才会迁移。错误账号会被撤销，不会清除旧队列和授权。",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(enabled = !busy, onClick = onBack, modifier = Modifier.fillMaxWidth()) { Text("返回") }
            }
        }
    }
}

@Composable
private fun AuthenticationScreen(
    initialBaseUrl: String,
    initialMessage: String,
    busy: Boolean,
    onSubmit: (AuthMode, String, String, String, String) -> Unit,
    locale: String,
    onLocaleChange: (String) -> Unit,
) {
    var mode by remember { mutableStateOf(AuthMode.LOGIN) }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var registrationCode by remember { mutableStateOf("") }
    var baseUrl by remember(initialBaseUrl) { mutableStateOf(initialBaseUrl) }
    val validation = validateAuthForm(mode, username, password, baseUrl)
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Surface(
            modifier = Modifier.fillMaxWidth(),
            color = Color.White,
            shape = RoundedCornerShape(12.dp),
            border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
        ) {
            Column(
                Modifier.padding(22.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(13.dp),
            ) {
                LanguageToggle(locale, onLocaleChange, enabled = !busy)
                Text("AI替身", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                Text(
                    if (mode == AuthMode.LOGIN) "登录后继续了解你的局势" else "建立独立、加密的AI替身账号",
                    color = Color(0xFF68706C),
                )
                if (initialMessage.isNotBlank()) {
                    Text(initialMessage, color = if (busy) Amber else Vermilion, style = MaterialTheme.typography.bodySmall)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (mode == AuthMode.LOGIN) {
                        Button(enabled = !busy, onClick = {}, modifier = Modifier.weight(1f)) { Text("登录") }
                        OutlinedButton(enabled = !busy, onClick = { mode = AuthMode.REGISTER }, modifier = Modifier.weight(1f)) {
                            Text("注册")
                        }
                    } else {
                        OutlinedButton(enabled = !busy, onClick = { mode = AuthMode.LOGIN }, modifier = Modifier.weight(1f)) {
                            Text("登录")
                        }
                        Button(enabled = !busy, onClick = {}, modifier = Modifier.weight(1f)) { Text("注册") }
                    }
                }
                OutlinedTextField(
                    username,
                    { username = it.take(64) },
                    label = { Text("用户名") },
                    enabled = !busy,
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    password,
                    { password = it.take(1024) },
                    label = { Text(if (mode == AuthMode.LOGIN) "密码" else "密码（至少10个字符）") },
                    enabled = !busy,
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )
                if (mode == AuthMode.REGISTER) {
                    OutlinedTextField(
                        registrationCode,
                        { registrationCode = it.take(512) },
                        label = { Text("邀请码（开放注册时可留空）") },
                        enabled = !busy,
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                OutlinedTextField(
                    baseUrl,
                    { baseUrl = it.take(300) },
                    label = { Text("服务器地址（HTTPS）") },
                    enabled = !busy,
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                validation?.let { Text(it, color = Vermilion, style = MaterialTheme.typography.bodySmall) }
                Button(
                    onClick = {
                        onSubmit(mode, username.trim(), password, baseUrl.trim(), registrationCode.trim())
                    },
                    enabled = !busy && validation == null,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (busy) {
                        CircularProgressIndicator(modifier = Modifier.width(18.dp).height(18.dp), strokeWidth = 2.dp)
                    } else {
                        Text(if (mode == AuthMode.LOGIN) "安全登录" else "注册并登录")
                    }
                }
                Text(
                    "密码只用于本次登录请求，不保存在手机；登录令牌由 Android Keystore 加密保存。",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

private fun validateAuthForm(mode: AuthMode, username: String, password: String, baseUrl: String): String? =
    com.mouchen.app.sync.validateCredentials(
        username,
        password,
        requireStrongPassword = mode == AuthMode.REGISTER,
    )
        ?: if (!com.mouchen.app.sync.isSecureBackendUrl(baseUrl)) "服务器地址必须使用 HTTPS" else null

private fun sharedImageFailureMessage(reason: SharedImageFailure): String = when (reason) {
    SharedImageFailure.UNAVAILABLE_IN_BUILD -> "当前版本不支持图片文字识别"
    SharedImageFailure.BUSY -> "上一张图片仍在识别，请稍后再试"
    SharedImageFailure.UNSUPPORTED_URI -> "只支持由其他 App 授权分享的图片"
    SharedImageFailure.UNSUPPORTED_TYPE -> "分享内容不是受支持的图片"
    SharedImageFailure.READ_DENIED -> "图片读取授权已失效，请重新分享"
    SharedImageFailure.READ_FAILED -> "图片读取失败，请重试"
    SharedImageFailure.TOO_LARGE -> "图片过大；单张最多 8 MB"
    SharedImageFailure.INVALID_IMAGE -> "图片格式无效或无法安全解码"
    SharedImageFailure.NO_TEXT -> "图片中没有识别到足够文字"
    SharedImageFailure.OCR_FAILED -> "手机本地文字识别失败，请重试"
}

private fun screenCaptureWarningLabel(detail: String): String = when (detail) {
    "account_collection_consent_required" ->
        "当前账号尚未允许内容采集；系统权限不会覆盖这个账号开关，当前没有读取屏幕内容。"
    "capture_authorization_denied" -> "你未允许屏幕采样；当前没有读取屏幕内容。需要时请重新启动并授权。"
    "capture_authorization_ended", "capture_session_ended" ->
        "屏幕采样授权已经结束；Android 要求每次会话由你重新授权，当前没有读取屏幕内容。"
    "capture_authorization_invalid", "capture_authorization_missing" ->
        "屏幕采样授权无效；当前没有读取屏幕内容，请重新启动并授权。"
    "capture_start_failed" -> "屏幕采样启动失败；当前没有读取屏幕内容，请重新授权后重试。"
    else -> "屏幕采样没有有效授权；当前没有读取屏幕内容，请重新启动并授权。"
}

private enum class PermissionAction {
    Calendar, Location, Notifications, NotificationAccess, UsageAccess,
    Microphone, Contacts, Sms, CallLog, Accessibility, InputMethod,
    StartAudio, StopAudio, StartScreen, StopScreen, BatteryOptimization,
}

private data class PermissionSnapshot(
    val calendar: Boolean,
    val location: Boolean,
    val notifications: Boolean,
    val notificationAccess: Boolean,
    val usageAccess: Boolean,
    val microphone: Boolean,
    val contacts: Boolean,
    val sms: Boolean,
    val callLog: Boolean,
    val accessibility: Boolean,
    val inputMethod: Boolean,
    val batteryOptimizationIgnored: Boolean,
) {
    val enabledCount: Int get() = listOf(
        calendar, location, notifications, notificationAccess, usageAccess,
    ).count { it }

    val backfillMask: Int get() = listOf(
        calendar,
        notificationAccess,
        usageAccess,
        contacts,
        sms,
        callLog,
        accessibility,
        inputMethod,
    ).foldIndexed(0) { index, mask, enabled -> if (enabled) mask or (1 shl index) else mask }
}

internal fun shouldBackfillAfterPermissionRefresh(previousMask: Int?, currentMask: Int): Boolean =
    previousMask != null && currentMask and previousMask.inv() != 0

internal fun shouldCollectAfterPermissionRefresh(previousMask: Int?, currentMask: Int): Boolean =
    previousMask != null && previousMask != currentMask

@ChecksSdkIntAtLeast(api = Build.VERSION_CODES.UPSIDE_DOWN_CAKE, parameter = 0)
internal fun usesDefaultDisplayProjectionConfig(sdkInt: Int): Boolean =
    sdkInt >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE

@Composable
private fun <T> Flow<List<T>>.collectList(): List<T> {
    var value by remember(this) { mutableStateOf(emptyList<T>()) }
    LaunchedEffect(this) { collectLatest { value = it } }
    return value
}

@Composable
private fun <T> Flow<T>.collectValue(initial: T): T {
    var value by remember(this) { mutableStateOf(initial) }
    LaunchedEffect(this) { collectLatest { value = it } }
    return value
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MouchenApp(
    adviceFlow: Flow<List<AdviceEntity>>,
    goalsFlow: Flow<List<GoalEntity>>,
    eventFlow: Flow<List<LocalEventEntity>>,
    pipelineStatusFlow: Flow<PipelineStatusSnapshot>,
    pendingEventsFlow: Flow<Int>,
    pendingGoalsFlow: Flow<Int>,
    permissionSnapshot: PermissionSnapshot,
    audioRunning: Boolean,
    screenRunning: Boolean,
    screenCaptureWarning: String,
    timingSettings: TimingSettings,
    onPermissionAction: (PermissionAction) -> Unit,
    onAddGoal: (GoalEntity) -> Unit,
    onShareContext: (String) -> Unit,
    onImportConversation: () -> Unit,
    onRecordQuestion: (String) -> Unit,
    onScanNow: () -> Unit,
    onSaveQuietHours: (Boolean, Int, Int) -> Unit,
    onTemporaryDriving: (Boolean) -> Unit,
    onAdviceFeedback: suspend (String, AdviceFeedbackKind) -> AdviceFeedbackResult,
    onAdviceGuidance: suspend (String, String) -> AdviceFeedbackResult,
    onAdviceOutcome: suspend (String, AdviceOutcomeKind) -> AdviceFeedbackResult,
    backendConnectionProvider: () -> BackendConnection,
    account: AuthSession,
    accountConsent: AccountConsent,
    onAccountConsentChange: (AccountConsent) -> Unit,
    onLogout: () -> Unit,
    locale: String,
    onLocaleChange: (String) -> Unit,
) {
    var selected by remember { mutableStateOf(Screen.Situation) }
    val backendConnection = remember(selected) { backendConnectionProvider() }
    val advice = adviceFlow.collectList()
    val goals = goalsFlow.collectList()
    val events = eventFlow.collectList()
    val pipelineStatus = pipelineStatusFlow.collectValue(PipelineStatusSnapshot())
    val pendingEvents = pendingEventsFlow.collectValue(0)
    val pendingGoals = pendingGoalsFlow.collectValue(0)
    Scaffold(
        containerColor = Paper,
        topBar = {
            TopAppBar(
                title = { Text("AI替身", fontWeight = FontWeight.SemiBold) },
                actions = {
                    LanguageToggle(locale, onLocaleChange)
                    if (account.username.isBlank()) {
                        Text("已登录", style = MaterialTheme.typography.labelMedium)
                    } else {
                        RawText(account.username.take(18), style = MaterialTheme.typography.labelMedium)
                    }
                    TextButton(onClick = onLogout) { Text("退出") }
                    StatusMark(permissionSnapshot.enabledCount >= 4)
                    Spacer(Modifier.width(16.dp))
                },
            )
        },
        bottomBar = {
            NavigationBar {
                Screen.entries.forEach { screen ->
                    NavigationBarItem(
                        selected = selected == screen,
                        onClick = { selected = screen },
                        icon = { Text(screen.label.take(1), fontWeight = FontWeight.Bold) },
                        label = { Text(screen.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (selected) {
                Screen.Situation -> SituationScreen(
                    goals,
                    advice,
                    events,
                    permissionSnapshot,
                    pipelineStatus,
                    pendingEvents,
                    pendingGoals,
                    backendConnection,
                    onShareContext,
                    onImportConversation,
                    { selected = Screen.Goals },
                    onScanNow,
                    onAdviceFeedback,
                    onAdviceGuidance,
                    onAdviceOutcome,
                )
                Screen.Advice -> AdviceScreen(
                    advice,
                    onAdviceFeedback,
                    onAdviceGuidance,
                    onAdviceOutcome,
                    onRecordQuestion,
                )
                Screen.Goals -> GoalsScreen(goals, onAddGoal)
                Screen.Connections -> ConnectionsScreen()
                Screen.Permissions -> PermissionsScreen(
                    permissionSnapshot,
                    audioRunning,
                    screenRunning,
                    screenCaptureWarning,
                    timingSettings,
                    backendConnection,
                    accountConsent,
                    onAccountConsentChange,
                    onPermissionAction,
                    onSaveQuietHours,
                    onTemporaryDriving,
                )
            }
        }
    }
}

@Composable
private fun LanguageToggle(locale: String, onChange: (String) -> Unit, enabled: Boolean = true) {
    Row(horizontalArrangement = Arrangement.spacedBy(2.dp), verticalAlignment = Alignment.CenterVertically) {
        TextButton(enabled = enabled && locale != LOCALE_ZH_CN, onClick = { onChange(LOCALE_ZH_CN) }) { Text("ZH") }
        TextButton(enabled = enabled && locale != LOCALE_EN_US, onClick = { onChange(LOCALE_EN_US) }) { Text("EN") }
    }
}

@Composable
private fun SituationScreen(
    goals: List<GoalEntity>,
    advice: List<AdviceEntity>,
    events: List<LocalEventEntity>,
    permissions: PermissionSnapshot,
    pipelineStatus: PipelineStatusSnapshot,
    pendingEvents: Int,
    pendingGoals: Int,
    backendConnection: BackendConnection,
    onShareContext: (String) -> Unit,
    onImportConversation: () -> Unit,
    onOpenGoals: () -> Unit,
    onScanNow: () -> Unit,
    onAdviceFeedback: suspend (String, AdviceFeedbackKind) -> AdviceFeedbackResult,
    onAdviceGuidance: suspend (String, String) -> AdviceFeedbackResult,
    onAdviceOutcome: suspend (String, AdviceOutcomeKind) -> AdviceFeedbackResult,
) {
    val recentCutoff = System.currentTimeMillis() - 24L * 60 * 60 * 1000
    val recentEvents = events.filter { it.occurredAt >= recentCutoff }
    val sourceCounts = recentEvents.groupingBy(::sourceLabel).eachCount().entries
        .sortedByDescending { it.value }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Metric("目标", goals.size.toString(), Modifier.weight(1f))
                Metric("待办谏言", advice.count { it.status == "active" }.toString(), Modifier.weight(1f))
                Metric("基础权限", "${permissions.enabledCount}/5", Modifier.weight(1f))
            }
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Metric("近24小时", recentEvents.size.toString(), Modifier.weight(1f))
                Metric("信息来源", sourceCounts.size.toString(), Modifier.weight(1f))
            }
        }
        item {
            PipelineStatusPanel(pipelineStatus, pendingEvents, pendingGoals)
        }
        if (goals.isEmpty()) {
            item {
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = Color(0xFFFFF7E7),
                    shape = RoundedCornerShape(8.dp),
                    border = BorderStroke(1.dp, Color(0xFFE7C98B)),
                ) {
                    Column(
                        Modifier.padding(14.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        Text("先告诉AI替身你要去哪里", fontWeight = FontWeight.SemiBold)
                        Text(
                            "没有你的目标原话，系统只能保存信息，不能判断什么值得主动提醒。",
                            color = Color(0xFF68706C),
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Button(onClick = onOpenGoals, shape = RoundedCornerShape(6.dp)) {
                            Text("建立第一个目标")
                        }
                    }
                }
            }
        }
        item {
            OutlinedButton(
                onClick = onScanNow,
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(6.dp),
            ) {
                Text("立即扫描并同步")
            }
        }
        item { OwnerContextPanel(backendConnection, onShareContext, onImportConversation) }
        item { SectionTitle("已了解信息") }
        if (sourceCounts.isEmpty()) {
            item { EmptyLine("尚未收到手机信息") }
        } else {
            items(sourceCounts.take(6), key = { it.key }) { source ->
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = Color.White,
                    shape = RoundedCornerShape(6.dp),
                ) {
                    Row(Modifier.padding(horizontal = 12.dp, vertical = 9.dp)) {
                        Text(source.key, modifier = Modifier.weight(1f))
                        Text("${source.value} 条", color = Color(0xFF68706C))
                    }
                }
            }
        }
        item { SectionTitle("当前战线") }
        if (goals.isEmpty()) {
            item { EmptyLine("尚未建立目标") }
        } else {
            items(goals.take(4), key = { it.id }) { goal -> GoalRow(goal) }
        }
        item { SectionTitle("下一步") }
        val current = advice.firstOrNull { it.status == "active" }
        if (current == null) {
            item { EmptyLine("暂无待处理谏言") }
        } else {
            item { AdviceCard(current, onAdviceFeedback, onAdviceGuidance, onAdviceOutcome) }
        }
    }
}

@Composable
private fun OwnerContextPanel(
    backendConnection: BackendConnection,
    onShareContext: (String) -> Unit,
    onImportConversation: () -> Unit,
) {
    var text by remember { mutableStateOf("") }
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Color.White,
        shape = RoundedCornerShape(8.dp),
        border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
            Text("告诉AI替身", fontWeight = FontWeight.SemiBold)
            Text(
                "把你正在想、刚看到、刚听到或遇到的情况记在这里。这不是提问；规则会立即判断，开启主动云分析后还会发现规则外问题。${ownerContextDisclosure(backendConnection)}",
                color = Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedTextField(
                value = text,
                onValueChange = { text = it.take(4_000) },
                label = { Text("想法、对话或当下情况") },
                modifier = Modifier.fillMaxWidth(),
                minLines = 2,
                maxLines = 6,
            )
            Button(
                onClick = {
                    val value = text.trim()
                    if (value.isNotEmpty()) {
                        onShareContext(value)
                        text = ""
                    }
                },
                enabled = text.isNotBlank(),
                shape = RoundedCornerShape(6.dp),
            ) {
                Text("保存并立即研判")
            }
            if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
                OutlinedButton(
                    onClick = onImportConversation,
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(6.dp),
                ) {
                    Text("选择并导入对话文本（.txt）")
                }
                Text(
                    "只读取你在系统文件选择器中主动选择的文件；无法读取微信或其他 App 的私有聊天库。单文件最多 512 KB，并按上限分段。",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
private fun PipelineStatusPanel(
    snapshot: PipelineStatusSnapshot,
    pendingEvents: Int,
    pendingGoals: Int,
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Color.White,
        shape = RoundedCornerShape(8.dp),
        border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("采集与同步", fontWeight = FontWeight.SemiBold)
            PipelineStageLine("采集", snapshot.collection)
            PipelineStageLine("同步", snapshot.sync)
            Text(
                "本地待同步：$pendingEvents 条事件，$pendingGoals 个目标",
                color = if (pendingEvents + pendingGoals > 0) Amber else Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun PipelineStageLine(label: String, stage: PipelineStageStatus) {
    val healthy = stage.phase in setOf(
        PipelinePhase.QUEUED,
        PipelinePhase.RUNNING,
        PipelinePhase.SUCCEEDED,
    )
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusMark(healthy)
            Spacer(Modifier.width(8.dp))
            Text(
                "${uiText(label)}: ${uiText(pipelinePhaseLabel(stage.phase))}",
                fontWeight = FontWeight.Medium,
            )
            Spacer(Modifier.weight(1f))
            if (stage.finishedAt > 0L) {
                Text(formatPipelineTime(stage.finishedAt), color = Color.Gray, style = MaterialTheme.typography.labelSmall)
            }
        }
        if (stage.attempted > 0 || stage.failed > 0) {
            Text(
                "本轮 ${stage.succeeded}/${stage.attempted} 成功，${stage.failed} 失败",
                color = Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
        }
        if (stage.detail.isNotBlank()) {
            Text(
                pipelineDetailLabel(stage.detail),
                color = if (stage.failed > 0) Vermilion else Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

private fun pipelinePhaseLabel(phase: PipelinePhase): String = when (phase) {
    PipelinePhase.IDLE -> "尚未运行"
    PipelinePhase.QUEUED -> "等待执行"
    PipelinePhase.RUNNING -> "执行中"
    PipelinePhase.SUCCEEDED -> "已完成"
    PipelinePhase.PARTIAL_FAILURE -> "部分失败，等待重试"
    PipelinePhase.FAILED -> "失败，等待重试"
    PipelinePhase.DISABLED -> "后端未启用，仅保存在本机"
    PipelinePhase.AUTH_REQUIRED -> "需要重新登录"
}

private fun pipelineDetailLabel(detail: String): String = when (detail) {
    "backend_disabled" -> "启用后端连接后会自动同步现有信息"
    "database_unavailable" -> "本地加密数据库暂不可用"
    "delivery_failed" -> "后端暂未接收全部内容"
    "batch_limit_reached" -> "数据较多，后台将继续分批同步"
    "unexpected_sync_error" -> "同步发生异常，后台将自动重试"
    "authentication_required" -> "登录后将自动继续同步本地信息"
    else -> "失败环节：$detail"
}

private fun formatPipelineTime(value: Long): String =
    SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(value))

private fun sourceLabel(event: LocalEventEntity): String = when {
    event.source.startsWith("android.self_report") ||
        event.source.startsWith("android.shared_text") ||
        event.source.startsWith("android.question") -> "我的自述"
    event.source.startsWith("android.shared_image") -> "分享图片"
    event.source.startsWith("android.conversation_import") -> "对话导入"
    event.source.startsWith("android.notification") -> "通知"
    event.source.startsWith("android.accessibility") -> "当前界面"
    event.source.startsWith("android.usage") -> "App 使用"
    event.source.startsWith("android.calendar") -> "日历"
    event.source.startsWith("android.ime") -> "输入内容"
    event.source.startsWith("android.screen") -> "屏幕会话"
    event.source.startsWith("android.microphone") -> "录音会话"
    event.source.startsWith("imap.") -> "邮件"
    event.source.startsWith("rss.") -> "外部情报"
    else -> "其他"
}

@Composable
private fun AdviceScreen(
    advice: List<AdviceEntity>,
    onAdviceFeedback: suspend (String, AdviceFeedbackKind) -> AdviceFeedbackResult,
    onAdviceGuidance: suspend (String, String) -> AdviceFeedbackResult,
    onAdviceOutcome: suspend (String, AdviceOutcomeKind) -> AdviceFeedbackResult,
    onRecordQuestion: (String) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val router = remember(context) {
        ModelFallbackRouter(
            privateClient = PrivateOllamaClient(OllamaConnectionStore(context)),
            backendClient = BackendModelClient(context, BackendConnectionStore(context)),
        )
    }
    var prompt by remember { mutableStateOf("") }
    var preferPrivate by remember { mutableStateOf(true) }
    var outboundApproved by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    var reply by remember { mutableStateOf<ModelReply?>(null) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item { SectionTitle("问AI替身") }
        item {
            ModelQuestionPanel(
                prompt = prompt,
                preferPrivate = preferPrivate,
                outboundApproved = outboundApproved,
                loading = loading,
                reply = reply,
                onPromptChange = { prompt = it },
                onPreferPrivateChange = { preferPrivate = it },
                onOutboundApprovedChange = { outboundApproved = it },
                onSubmit = {
                    val question = prompt.trim()
                    if (question.isNotEmpty() && !loading) {
                        onRecordQuestion(question)
                        val approvedForCall = outboundApproved
                        outboundApproved = false
                        loading = true
                        reply = null
                        scope.launch {
                            try {
                                reply = router.chat(
                                    ModelChatRequest(
                                        prompt = question,
                                        context = JSONObject().put(
                                            "active_advice_count",
                                            advice.count { it.status == "active" },
                                        ),
                                        adviceLevel = 2,
                                        purpose = "user_question",
                                        preferPrivateNode = preferPrivate,
                                        outboundApproved = approvedForCall,
                                    ),
                                )
                            } finally {
                                loading = false
                            }
                        }
                    }
                },
            )
        }
        item { SectionTitle("谏言台账") }
        if (advice.isEmpty()) item { EmptyLine("暂无谏言") }
        items(advice, key = { it.id }) {
            AdviceCard(it, onAdviceFeedback, onAdviceGuidance, onAdviceOutcome)
        }
    }
}

@Composable
private fun ModelQuestionPanel(
    prompt: String,
    preferPrivate: Boolean,
    outboundApproved: Boolean,
    loading: Boolean,
    reply: ModelReply?,
    onPromptChange: (String) -> Unit,
    onPreferPrivateChange: (Boolean) -> Unit,
    onOutboundApprovedChange: (Boolean) -> Unit,
    onSubmit: () -> Unit,
) {
    Surface(color = Color.White, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
            OutlinedTextField(
                value = prompt,
                onValueChange = onPromptChange,
                label = { Text("问题") },
                modifier = Modifier.fillMaxWidth(),
                minLines = 2,
                maxLines = 6,
                enabled = !loading,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(preferPrivate, onPreferPrivateChange, enabled = !loading)
                Text("私有节点优先", style = MaterialTheme.typography.bodySmall)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(outboundApproved, onOutboundApprovedChange, enabled = !loading)
                Text("允许本次问题和最小上下文上云", style = MaterialTheme.typography.bodySmall)
            }
            Button(
                onClick = onSubmit,
                enabled = prompt.isNotBlank() && !loading,
                shape = RoundedCornerShape(6.dp),
            ) {
                if (loading) {
                    CircularProgressIndicator(
                        modifier = Modifier.width(18.dp).height(18.dp),
                        strokeWidth = 2.dp,
                        color = Color.White,
                    )
                } else {
                    Text("请AI替身作答")
                }
            }
            reply?.let { answer ->
                Text(
                    "${answer.provider} · ${answer.model}${if (answer.degraded) " · 降级" else ""}",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.labelSmall,
                )
                if (answer.content.isBlank()) {
                    Text("模型暂不可用，请先在连接页配置 Backend 或 Ollama。", style = MaterialTheme.typography.bodyMedium)
                } else {
                    RawText(answer.content, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}

@Composable
private fun AdviceCard(
    advice: AdviceEntity,
    onFeedback: (suspend (String, AdviceFeedbackKind) -> AdviceFeedbackResult)? = null,
    onGuidance: (suspend (String, String) -> AdviceFeedbackResult)? = null,
    onOutcome: (suspend (String, AdviceOutcomeKind) -> AdviceFeedbackResult)? = null,
) {
    val scope = rememberCoroutineScope()
    var feedbackLoading by remember(advice.id) { mutableStateOf(false) }
    var feedbackMessage by remember(advice.id) { mutableStateOf<String?>(null) }
    var showCorrectionDialog by remember(advice.id) { mutableStateOf(false) }
    var showGuidanceDialog by remember(advice.id) { mutableStateOf(false) }
    var guidanceText by remember(advice.id) { mutableStateOf("") }
    val submitFeedback: (AdviceFeedbackKind) -> Unit = { kind ->
        onFeedback?.let { handler ->
            feedbackLoading = true
            feedbackMessage = null
            scope.launch {
                val result = handler(advice.id, kind)
                feedbackMessage = result.message
                feedbackLoading = false
            }
        }
    }
    val levelColor = when (advice.level) {
        4 -> Vermilion
        3 -> Amber
        2 -> Pine
        else -> Color(0xFF5E6662)
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(8.dp),
        colors = CardDefaults.cardColors(containerColor = Color.White),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("L${advice.level}", color = levelColor, fontWeight = FontWeight.Bold)
                Spacer(Modifier.width(8.dp))
                RawText(advice.domain, style = MaterialTheme.typography.labelLarge)
                Spacer(Modifier.weight(1f))
                if (advice.delivery == "brief") {
                    Text("简报", style = MaterialTheme.typography.labelSmall, color = Color(0xFF68706C))
                    Spacer(Modifier.width(8.dp))
                }
                Text(advice.status, style = MaterialTheme.typography.labelSmall, color = Color.Gray)
            }
            RawText(advice.action, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text("第一步：${advice.firstStep}", style = MaterialTheme.typography.bodyMedium)
            firstEvidence(advice)?.let {
                Text("依据：$it", color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
            }
            if (advice.alternative.isNotBlank()) {
                Text("替代路径：${advice.alternative}", style = MaterialTheme.typography.bodySmall)
            }
            predictionOutcome(advice)?.let {
                Text("核验：$it", color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
            }
            if (advice.goalQuote.isNotBlank()) {
                Text("「${advice.goalQuote}」", color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
            }
            if (advice.status == "active" && onFeedback != null) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { submitFeedback(AdviceFeedbackKind.Adopted) },
                        enabled = !feedbackLoading,
                        shape = RoundedCornerShape(6.dp),
                    ) { Text("采纳") }
                    OutlinedButton(
                        onClick = { submitFeedback(AdviceFeedbackKind.Later) },
                        enabled = !feedbackLoading,
                        shape = RoundedCornerShape(6.dp),
                    ) { Text("稍后处理") }
                    OutlinedButton(
                        onClick = { submitFeedback(AdviceFeedbackKind.Acknowledged) },
                        enabled = !feedbackLoading,
                        shape = RoundedCornerShape(6.dp),
                    ) { Text("知道了") }
                }
                TextButton(
                    onClick = { submitFeedback(AdviceFeedbackKind.StopTopic) },
                    enabled = !feedbackLoading,
                ) { Text("言尽于此，不再说这条") }
                TextButton(
                    onClick = { showCorrectionDialog = true },
                    enabled = !feedbackLoading,
                ) { Text("这条不准，给AI替身纠错") }
                if (onGuidance != null) {
                    TextButton(
                        onClick = { showGuidanceDialog = true },
                        enabled = !feedbackLoading,
                    ) { Text("给AI替身方向") }
                }
            }
            if (advice.status == "adopted" && onOutcome != null) {
                Text("这条建议的结果如何？", style = MaterialTheme.typography.bodyMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = {
                            feedbackLoading = true
                            feedbackMessage = null
                            scope.launch {
                                val result = onOutcome(advice.id, AdviceOutcomeKind.Correct)
                                feedbackMessage = result.message
                                feedbackLoading = false
                            }
                        },
                        enabled = !feedbackLoading,
                        shape = RoundedCornerShape(6.dp),
                    ) { Text("结果达成") }
                    OutlinedButton(
                        onClick = {
                            feedbackLoading = true
                            feedbackMessage = null
                            scope.launch {
                                val result = onOutcome(advice.id, AdviceOutcomeKind.Incorrect)
                                feedbackMessage = result.message
                                feedbackLoading = false
                            }
                        },
                        enabled = !feedbackLoading,
                        shape = RoundedCornerShape(6.dp),
                    ) { Text("结果未达成") }
                }
            }
            feedbackMessage?.let {
                Text(it, color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
            }
        }
    }
    if (showCorrectionDialog && onFeedback != null) {
        AlertDialog(
            onDismissRequest = { showCorrectionDialog = false },
            title = { Text("这条谏言哪里不准？") },
            text = {
                Column {
                    listOf(
                        "事实有误" to AdviceFeedbackKind.FactError,
                        "预测有误" to AdviceFeedbackKind.PredictionError,
                        "与我的目标无关" to AdviceFeedbackKind.Irrelevant,
                        "提醒时机不对" to AdviceFeedbackKind.TimingError,
                    ).forEach { (label, kind) ->
                        TextButton(
                            onClick = {
                                showCorrectionDialog = false
                                submitFeedback(kind)
                            },
                            modifier = Modifier.fillMaxWidth(),
                            enabled = !feedbackLoading,
                        ) { Text(label) }
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { showCorrectionDialog = false }) { Text("取消") }
            },
        )
    }
    if (showGuidanceDialog && onGuidance != null) {
        AlertDialog(
            onDismissRequest = { if (!feedbackLoading) showGuidanceDialog = false },
            title = { Text("希望AI替身往哪个方向调整？") },
            text = {
                OutlinedTextField(
                    value = guidanceText,
                    onValueChange = {
                        if (it.length <= AdviceFeedbackClient.MAX_GUIDANCE_NOTE_LENGTH) guidanceText = it
                    },
                    label = { Text("你的方向") },
                    supportingText = {
                        Text("例如：先关注现金流，不要再建议扩张")
                    },
                    minLines = 3,
                    maxLines = 6,
                    enabled = !feedbackLoading,
                    modifier = Modifier.fillMaxWidth(),
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        val note = guidanceText.trim()
                        if (note.isNotEmpty()) {
                            feedbackLoading = true
                            feedbackMessage = null
                            scope.launch {
                                val result = onGuidance(advice.id, note)
                                feedbackMessage = result.message
                                if (result.accepted) guidanceText = ""
                                showGuidanceDialog = false
                                feedbackLoading = false
                            }
                        }
                    },
                    enabled = guidanceText.isNotBlank() && !feedbackLoading,
                ) { Text("发送方向") }
            },
            dismissButton = {
                TextButton(
                    onClick = { showGuidanceDialog = false },
                    enabled = !feedbackLoading,
                ) { Text("取消") }
            },
        )
    }
}

private fun firstEvidence(advice: AdviceEntity): String? = runCatching {
    val values = JSONArray(advice.evidenceJson)
    values.optJSONObject(0)?.optString("fact")?.trim()?.takeIf(String::isNotEmpty)
}.getOrNull()

private fun predictionOutcome(advice: AdviceEntity): String? = runCatching {
    val prediction = JSONObject(advice.predictionJson)
    val value = if (advice.status == "adopted") {
        prediction.optString("adopted_expected_result").trim().ifEmpty {
            prediction.optString("outcome").trim()
        }
    } else {
        prediction.optString("outcome").trim()
    }
    value.takeIf(String::isNotEmpty)
}.getOrNull()

@Composable
private fun GoalsScreen(goals: List<GoalEntity>, onAddGoal: (GoalEntity) -> Unit) {
    var showDialog by remember { mutableStateOf(false) }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                SectionTitle("目标原文")
                Spacer(Modifier.weight(1f))
                Button(onClick = { showDialog = true }, shape = RoundedCornerShape(6.dp)) {
                    Text("新增目标")
                }
            }
        }
        if (goals.isEmpty()) item { EmptyLine("尚未建立目标") }
        items(goals, key = { it.id }) { GoalRow(it) }
    }
    if (showDialog) {
        AddGoalDialog(
            onDismiss = { showDialog = false },
            onConfirm = {
                onAddGoal(it)
                showDialog = false
            },
        )
    }
}

@Composable
private fun GoalRow(goal: GoalEntity) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Color.White,
        shape = RoundedCornerShape(8.dp),
        border = BorderStroke(1.dp, Color(0xFFE1E4E0)),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                RawText(goal.domain, color = Pine, style = MaterialTheme.typography.labelLarge)
                if (goal.isRedline) {
                    Spacer(Modifier.width(8.dp))
                    Text("红线", color = Vermilion, style = MaterialTheme.typography.labelMedium)
                }
            }
            RawText(goal.title, fontWeight = FontWeight.SemiBold, maxLines = 2, overflow = TextOverflow.Ellipsis)
            RawText("「${goal.quote}」", color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun AddGoalDialog(onDismiss: () -> Unit, onConfirm: (GoalEntity) -> Unit) {
    var domain by remember { mutableStateOf("事业") }
    var title by remember { mutableStateOf("") }
    var quote by remember { mutableStateOf("") }
    var keywords by remember { mutableStateOf("") }
    var packages by remember { mutableStateOf("") }
    var weeklyHours by remember { mutableStateOf("") }
    var redline by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("新增目标") },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                OutlinedTextField(domain, { domain = it }, label = { Text("领域") }, singleLine = true)
                OutlinedTextField(title, { title = it }, label = { Text("目标") }, singleLine = true)
                OutlinedTextField(quote, { quote = it }, label = { Text("你的原话") }, minLines = 2)
                OutlinedTextField(
                    keywords,
                    { keywords = it },
                    label = { Text("关注关键词（逗号分隔）") },
                    minLines = 2,
                )
                OutlinedTextField(
                    packages,
                    { packages = it },
                    label = { Text("相关 App 包名（可选，逗号分隔）") },
                    minLines = 2,
                )
                OutlinedTextField(
                    weeklyHours,
                    { weeklyHours = it.filter { char -> char.isDigit() || char == '.' } },
                    label = { Text("每周计划投入小时（可选）") },
                    singleLine = true,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(redline, { redline = it })
                    Text("设为红线域")
                }
            }
        },
        confirmButton = {
            Button(
                enabled = domain.isNotBlank() && title.isNotBlank() && quote.isNotBlank(),
                onClick = {
                    onConfirm(
                        GoalEntity(
                            domain = domain.trim(),
                            title = title.trim(),
                            quote = quote.trim(),
                            version = (System.currentTimeMillis() / 1000L).toInt(),
                            isRedline = redline,
                            targetJson = JSONObject().apply {
                                put(
                                    "keywords",
                                    JSONArray(
                                        keywords.split(Regex("[,，\\n]+"))
                                            .map(String::trim)
                                            .filter(String::isNotEmpty)
                                            .distinct(),
                                    ),
                                )
                                put(
                                    "packages",
                                    JSONArray(
                                        packages.split(Regex("[,，\\n]+"))
                                            .map(String::trim)
                                            .filter(String::isNotEmpty)
                                            .distinct(),
                                    ),
                                )
                                weeklyHours.toDoubleOrNull()?.takeIf { it > 0 }?.let {
                                    put("weekly_hours", it)
                                }
                            }.toString(),
                        ),
                    )
                },
            ) { Text("确认") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun PermissionsScreen(
    snapshot: PermissionSnapshot,
    audioRunning: Boolean,
    screenRunning: Boolean,
    screenCaptureWarning: String,
    timingSettings: TimingSettings,
    backendConnection: BackendConnection,
    accountConsent: AccountConsent,
    onAccountConsentChange: (AccountConsent) -> Unit,
    onAction: (PermissionAction) -> Unit,
    onSaveQuietHours: (Boolean, Int, Int) -> Unit,
    onTemporaryDriving: (Boolean) -> Unit,
) {
    val base = listOf(
        Triple("日历", snapshot.calendar, PermissionAction.Calendar),
        Triple("位置", snapshot.location, PermissionAction.Location),
        Triple("通知推送", snapshot.notifications, PermissionAction.Notifications),
        Triple("通知读取", snapshot.notificationAccess, PermissionAction.NotificationAccess),
        Triple("应用使用记录", snapshot.usageAccess, PermissionAction.UsageAccess),
    )
    val privateItems = if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) listOf(
        Triple("麦克风", snapshot.microphone, PermissionAction.Microphone),
        Triple("联系人（姓名与标识哈希）", snapshot.contacts, PermissionAction.Contacts),
        Triple("近 7 天短信", snapshot.sms, PermissionAction.Sms),
        Triple("近 7 天通话记录", snapshot.callLog, PermissionAction.CallLog),
        Triple("可见界面读取", snapshot.accessibility, PermissionAction.Accessibility),
        Triple("AI替身输入法", snapshot.inputMethod, PermissionAction.InputMethod),
    ) else emptyList()
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item { SectionTitle("账号数据开关") }
        item {
            AccountConsentCard(
                consent = accountConsent,
                onChange = onAccountConsentChange,
            )
        }
        item { SectionTitle("系统授权") }
        items(base, key = { it.first }) { (name, enabled, action) ->
            PermissionRow(name, enabled) { onAction(action) }
        }
        item { SectionTitle("推送时机") }
        item {
            TimingSettingsCard(
                settings = timingSettings,
                onSaveQuietHours = onSaveQuietHours,
                onTemporaryDriving = onTemporaryDriving,
            )
        }
        item { SectionTitle("后台运行") }
        item {
            BatteryOptimizationCard(
                ignored = snapshot.batteryOptimizationIgnored,
                onOpenSettings = { onAction(PermissionAction.BatteryOptimization) },
            )
        }
        if (BuildConfig.ALLOW_SENSITIVE_CAPTURE) {
            item { SectionTitle("私测感知") }
            item {
                Text(
                    "联系人标识和通话只发送最小化字段；短信原文仅存本机，最小化上下文模式可能将脱敏后的短信片段发给已配置的模型。三项权限可分别授予或拒绝。",
                    color = Color(0xFF68706C),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            items(privateItems, key = { it.first }) { (name, enabled, action) ->
                PermissionRow(name, enabled) { onAction(action) }
            }
            item {
                CaptureControl(
                    title = "环境录音",
                    description = audioCaptureDisclosure(backendConnection),
                    running = audioRunning,
                    onStart = { onAction(PermissionAction.StartAudio) },
                    onStop = { onAction(PermissionAction.StopAudio) },
                )
            }
            item {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    if (screenCaptureWarning.isNotBlank()) {
                        Text(
                            screenCaptureWarningLabel(screenCaptureWarning),
                            color = Vermilion,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    CaptureControl(
                        title = "屏幕采样",
                        description = "截图加密保存在手机，并在手机本地识别中英文文字",
                        running = screenRunning,
                        onStart = { onAction(PermissionAction.StartScreen) },
                        onStop = { onAction(PermissionAction.StopScreen) },
                    )
                }
            }
        }
    }
}

@Composable
private fun AccountConsentCard(
    consent: AccountConsent,
    onChange: (AccountConsent) -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = Color.White)) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("系统权限只代表 Android 允许访问；以下开关是当前账号对AI替身的独立授权。")
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(
                    checked = consent.collectionEnabled,
                    onCheckedChange = { onChange(consent.copy(collectionEnabled = it)) },
                )
                Column(Modifier.weight(1f)) {
                    Text("允许AI替身采集", fontWeight = FontWeight.SemiBold)
                    Text("关闭时不新增日历、位置、通知、可见界面、输入法、录音或屏幕事件。")
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(
                    checked = consent.cloudAnalysisEnabled,
                    onCheckedChange = { onChange(consent.copy(cloudAnalysisEnabled = it)) },
                )
                Column(Modifier.weight(1f)) {
                    Text("允许主动云分析", fontWeight = FontWeight.SemiBold)
                    Text("关闭时不会为当前账号发送主动云分析授权；完整原文仍受连接页的单独开关限制。")
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(
                    checked = consent.fullContextCloudEnabled,
                    enabled = consent.cloudAnalysisEnabled,
                    onCheckedChange = { onChange(consent.copy(fullContextCloudEnabled = it)) },
                )
                Column(Modifier.weight(1f)) {
                    Text("允许完整上下文上云", fontWeight = FontWeight.SemiBold)
                    Text("默认关闭；只有当前账号在这里授权且连接页也允许时，才会发送净化后的完整上下文。")
                }
            }
        }
    }
}

@Composable
private fun TimingSettingsCard(
    settings: TimingSettings,
    onSaveQuietHours: (Boolean, Int, Int) -> Unit,
    onTemporaryDriving: (Boolean) -> Unit,
) {
    var showQuietDialog by remember { mutableStateOf(false) }
    val driving = settings.isDriving(System.currentTimeMillis())
    Surface(color = Color.White, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
            Text("静默与驾驶", fontWeight = FontWeight.Medium)
            Text(
                if (settings.quietHoursEnabled) {
                    "静默时段 ${formatClockMinute(settings.quietStartMinute)}–${formatClockMinute(settings.quietEndMinute)}"
                } else {
                    "静默时段未启用"
                },
                color = Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "系统勿扰开启时也会自动暂缓非紧急进言。",
                color = Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { showQuietDialog = true }, shape = RoundedCornerShape(6.dp)) {
                    Text("设置静默时段")
                }
                if (driving) {
                    OutlinedButton(onClick = { onTemporaryDriving(false) }, shape = RoundedCornerShape(6.dp)) {
                        Text("结束驾驶模式")
                    }
                } else {
                    Button(onClick = { onTemporaryDriving(true) }, shape = RoundedCornerShape(6.dp)) {
                        Text("驾驶模式 2 小时")
                    }
                }
            }
            if (driving) {
                Text("驾驶模式已开启；非紧急进言将暂缓。", color = Amber, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
    if (showQuietDialog) {
        QuietHoursDialog(
            settings = settings,
            onDismiss = { showQuietDialog = false },
            onConfirm = { enabled, start, end ->
                onSaveQuietHours(enabled, start, end)
                showQuietDialog = false
            },
        )
    }
}

@Composable
private fun QuietHoursDialog(
    settings: TimingSettings,
    onDismiss: () -> Unit,
    onConfirm: (Boolean, Int, Int) -> Unit,
) {
    var enabled by remember(settings) { mutableStateOf(settings.quietHoursEnabled) }
    var startText by remember(settings) { mutableStateOf(formatClockMinute(settings.quietStartMinute)) }
    var endText by remember(settings) { mutableStateOf(formatClockMinute(settings.quietEndMinute)) }
    val start = parseClockMinute(startText)
    val end = parseClockMinute(endText)
    val valid = !enabled || (start != null && end != null && start != end)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("静默时段") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(enabled, { enabled = it })
                    Text("启用每日静默")
                }
                OutlinedTextField(
                    value = startText,
                    onValueChange = { startText = it.take(5) },
                    label = { Text("开始（HH:mm）") },
                    singleLine = true,
                    isError = enabled && start == null,
                )
                OutlinedTextField(
                    value = endText,
                    onValueChange = { endText = it.take(5) },
                    label = { Text("结束（HH:mm）") },
                    singleLine = true,
                    isError = enabled && end == null,
                )
                if (enabled && start != null && start == end) {
                    Text("开始与结束时间不能相同", color = Vermilion, style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {
            Button(
                enabled = valid,
                onClick = {
                    onConfirm(
                        enabled,
                        start ?: settings.quietStartMinute,
                        end ?: settings.quietEndMinute,
                    )
                },
            ) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun PermissionRow(name: String, enabled: Boolean, onClick: () -> Unit) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Color.White,
        shape = RoundedCornerShape(8.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            StatusMark(enabled)
            Spacer(Modifier.width(10.dp))
            Text(name, Modifier.weight(1f), fontWeight = FontWeight.Medium)
            OutlinedButton(onClick = onClick, shape = RoundedCornerShape(6.dp)) {
                Text(if (enabled) "设置" else "授权")
            }
        }
    }
}

@Composable
private fun BatteryOptimizationCard(ignored: Boolean, onOpenSettings: () -> Unit) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        color = Color.White,
        shape = RoundedCornerShape(8.dp),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusMark(ignored)
                Spacer(Modifier.width(10.dp))
                Text(
                    if (ignored) "系统省电豁免：已允许" else "系统省电豁免：未允许",
                    fontWeight = FontWeight.Medium,
                )
            }
            Text(
                batteryOptimizationDescription(ignored),
                color = Color(0xFF68706C),
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(onClick = onOpenSettings, shape = RoundedCornerShape(6.dp)) {
                Text("打开系统省电设置")
            }
        }
    }
}

@Composable
private fun CaptureControl(
    title: String,
    description: String,
    running: Boolean,
    onStart: () -> Unit,
    onStop: () -> Unit,
) {
    Surface(color = Color.White, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(title, fontWeight = FontWeight.Medium)
                Text(if (running) "运行中" else "已停止", color = if (running) Pine else Color.Gray)
                Text(description, color = Color(0xFF68706C), style = MaterialTheme.typography.bodySmall)
            }
            if (running) {
                OutlinedButton(onClick = onStop, shape = RoundedCornerShape(6.dp)) { Text("停止") }
            } else {
                Button(onClick = onStart, shape = RoundedCornerShape(6.dp)) { Text("启动") }
            }
        }
    }
}

@Composable
private fun Metric(label: String, value: String, modifier: Modifier = Modifier) {
    Surface(modifier, color = Color.White, shape = RoundedCornerShape(8.dp)) {
        Column(Modifier.padding(12.dp)) {
            Text(value, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text(label, color = Color(0xFF68706C), style = MaterialTheme.typography.labelMedium)
        }
    }
}

@Composable
private fun StatusMark(enabled: Boolean) {
    Box(
        Modifier.width(9.dp).height(9.dp),
        contentAlignment = Alignment.Center,
    ) {
        Surface(
            modifier = Modifier.fillMaxSize(),
            color = if (enabled) Pine else Color(0xFFB7BCB8),
            shape = RoundedCornerShape(5.dp),
        ) {}
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(text, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
}

@Composable
private fun EmptyLine(text: String) {
    Text(text, color = Color(0xFF747C78), modifier = Modifier.padding(vertical = 12.dp))
}

@Composable
private fun MouchenTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = Vermilion,
            onPrimary = Color.White,
            secondary = Pine,
            background = Paper,
            surface = Color.White,
            onSurface = Ink,
        ),
        content = content,
    )
}
