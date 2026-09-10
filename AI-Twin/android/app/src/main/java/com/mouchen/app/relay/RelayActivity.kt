package com.mouchen.app.relay

import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.mouchen.app.sync.OutboundCredentialSanitizer
import java.time.Instant
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/** Explicit owner input only. No backend identity is fabricated and no legacy data is read. */
class RelayActivity : ComponentActivity() {
    private var settings by mutableStateOf<RelaySettings?>(null)
    private var imported by mutableStateOf<RelaySettings?>(null)
    private var draft by mutableStateOf("")
    private var status by mutableStateOf("保存连接后，可将手动记录发给电脑处理")
    private var busy by mutableStateOf(false)
    private var savedGeneration by mutableIntStateOf(0)
    private var directProblem by mutableStateOf(false)
    private var eventDomain by mutableStateOf("general")
    private val importFile = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) lifecycleScope.launch {
            try {
                imported = withContext(Dispatchers.IO) {
                    contentResolver.openInputStream(uri)?.use { stream ->
                        val bytes = stream.readBytesBounded(16 * 1024)
                        decodeImportedSettings(bytes.toString(Charsets.UTF_8))
                    } ?: error("无法读取配置")
                }
                status = "已填入配置；请核对手机和电脑名称后点击保存并启用"
            } catch (_: Exception) { status = "配置无法导入，请检查文件格式与大小" }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        settings = RelaySettingsStore(this).load()
        acceptSharedDraft(intent)
        runCatching { RelaySyncWorker.configure(this) }
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    val current = settings
                    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Text("AI替身 · 电脑本地处理", style = MaterialTheme.typography.headlineSmall)
                        Text("你主动发送的目标和记录会经云端临时中转，交给电脑处理。电脑需保持运行。此入口使用独立加密记录，不会读取原账号内容或开启自动采集。")
                        Text("中转文件最长保存 24 小时，到期后手机仍保留待处理请求并重试；云端不做分析。传输使用 HTTPS，当前中转不是端到端加密。", style = MaterialTheme.typography.bodySmall)
                        ConnectionForm(current, imported, savedGeneration,
                            onImport = { importFile.launch(arrayOf("application/json", "text/plain", "application/octet-stream")) },
                            onSave = ::saveConnection)
                        if (current != null) {
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Button(enabled = !busy, onClick = { toggleEnabled() }) { Text(if (current.enabled) "暂停同步" else "恢复同步") }
                                OutlinedButton(enabled = current.enabled && !busy, onClick = { runSync() }) { Text("立即同步") }
                            }
                            Text(if (current.enabled) "已启用：打开此页时约 30 秒同步一次；后台由手机系统安排，通常至少间隔 15 分钟。" else "已暂停；手动保存的待发内容在恢复后发送。")
                        }
                        Text(status, style = MaterialTheme.typography.bodyMedium)
                        if (current != null) {
                            GoalForm(onSend = { body -> enqueue("goal.create", body) })
                            Text("记录或分享给电脑", style = MaterialTheme.typography.titleMedium)
                            OutlinedTextField(value = draft, onValueChange = { draft = it.take(8000) },
                                label = { Text("记录事实、问题或粘贴分享内容") }, minLines = 3, modifier = Modifier.fillMaxWidth())
                            OutlinedTextField(eventDomain, { eventDomain = it.take(80) }, label = { Text("这条记录的领域，与目标一致") }, modifier = Modifier.fillMaxWidth())
                            Row {
                                Checkbox(checked = directProblem, onCheckedChange = { directProblem = it })
                                Text("这段文字描述我遇到的真实问题，请电脑对照目标给建议")
                            }
                            Text("电脑先保存事实，只在目标与证据符合本地规则时给出建议；没有建议也会返回处理回执。", style = MaterialTheme.typography.bodySmall)
                            Button(enabled = draft.isNotBlank() && !busy, onClick = {
                                val facts = JSONObject().put("text", draft.trim()).put("domain", eventDomain.trim().ifBlank { "general" })
                                    .put("context", if (directProblem) "manual_problem" else "owner_note")
                                    .put("analysis_requested", directProblem)
                                    .put("content_kind", "user_input").put("speaker", "user")
                                    .put("evidence_strength", if (directProblem) "direct" else "contextual")
                                val body = JSONObject().put("source", "android.relay.manual").put("type", "thought.note")
                                    .put("occurred_at", Instant.now().toString()).put("facts", facts)
                                    .put("entities", JSONArray()).put("confidence", 1.0).put("sensitivity", "personal")
                                    .put("consent_scope", "owner.explicit_relay")
                                enqueue("event.create", body) { draft = ""; directProblem = false }
                            }) { Text(if (current.enabled) "确认发给电脑" else "保存，恢复后发给电脑") }
                            OutlinedButton(enabled = !busy, onClick = { enqueue("snapshot.get", JSONObject()) }) { Text("向电脑刷新目标和建议") }
                            RelayRecords(current.partition, onFeedback = { id, kind ->
                                enqueue("feedback.create", JSONObject().put("advice_id", id).put("kind", kind).put("note", ""))
                            })
                        }
                    }
                }
            }
        }
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (true) {
                    if (settings?.enabled == true && !busy) syncNow()
                    delay(30_000)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        acceptSharedDraft(intent)
    }

    private fun acceptSharedDraft(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND && intent.type == "text/plain") {
            val shared = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty()
            if (shared.isNotBlank()) {
                draft = shared.take(8000)
                directProblem = false
                status = "分享内容尚未发送，请检查后点击“确认发给电脑”"
            }
        }
    }

    private fun saveConnection(value: RelaySettings) {
        lifecycleScope.launch {
            try {
                settings = withContext(Dispatchers.IO) { RelaySettingsStore(this@RelayActivity).save(value.copy(enabled = true)) }
                imported = null
                savedGeneration++
                runCatching { RelaySyncWorker.configure(this@RelayActivity) }
                status = "已启用连接。更换设备或服务器会显示对应的独立记录，原记录仍保留。"
            } catch (error: Exception) { status = error.message ?: "无法保存连接" }
        }
    }

    private fun toggleEnabled() {
        val previous = settings ?: return
        lifecycleScope.launch {
            try {
                settings = withContext(Dispatchers.IO) { RelaySettingsStore(this@RelayActivity).save(previous.copy(enabled = !previous.enabled)) }
                runCatching { RelaySyncWorker.configure(this@RelayActivity) }
                status = if (settings?.enabled == true) "已恢复同步" else "已暂停后续传输；已暂存云端的内容仍可由电脑接收"
            } catch (_: Exception) { status = "无法保存暂停设置，请重试" }
        }
    }

    private fun enqueue(operation: String, body: JSONObject, onSaved: () -> Unit = {}) {
        lifecycleScope.launch {
            try {
                val cleaned = OutboundCredentialSanitizer.sanitize(body)
                RelaySync(this@RelayActivity).enqueue(operation, cleaned.payload)
                onSaved()
                status = if (cleaned.redacted) "已排队；检测到的凭据内容已隐藏" else "已保存在手机并排队，电脑回执后标记完成"
            } catch (_: Exception) { status = "未确认保存结果，请先查看最近请求，再决定是否重新提交" }
        }
    }

    private fun runSync() { lifecycleScope.launch { syncNow() } }
    private suspend fun syncNow() {
        if (busy) return
        busy = true
        try { status = RelaySync(this).sync() }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { status = "本轮同步未完成；本机内容保留。请检查网络、连接配置及电脑状态后重试。" }
        finally { busy = false }
    }

    @Composable
    private fun RelayRecords(partition: String, onFeedback: (String, String) -> Unit) {
        val dao = remember { RelayDatabase.get(this).dao() }
        val records by remember(partition) { dao.observeRecords(partition) }.collectAsState(emptyList())
        val requests by remember(partition) { dao.observeRequests(partition) }.collectAsState(emptyList())
        Text("电脑确认的目标与建议", style = MaterialTheme.typography.titleMedium)
        records.take(100).forEach { record ->
            val json = remember(record.payload) { JSONObject(record.payload) }
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    if (record.kind == "goal") {
                        Text(json.optString("title"), style = MaterialTheme.typography.titleSmall)
                        Text(json.optString("quote"))
                    } else {
                        Text(json.optString("action"), style = MaterialTheme.typography.titleSmall)
                        Text("第一步：${json.optString("first_step")}")
                        val evidence = json.optJSONArray("evidence")
                        if (evidence != null && evidence.length() > 0) Text("依据：${evidence.optJSONObject(0)?.optString("fact").orEmpty()}")
                        Text("目标：${json.optString("goal_quote")} · 状态：${json.optString("status")}")
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            TextButton(onClick = { onFeedback(record.id, "adopted") }) { Text("采纳") }
                            TextButton(onClick = { onFeedback(record.id, "irrelevant") }) { Text("不适用") }
                        }
                    }
                }
            }
        }
        if (records.isEmpty()) Text("还没有电脑回传内容。先保存目标并发一条记录。")
        Text("最近请求", style = MaterialTheme.typography.titleMedium)
        requests.take(20).forEach {
            val label = when (it.operation) { "goal.create" -> "目标"; "event.create" -> "记录"; "feedback.create" -> "反馈"; else -> "刷新" }
            val state = when (it.state) { "completed" -> "电脑已处理"; "rejected" -> "电脑未接受，请检查内容后重新提交"; else -> if (it.uploadedAt == 0L) "等待发送" else "云端暂存，等待电脑回执" }
            Text("$label · $state", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun ConnectionForm(current: RelaySettings?, imported: RelaySettings?, generation: Int, onImport: () -> Unit, onSave: (RelaySettings) -> Unit) {
    val initial = imported ?: current ?: RelaySettings()
    var url by remember(imported, generation) { mutableStateOf(initial.url) }
    var device by remember(imported, generation) { mutableStateOf(initial.device) }
    var peer by remember(imported, generation) { mutableStateOf(initial.peer) }
    var token by remember(imported, generation) { mutableStateOf(initial.token) }
    var expanded by remember { mutableStateOf(current == null) }
    LaunchedEffect(imported) { if (imported != null) expanded = true }
    OutlinedButton(onClick = { expanded = !expanded }) { Text(if (expanded) "收起连接设置" else "连接设置") }
    if (expanded) {
        OutlinedButton(onClick = onImport) { Text("从私密配置文件导入") }
        OutlinedTextField(url, { url = it.take(500) }, label = { Text("HTTPS 中转地址") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(device, { device = it.take(48) }, label = { Text("手机设备名称") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(peer, { peer = it.take(48) }, label = { Text("处理电脑的设备名称") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(token, { token = it.take(512) }, label = { Text("手机设备令牌") }, singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        Button(onClick = { onSave(RelaySettings(url.trim().trimEnd('/'), device.trim(), peer.trim(), token.trim())) }) { Text("保存并启用") }
    }
}

@Composable
private fun GoalForm(onSend: (JSONObject) -> Unit) {
    var title by remember { mutableStateOf("") }
    var quote by remember { mutableStateOf("") }
    var domain by remember { mutableStateOf("general") }
    Text("告诉电脑你的目标", style = MaterialTheme.typography.titleMedium)
    OutlinedTextField(title, { title = it.take(200) }, label = { Text("目标名称") }, modifier = Modifier.fillMaxWidth())
    OutlinedTextField(quote, { quote = it.take(4000) }, label = { Text("具体要求和完成标准") }, modifier = Modifier.fillMaxWidth())
    OutlinedTextField(domain, { domain = it.take(80) }, label = { Text("领域，例如 general、work、learning") }, modifier = Modifier.fillMaxWidth())
    Button(enabled = title.isNotBlank() && quote.isNotBlank() && domain.isNotBlank(), onClick = {
        onSend(JSONObject().put("title", title.trim()).put("quote", quote.trim()).put("domain", domain.trim())
            .put("target", JSONObject()).put("is_redline", false))
    }) { Text("保存并排队发送目标") }
}

private fun java.io.InputStream.readBytesBounded(limit: Int): ByteArray {
    val output = java.io.ByteArrayOutputStream()
    val buffer = ByteArray(4096)
    while (true) {
        val count = read(buffer)
        if (count < 0) break
        require(output.size() + count <= limit)
        output.write(buffer, 0, count)
    }
    return output.toByteArray()
}
