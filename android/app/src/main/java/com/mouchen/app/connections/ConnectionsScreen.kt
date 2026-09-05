package com.mouchen.app.connections

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
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
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import com.mouchen.app.collectors.CollectionWorker
import com.mouchen.app.collectors.MailAuthMode
import com.mouchen.app.localization.MouchenText as Text
import com.mouchen.app.sync.AudioProcessingLocation
import kotlinx.coroutines.launch
import java.util.Locale

private val ConnectionGreen = Color(0xFF356859)
private val ConnectionRed = Color(0xFF9F2D35)

@Composable
fun ConnectionsScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val repository = remember(context) { ConnectionSettingsRepository(context) }
    val scope = rememberCoroutineScope()
    var form by remember { mutableStateOf<ConnectionSettingsForm?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var messageIsError by remember { mutableStateOf(false) }
    var loadAttempt by remember { mutableIntStateOf(0) }
    var ollamaProbe by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(repository, loadAttempt) {
        runCatching { repository.load() }
            .onSuccess {
                form = it
                message = null
            }
            .onFailure {
                message = it.message ?: "连接配置读取失败"
                messageIsError = true
            }
    }

    val current = form
    if (current == null) {
        Column(
            modifier = modifier.fillMaxSize(),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (message == null) {
                CircularProgressIndicator()
            } else {
                Text(message.orEmpty(), color = ConnectionRed)
                OutlinedButton(onClick = { loadAttempt++ }) { Text("重试") }
            }
        }
        return
    }

    fun report(result: Result<Unit>, success: String) {
        result.fold(
            onSuccess = {
                message = success
                messageIsError = false
            },
            onFailure = {
                message = it.message ?: "保存失败"
                messageIsError = true
            },
        )
    }

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item { DeviceCapabilitySection(current) }
        message?.let { status ->
            item {
                Surface(
                    color = if (messageIsError) Color(0xFFFFEDEE) else Color(0xFFE8F3EE),
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(
                        status,
                        color = if (messageIsError) ConnectionRed else ConnectionGreen,
                        modifier = Modifier.padding(12.dp),
                    )
                }
            }
        }
        item {
            ConnectionSection(
                title = "AI替身 Backend",
                enabled = current.backendEnabled,
                onEnabledChange = { form = current.copy(backendEnabled = it) },
                onSave = {
                    scope.launch {
                        val result = runCatching { repository.saveBackend(current) }
                        report(result, "Backend 已加密保存")
                        if (result.isSuccess) CollectionWorker.enqueueImmediate(context)
                    }
                },
            ) {
                ConnectionField("URL", current.backendUrl) { form = current.copy(backendUrl = it) }
                Text(
                    "账号身份由登录服务器确认：${current.backendUser.ifBlank { "尚未登录" }}。访问令牌不可在设置中查看或编辑。",
                    color = Color(0xFF59615D),
                    style = MaterialTheme.typography.bodySmall,
                )
                ConnectionField("录音转文字地址（留空则不上传录音）", current.sttBaseUrl) {
                    form = current.copy(sttBaseUrl = it)
                }
                Text("录音在哪里转成文字", style = MaterialTheme.typography.labelLarge)
                Column {
                    ProcessingLocationChoice(
                        "我的电脑或可信局域网",
                        current.sttProcessingLocation == AudioProcessingLocation.TRUSTED_LAN,
                    ) { form = current.copy(sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN) }
                    ProcessingLocationChoice(
                        "我的私人服务器",
                        current.sttProcessingLocation == AudioProcessingLocation.PRIVATE_VPS,
                    ) { form = current.copy(sttProcessingLocation = AudioProcessingLocation.PRIVATE_VPS) }
                    ProcessingLocationChoice(
                        "公共云语音服务",
                        current.sttProcessingLocation == AudioProcessingLocation.PUBLIC_CLOUD,
                    ) { form = current.copy(sttProcessingLocation = AudioProcessingLocation.PUBLIC_CLOUD) }
                }
                Text(
                    "当前版本没有手机本地转写能力。只要填写转写地址，原始录音就会离开手机；AI替身会按你选择的位置如实记录。",
                    color = Color(0xFF9A641A),
                    style = MaterialTheme.typography.bodySmall,
                )
                ToggleLine("仅同步脱敏最小片段", current.backendMinimizedContextOnly) {
                    form = current.copy(backendMinimizedContextOnly = it)
                }
                ToggleLine("主动云分析（GPT-5.6 Sol）", current.backendProactiveCloud) {
                    form = current.copy(backendProactiveCloud = it)
                }
                Text(
                    "模型通道由后端决定：API Key 通道可用 Responses Pro；Codex 登录通道仅为 GPT-5.6 Sol 临时通道。L3 风险会再请求 Claude Code 复核。",
                    color = Color(0xFF59615D),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
        item {
            ConnectionSection(
                title = "Ollama 私有节点",
                enabled = current.ollamaEnabled,
                onEnabledChange = { form = current.copy(ollamaEnabled = it) },
                onSave = {
                    scope.launch {
                        report(runCatching { repository.saveOllama(current) }, "Ollama 已加密保存")
                    }
                },
                secondaryLabel = "保存并测试",
                onSecondary = {
                    scope.launch {
                        val result = runCatching {
                            repository.saveOllama(current)
                            repository.probeOllama()
                        }
                        result.onSuccess { probe ->
                            ollamaProbe = when {
                                !probe.reachable -> "不可达：${probe.reason ?: "未知错误"}"
                                probe.availableModels.isEmpty() -> "已连接，未发现模型"
                                else -> "已连接：${probe.availableModels.size} 个模型，${probe.sevenBOrLargerModels.size} 个 7B+"
                            }
                            message = "Ollama 探测完成"
                            messageIsError = !probe.reachable
                        }.onFailure {
                            ollamaProbe = "不可达：${it.message ?: "未知错误"}"
                            message = "Ollama 探测失败"
                            messageIsError = true
                        }
                    }
                },
            ) {
                ConnectionField("URL", current.ollamaUrl) { form = current.copy(ollamaUrl = it) }
                ConnectionField("模型", current.ollamaModel) { form = current.copy(ollamaModel = it) }
                SecretField("访问 Token", current.ollamaToken) { form = current.copy(ollamaToken = it) }
                ollamaProbe?.let { Text(it, color = Color(0xFF59615D), style = MaterialTheme.typography.bodySmall) }
            }
        }
        item {
            ConnectionSection(
                title = "外情 RSS",
                enabled = current.rssEnabled,
                onEnabledChange = { form = current.copy(rssEnabled = it) },
                onSave = {
                    scope.launch {
                        val result = runCatching { repository.saveRss(current) }
                        report(result, "RSS 已加密保存")
                        if (result.isSuccess) CollectionWorker.enqueueImmediate(context)
                    }
                },
            ) {
                ConnectionField("源 URL", current.rssUrl) { form = current.copy(rssUrl = it) }
                ConnectionField("目标领域", current.rssDomain) { form = current.copy(rssDomain = it) }
                ConnectionField("关键词（逗号分隔）", current.rssKeywords, singleLine = false) {
                    form = current.copy(rssKeywords = it)
                }
                ToggleLine("匹配项作为威胁", current.rssThreats) { form = current.copy(rssThreats = it) }
            }
        }
        item {
            ConnectionSection(
                title = "邮件 IMAP",
                enabled = current.imapEnabled,
                onEnabledChange = { form = current.copy(imapEnabled = it) },
                onSave = {
                    scope.launch {
                        val result = runCatching { repository.saveImap(current) }
                        report(result, "IMAP 已加密保存")
                        if (result.isSuccess) CollectionWorker.enqueueImmediate(context)
                    }
                },
            ) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    ConnectionField(
                        label = "主机",
                        value = current.imapHost,
                        modifier = Modifier.weight(2f),
                        onChange = { form = current.copy(imapHost = it) },
                    )
                    ConnectionField(
                        label = "端口",
                        value = current.imapPort,
                        modifier = Modifier.weight(1f),
                        keyboardType = KeyboardType.Number,
                        onChange = { form = current.copy(imapPort = it) },
                    )
                }
                ConnectionField("邮箱", current.imapEmail) { form = current.copy(imapEmail = it) }
                ConnectionField("目标领域", current.imapDomain) { form = current.copy(imapDomain = it) }
                ConnectionField("用户名", current.imapUsername) { form = current.copy(imapUsername = it) }
                SecretField(
                    if (current.imapAuthMode == MailAuthMode.APP_PASSWORD) "应用专用密码" else "OAuth Token",
                    current.imapSecret,
                ) { form = current.copy(imapSecret = it) }
                Text("认证方式", style = MaterialTheme.typography.labelLarge)
                Row(Modifier.fillMaxWidth()) {
                    AuthChoice(
                        "应用密码",
                        current.imapAuthMode == MailAuthMode.APP_PASSWORD,
                    ) { form = current.copy(imapAuthMode = MailAuthMode.APP_PASSWORD) }
                    AuthChoice(
                        "OAuth Token",
                        current.imapAuthMode == MailAuthMode.OAUTH2_ACCESS_TOKEN,
                    ) { form = current.copy(imapAuthMode = MailAuthMode.OAUTH2_ACCESS_TOKEN) }
                }
                ToggleLine("TLS 加密", current.imapTls) { form = current.copy(imapTls = it) }
            }
        }
    }
}

@Composable
private fun DeviceCapabilitySection(form: ConnectionSettingsForm) {
    val capability = form.deviceCapability
    val memoryGb = capability.totalMemoryBytes.toDouble() / (1024.0 * 1024.0 * 1024.0)
    Surface(color = Color.White, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("本机 7B 能力", fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                Text(
                    if (capability.suitableForOptional7B) "适合可选运行" else "建议使用私有节点",
                    color = if (capability.suitableForOptional7B) ConnectionGreen else Color(0xFF9A641A),
                    style = MaterialTheme.typography.labelLarge,
                )
            }
            Text(
                "${if (capability.arm64) "ARM64" else "非 ARM64"} · ${String.format(Locale.US, "%.1f", memoryGb)} GB RAM",
                color = Color(0xFF59615D),
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun ConnectionSection(
    title: String,
    enabled: Boolean,
    onEnabledChange: (Boolean) -> Unit,
    onSave: () -> Unit,
    secondaryLabel: String? = null,
    onSecondary: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    Surface(color = Color.White, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
                Switch(checked = enabled, onCheckedChange = onEnabledChange)
            }
            content()
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                if (secondaryLabel != null && onSecondary != null) {
                    OutlinedButton(onClick = onSecondary, shape = RoundedCornerShape(6.dp)) {
                        Text(secondaryLabel)
                    }
                    Spacer(Modifier.width(8.dp))
                }
                Button(onClick = onSave, shape = RoundedCornerShape(6.dp)) { Text("保存") }
            }
        }
    }
}

@Composable
private fun ConnectionField(
    label: String,
    value: String,
    modifier: Modifier = Modifier.fillMaxWidth(),
    singleLine: Boolean = true,
    keyboardType: KeyboardType = KeyboardType.Text,
    onChange: (String) -> Unit,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label) },
        modifier = modifier,
        singleLine = singleLine,
        minLines = if (singleLine) 1 else 2,
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
    )
}

@Composable
private fun SecretField(label: String, value: String, onChange: (String) -> Unit) {
    var visible by remember { mutableStateOf(false) }
    OutlinedTextField(
        value = value,
        onValueChange = onChange,
        label = { Text(label) },
        modifier = Modifier.fillMaxWidth(),
        singleLine = true,
        visualTransformation = if (visible) VisualTransformation.None else PasswordVisualTransformation(),
        trailingIcon = {
            TextButtonCompact(if (visible) "隐藏" else "显示") { visible = !visible }
        },
    )
}

@Composable
private fun TextButtonCompact(text: String, onClick: () -> Unit) {
    androidx.compose.material3.TextButton(onClick = onClick, contentPadding = PaddingValues(horizontal = 8.dp)) {
        Text(text)
    }
}

@Composable
private fun ToggleLine(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun AuthChoice(label: String, selected: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier.padding(end = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = onClick)
        Text(label)
    }
}

@Composable
private fun ProcessingLocationChoice(label: String, selected: Boolean, onClick: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = onClick)
        Text(label)
    }
}
