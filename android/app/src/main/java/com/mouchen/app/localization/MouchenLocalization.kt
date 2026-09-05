package com.mouchen.app.localization

import android.content.Context
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.Text as MaterialText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.sp
import java.util.Locale

internal const val LOCALE_ZH_CN = "zh-CN"
internal const val LOCALE_EN_US = "en-US"

internal fun normalizeMouchenLocale(value: String?): String? = when (value?.trim()?.replace('_', '-')) {
    "zh-CN", "zh-cn", "zh-Hans", "zh-hans" -> LOCALE_ZH_CN
    "en-US", "en-us", "en" -> LOCALE_EN_US
    else -> null
}

internal fun systemMouchenLocale(locale: Locale = Locale.getDefault()): String =
    if (locale.language.equals("zh", ignoreCase = true)) LOCALE_ZH_CN else LOCALE_EN_US

internal class MouchenLocaleStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun load(userId: String? = null): String = normalizeMouchenLocale(
        userId?.takeIf(String::isNotBlank)?.let { preferences.getString("$ACCOUNT_PREFIX$it", null) },
    ) ?: normalizeMouchenLocale(preferences.getString(LAST_ACTIVE, null)) ?: systemMouchenLocale()

    /** Signed-out UI follows the system until the user explicitly chooses a language. */
    fun loadPreLogin(): String =
        normalizeMouchenLocale(preferences.getString(PRE_LOGIN, null)) ?: systemMouchenLocale()

    fun saveAccount(userId: String, locale: String): Boolean {
        val normalized = normalizeMouchenLocale(locale) ?: return false
        if (userId.isBlank()) return false
        return preferences.edit()
            .putString("$ACCOUNT_PREFIX$userId", normalized)
            .putString(LAST_ACTIVE, normalized)
            .commit()
    }

    fun savePreLogin(locale: String): Boolean {
        val normalized = normalizeMouchenLocale(locale) ?: return false
        return preferences.edit()
            .putString(PRE_LOGIN, normalized)
            .putString(LAST_ACTIVE, normalized)
            .commit()
    }

    private companion object {
        const val PREFERENCES = "mouchen_locale_preferences"
        const val ACCOUNT_PREFIX = "account."
        const val LAST_ACTIVE = "last_active"
        const val PRE_LOGIN = "pre_login"
    }
}

internal val LocalMouchenLocale = compositionLocalOf { systemMouchenLocale() }

@Composable
internal fun MouchenLocaleProvider(locale: String, content: @Composable () -> Unit) {
    CompositionLocalProvider(LocalMouchenLocale provides (normalizeMouchenLocale(locale) ?: systemMouchenLocale())) {
        content()
    }
}

internal fun isEnglish(locale: String): Boolean = normalizeMouchenLocale(locale) == LOCALE_EN_US

@Composable
internal fun uiText(value: String): String = localizeUiText(value, LocalMouchenLocale.current)

internal fun Context.uiText(value: String): String =
    localizeUiText(value, MouchenLocaleStore(this).load())

internal fun Context.accountText(english: String, chinese: String): String =
    if (isEnglish(MouchenLocaleStore(this).load())) english else chinese

/** Localizes application chrome only. Unknown text is returned byte-for-byte so evidence is never translated. */
internal fun localizeUiText(value: String, locale: String): String {
    if (!isEnglish(locale) || value.isBlank()) return value
    ENGLISH[value]?.let { return it }
    return localizeDynamic(value)
}

@Composable
internal fun MouchenText(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = Color.Unspecified,
    fontSize: TextUnit = TextUnit.Unspecified,
    fontStyle: FontStyle? = null,
    fontWeight: FontWeight? = null,
    fontFamily: FontFamily? = null,
    letterSpacing: TextUnit = TextUnit.Unspecified,
    textDecoration: TextDecoration? = null,
    textAlign: TextAlign? = null,
    lineHeight: TextUnit = TextUnit.Unspecified,
    overflow: TextOverflow = TextOverflow.Clip,
    softWrap: Boolean = true,
    maxLines: Int = Int.MAX_VALUE,
    minLines: Int = 1,
    onTextLayout: ((TextLayoutResult) -> Unit)? = null,
    style: TextStyle = LocalTextStyle.current,
) {
    MaterialText(
        text = uiText(text), modifier = modifier, color = color, fontSize = fontSize,
        fontStyle = fontStyle, fontWeight = fontWeight, fontFamily = fontFamily,
        letterSpacing = letterSpacing, textDecoration = textDecoration, textAlign = textAlign,
        lineHeight = lineHeight, overflow = overflow, softWrap = softWrap, maxLines = maxLines,
        minLines = minLines, onTextLayout = onTextLayout, style = style,
    )
}

private fun localizeDynamic(value: String): String {
    val ownerPrefix = "把你正在想、刚看到、刚听到或遇到的情况记在这里。这不是提问；规则会立即判断，开启主动云分析后还会发现规则外问题。"
    if (value.startsWith(ownerPrefix)) {
        return "Record what you are thinking, seeing, hearing, or facing. This is not a question: rules assess it immediately, and proactive cloud analysis can discover issues beyond the rules." +
            localizeUiText(value.removePrefix(ownerPrefix), LOCALE_EN_US)
    }
    Regex("^L(\\d+) · (.+)$").matchEntire(value)?.let { return "L${it.groupValues[1]} · ${it.groupValues[2]}" }
    Regex("^再次提醒 · L(\\d+) · (.+)$").matchEntire(value)?.let { return "Reminder · L${it.groupValues[1]} · ${it.groupValues[2]}" }
    Regex("^(.+) 条$").matchEntire(value)?.let { return "${it.groupValues[1]} items" }
    Regex("^固定服务器：(.+)$").matchEntire(value)?.let { return "Fixed server: ${it.groupValues[1]}" }
    Regex("^选择前，AI替身不会采集、同步或弹出建言。服务器已固定为 (.+)，领取时不能更改。$").matchEntire(value)?.let {
        return "Until you choose, My AI Twin will not collect, sync, or notify advice. The server is fixed to ${it.groupValues[1]} and cannot be changed during claim."
    }
    Regex("^账号身份由登录服务器确认：(.+)。访问令牌不可在设置中查看或编辑。$").matchEntire(value)?.let {
        return "Account identity confirmed by the sign-in server: ${it.groupValues[1]}. Access tokens cannot be viewed or edited in Settings."
    }
    Regex("^已连接：(\\d+) 个模型，(\\d+) 个 7B\\+$").matchEntire(value)?.let {
        return "Connected: ${it.groupValues[1]} models, ${it.groupValues[2]} with 7B+ parameters"
    }
    Regex("^不可达：(.*)$").matchEntire(value)?.let { return "Unreachable: ${it.groupValues[1]}" }
    Regex("^正在启动(.+)…$").matchEntire(value)?.let {
        return "Starting ${localizeUiText(it.groupValues[1], LOCALE_EN_US)}…"
    }
    Regex("^(.+)正在占用麦克风，请停止后重试$").matchEntire(value)?.let {
        return "${localizeUiText(it.groupValues[1], LOCALE_EN_US)} is using the microphone. Stop it and try again."
    }
    Regex("^双拼  (.+)$").matchEntire(value)?.let { return "Double pinyin  ${it.groupValues[1]}" }
    Regex("^静默时段 (.+)$").matchEntire(value)?.let { return "Quiet hours ${it.groupValues[1]}" }
    Regex("^本地待同步：(\\d+) 条事件，(\\d+) 个目标$").matchEntire(value)?.let {
        return "Pending locally: ${it.groupValues[1]} events, ${it.groupValues[2]} goals"
    }
    Regex("^本轮 (\\d+)/(\\d+) 成功，(\\d+) 失败$").matchEntire(value)?.let {
        return "This run: ${it.groupValues[1]}/${it.groupValues[2]} succeeded, ${it.groupValues[3]} failed"
    }
    Regex("^已导入 (\\d+) 段对话文本(并请求同步|，当前未能排队同步)$").matchEntire(value)?.let {
        return if (it.groupValues[2] == "并请求同步")
            "Imported ${it.groupValues[1]} conversation segments and requested sync"
        else "Imported ${it.groupValues[1]} conversation segments; sync could not be queued"
    }
    Regex("^到核验时点 · (.+)$").matchEntire(value)?.let { return "Review due · ${it.groupValues[1]}" }
    Regex("^第一步：(.+)$").matchEntire(value)?.let { return "First step: ${it.groupValues[1]}" }
    Regex("^依据：(.+)$").matchEntire(value)?.let { return "Evidence: ${it.groupValues[1]}" }
    Regex("^核验：(.+)$").matchEntire(value)?.let { return "Review: ${it.groupValues[1]}" }
    Regex("^替代路径：(.+)$").matchEntire(value)?.let { return "Alternative: ${it.groupValues[1]}" }
    Regex("^失败环节：(.+)$").matchEntire(value)?.let { return "Failed stage: ${it.groupValues[1]}" }
    Regex("^反馈失败：HTTP (\\d+)$").matchEntire(value)?.let { return "Feedback failed: HTTP ${it.groupValues[1]}" }
    Regex("^方向提交失败：HTTP (\\d+)$").matchEntire(value)?.let { return "Guidance failed: HTTP ${it.groupValues[1]}" }
    Regex("^结果提交失败：HTTP (\\d+)$").matchEntire(value)?.let { return "Outcome submission failed: HTTP ${it.groupValues[1]}" }
    Regex("^方向说明不能超过 (\\d+) 个字$").matchEntire(value)?.let { return "Guidance cannot exceed ${it.groupValues[1]} characters" }
    return value
}

private val ENGLISH = mapOf(
    "AI替身" to "My AI Twin",
    "局势" to "Situation",
    "谏言" to "Advice",
    "目标" to "Goals",
    "连接" to "Connections",
    "权限" to "Permissions",
    "设置" to "Settings",
    "登录" to "Sign in",
    "注册" to "Create account",
    "退出" to "Sign out",
    "返回" to "Back",
    "取消" to "Cancel",
    "确认" to "Confirm",
    "保存" to "Save",
    "停止" to "Stop",
    "启动" to "Start",
    "重试" to "Retry",
    "授权" to "Authorize",
    "已登录" to "Signed in",
    "已停止" to "Stopped",
    "运行中" to "Running",
    "正在确认安全登录…" to "Verifying secure sign-in…",
    "正在登录…" to "Signing in…",
    "正在注册…" to "Creating account…",
    "正在安全领取主人账号…" to "Securely claiming the owner account…",
    "登录状态已失效，请重新登录" to "Your session expired. Please sign in again.",
    "安全登录" to "Secure sign-in",
    "注册并登录" to "Create account and sign in",
    "用户名" to "Username",
    "密码" to "Password",
    "密码（至少10个字符）" to "Password (at least 10 characters)",
    "密码（至少 10 个字符）" to "Password (at least 10 characters)",
    "邀请码（开放注册时可留空）" to "Invitation code (optional when registration is open)",
    "服务器地址（HTTPS）" to "Server address (HTTPS)",
    "登录后继续了解你的局势" to "Sign in to continue understanding your situation",
    "建立独立、加密的AI替身账号" to "Create an isolated, encrypted My AI Twin account",
    "密码只用于本次登录请求，不保存在手机；登录令牌由 Android Keystore 加密保存。" to
        "Your password is used only for this sign-in request and is never stored on the phone. The Android Keystore encrypts your session token.",
    "服务器地址必须使用 HTTPS" to "The server address must use HTTPS",
    "语言未能同步到账号，请检查网络后重试" to "Language could not be synced to your account. Check the network and try again.",
    "全文先加密保存在手机；后端连接已关闭，当前不会上云。" to
        " Full text is encrypted on the phone. The backend is disabled, so it is not sent to the cloud.",
    "全文先加密保存在手机；同步时允许发送完整上下文，但密码、验证码和支付密钥仍会剔除。" to
        " Full text is encrypted on the phone. Sync may send full context, but passwords, verification codes, and payment secrets are still removed.",
    "全文先加密保存在手机；同步时只发送脱敏的最小片段。" to
        " Full text is encrypted on the phone. Sync sends only redacted minimal excerpts.",
    "系统已允许忽略电池优化，但 Android 仍可能终止进程；这不是常驻保证。" to
        "Battery optimization exemption is enabled, but Android may still stop the process; continuous operation is not guaranteed.",
    "系统省电可能延迟后台采集和同步。AI替身不会自动修改设置，请按需手动选择。" to
        "Android power saving may delay background collection and sync. My AI Twin does not change this setting automatically.",
    "录音在手机加密分段；后端连接已关闭，原始录音不会上传，也不会生成转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone. The backend is disabled, so raw audio is not uploaded or transcribed. Tap Stop to end.",
    "录音在手机加密分段；未配置转写地址，原始录音不会上传，也不会生成转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone. No transcription URL is configured, so raw audio is not uploaded or transcribed. Tap Stop to end.",
    "录音在手机加密分段；尚未配置访问 Token，原始录音不会上传，也不会生成转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone. No access token is configured, so raw audio is not uploaded or transcribed. Tap Stop to end.",
    "录音在手机加密分段；原始录音会通过 HTTPS 发送到你配置的电脑或可信局域网节点转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone, then sent over HTTPS to your configured computer or trusted LAN node for transcription. Tap Stop to end.",
    "录音在手机加密分段；原始录音会通过 HTTPS 发送到你配置的私人服务器转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone, then sent over HTTPS to your configured private server for transcription. Tap Stop to end.",
    "录音在手机加密分段；原始录音会通过 HTTPS 发送到你配置的公共云语音服务转写。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone, then sent over HTTPS to your configured public cloud speech service for transcription. Tap Stop to end.",
    "录音在手机加密分段；当前版本不支持手机本地转写，原始录音不会上传。 点按“停止”可结束。" to
        "Audio is encrypted in segments on the phone. This build has no on-device transcription, so raw audio is not uploaded. Tap Stop to end.",
    "用户名需为 3–64 个字符" to "Username must contain 3–64 characters",
    "用户名不能包含空格或控制字符" to "Username cannot contain spaces or control characters",
    "请输入密码" to "Enter your password",
    "密码至少需要 10 个字符" to "Password must contain at least 10 characters",
    "密码过长" to "Password is too long",
    "已安全退出" to "Signed out securely",
    "安全退出未完全确认；请联网后重新打开应用再试" to "Sign-out could not be fully confirmed. Reconnect, reopen the app, and try again.",
    "领取AI替身主人账号" to "Claim the My AI Twin owner account",
    "领取主人账号" to "Claim owner account",
    "已找到旧版主人数据。领取后可用账号密码在 Windows、Android 和 iPhone 共享同一份云端数据。" to
        "Legacy owner data was found. Claim it to share the same cloud data across Windows, Android, and iPhone.",
    "本次暂时继续旧模式" to "Continue legacy mode this session",
    "暂时继续只在本次应用进程有效；手机重启或应用进程结束后会再次询问。" to
        "This temporary choice lasts only for this app process. My AI Twin will ask again after a restart.",
    "主人账号已经在其他设备领取：直接登录。" to "The owner account was already claimed on another device. Sign in directly.",
    "首次领取：使用主人邀请码注册。" to "First claim: create an account with the owner invitation code.",
    "登录已领取账号" to "Sign in to claimed account",
    "首次领取" to "First claim",
    "主人邀请码" to "Owner invitation code",
    "登录并领取" to "Sign in and claim",
    "注册并领取" to "Create account and claim",
    "只有服务器返回的账号身份与旧主人身份完全一致才会迁移。错误账号会被撤销，不会清除旧队列和授权。" to
        "Migration occurs only when the server identity exactly matches the legacy owner. A wrong session is revoked without clearing the legacy queue or permissions.",
    "立即扫描并同步" to "Scan and sync now",
    "已了解信息" to "Information understood",
    "尚未收到手机信息" to "No phone information received yet",
    "当前战线" to "Active fronts",
    "尚未建立目标" to "No goals yet",
    "下一步" to "Next steps",
    "暂无待处理谏言" to "No pending advice",
    "暂无谏言" to "No advice yet",
    "先告诉AI替身你要去哪里" to "First, tell My AI Twin where you want to go",
    "没有你的目标原话，系统只能保存信息，不能判断什么值得主动提醒。" to
        "Without your goal in your own words, My AI Twin can store information but cannot judge what deserves proactive attention.",
    "建立第一个目标" to "Create your first goal",
    "近24小时" to "Last 24 hours",
    "信息来源" to "Sources",
    "基础权限" to "Core permissions",
    "待办谏言" to "Pending advice",
    "告诉AI替身" to "Tell My AI Twin",
    "想法、对话或当下情况" to "A thought, conversation, or current situation",
    "保存并立即研判" to "Save and assess now",
    "选择并导入对话文本（.txt）" to "Choose and import conversation text (.txt)",
    "只读取你在系统文件选择器中主动选择的文件；无法读取微信或其他 App 的私有聊天库。单文件最多 512 KB，并按上限分段。" to
        "My AI Twin reads only the file you explicitly choose. It cannot access private chat databases in WeChat or other apps. Maximum 512 KB per file, split into bounded segments.",
    "采集与同步" to "Collection and sync",
    "采集" to "Collection",
    "同步" to "Sync",
    "尚未运行" to "Not run yet",
    "等待执行" to "Queued",
    "执行中" to "Running",
    "已完成" to "Completed",
    "部分失败，等待重试" to "Partially failed; retry pending",
    "失败，等待重试" to "Failed; retry pending",
    "后端未启用，仅保存在本机" to "Backend disabled; stored only on this device",
    "需要重新登录" to "Sign-in required",
    "启用后端连接后会自动同步现有信息" to "Existing information will sync after the backend is enabled",
    "本地加密数据库暂不可用" to "The local encrypted database is temporarily unavailable",
    "后端暂未接收全部内容" to "The backend has not received all content yet",
    "数据较多，后台将继续分批同步" to "More data remains; background sync will continue in batches",
    "同步发生异常，后台将自动重试" to "Sync encountered an error and will retry automatically",
    "登录后将自动继续同步本地信息" to "Local information will resume syncing after sign-in",
    "我的自述" to "My note",
    "分享图片" to "Shared image",
    "对话导入" to "Conversation import",
    "通知" to "Notifications",
    "当前界面" to "Visible screen",
    "App 使用" to "App usage",
    "日历" to "Calendar",
    "输入内容" to "Typed content",
    "屏幕会话" to "Screen session",
    "录音会话" to "Audio session",
    "邮件" to "Email",
    "外部情报" to "External intelligence",
    "其他" to "Other",
    "谏言台账" to "Advice ledger",
    "采纳" to "Adopt",
    "稍后处理" to "Handle later",
    "知道了" to "Acknowledged",
    "言尽于此，不再说这条" to "Stop this topic",
    "事实有误" to "Fact is wrong",
    "预测有误" to "Prediction is wrong",
    "与我的目标无关" to "Irrelevant to my goals",
    "提醒时机不对" to "Bad timing",
    "给AI替身方向" to "Guide My AI Twin",
    "你的方向" to "Your guidance",
    "希望AI替身往哪个方向调整？" to "How should My AI Twin adjust its direction?",
    "例如：先关注现金流，不要再建议扩张" to "For example: prioritize cash flow and stop suggesting expansion",
    "发送方向" to "Send guidance",
    "这条建议的结果如何？" to "What was the outcome of this advice?",
    "结果达成" to "Outcome achieved",
    "结果未达成" to "Outcome not achieved",
    "这条谏言哪里不准？" to "What was wrong with this advice?",
    "静默与驾驶" to "Quiet hours and driving",
    "静默时段" to "Quiet hours",
    "静默时段未启用" to "Quiet hours are off",
    "驾驶模式已开启；非紧急进言将暂缓。" to "Driving mode is on; non-urgent advice is deferred.",
    "启用每日静默" to "Enable daily quiet hours",
    "开始（HH:mm）" to "Start (HH:mm)",
    "结束（HH:mm）" to "End (HH:mm)",
    "设置静默时段" to "Set quiet hours",
    "驾驶模式 2 小时" to "Driving mode for 2 hours",
    "结束驾驶模式" to "End driving mode",
    "系统勿扰开启时也会自动暂缓非紧急进言。" to "Android Do Not Disturb also defers non-urgent advice.",
    "开始与结束时间不能相同" to "Start and end times cannot be the same",
    "系统授权" to "System permissions",
    "通知推送" to "Advice notifications",
    "通知读取" to "Notification access",
    "应用使用记录" to "App usage access",
    "位置" to "Location",
    "麦克风" to "Microphone",
    "AI替身输入法" to "My AI Twin keyboard",
    "可见界面读取" to "Visible screen access",
    "后台运行" to "Background operation",
    "系统省电豁免：已允许" to "Battery optimization exemption: allowed",
    "系统省电豁免：未允许" to "Battery optimization exemption: not allowed",
    "打开系统省电设置" to "Open battery settings",
    "账号数据开关" to "Account data controls",
    "系统权限只代表 Android 允许访问；以下开关是当前账号对AI替身的独立授权。" to
        "Android permissions only allow access. The controls below are separate consent for this My AI Twin account.",
    "允许AI替身采集" to "Allow My AI Twin to collect",
    "允许主动云分析" to "Allow proactive cloud analysis",
    "允许完整上下文上云" to "Allow full context in cloud analysis",
    "默认关闭；只有当前账号在这里授权且连接页也允许时，才会发送净化后的完整上下文。" to
        "Off by default. Sanitized full context is sent only when this account and the Connections setting both allow it.",
    "关闭时不新增日历、位置、通知、可见界面、输入法、录音或屏幕事件。" to
        "When off, no new calendar, location, notification, visible-screen, keyboard, audio, or screen events are collected.",
    "关闭时不会为当前账号发送主动云分析授权；完整原文仍受连接页的单独开关限制。" to
        "When off, this account does not authorize proactive cloud analysis. Full raw context remains controlled separately in Connections.",
    "环境录音" to "Ambient audio",
    "屏幕采样" to "Screen capture",
    "截图加密保存在手机，并在手机本地识别中英文文字" to "Screenshots are encrypted on the phone and Chinese/English text is recognized on-device",
    "私测感知" to "Private-alpha sensing",
    "近 7 天短信" to "SMS from the last 7 days",
    "近 7 天通话记录" to "Calls from the last 7 days",
    "联系人（姓名与标识哈希）" to "Contacts (name and hashed identifier)",
    "联系人标识和通话只发送最小化字段；短信原文仅存本机，最小化上下文模式可能将脱敏后的短信片段发给已配置的模型。三项权限可分别授予或拒绝。" to
        "Contact identifiers and calls send minimized fields only. Raw SMS stays on-device; minimized-context mode may send redacted SMS excerpts to the configured model. Each permission can be granted or denied separately.",
    "请先在权限页开启当前账号的“允许AI替身采集”" to "Enable “Allow My AI Twin to collect” for this account on the Permissions page first",
    "没有收到可读取的图片" to "No readable image was received",
    "当前版本不支持图片文字识别" to "Image text recognition is unavailable in this build",
    "上一张图片仍在识别，请稍后再试" to "The previous image is still being recognized. Try again shortly.",
    "只支持由其他 App 授权分享的图片" to "Only images explicitly shared by another app are supported",
    "分享内容不是受支持的图片" to "The shared content is not a supported image",
    "图片读取授权已失效，请重新分享" to "Image access expired. Share it again.",
    "图片读取失败，请重试" to "Could not read the image. Try again.",
    "图片过大；单张最多 8 MB" to "The image is too large; maximum 8 MB",
    "图片格式无效或无法安全解码" to "The image is invalid or cannot be decoded safely",
    "图片中没有识别到足够文字" to "Not enough text was recognized in the image",
    "手机本地文字识别失败，请重试" to "On-device text recognition failed. Try again.",
    "图片文字未能保存，请重试" to "Image text could not be saved. Try again.",
    "图片文字已在手机识别并请求同步" to "Image text was recognized on-device and queued for sync",
    "图片文字已保存，当前未能排队同步" to "Image text was saved, but sync could not be queued",
    "只支持从系统文件选择器导入" to "Import only through the system file picker",
    "请选择 text/plain 或 .txt 文件" to "Choose a text/plain or .txt file",
    "文件过大；最多 512 KB、8 万字、24 段" to "File too large; maximum 512 KB, 80,000 characters, and 24 segments",
    "文件中没有可导入的文字" to "The file contains no importable text",
    "文件不是有效的 UTF-8 文本" to "The file is not valid UTF-8 text",
    "系统没有授予读取权限，请重新选择" to "Read access was not granted. Choose the file again.",
    "文件读取失败，请重试" to "Could not read the file. Try again.",
    "对话文本未能保存，请重试" to "Conversation text could not be saved. Try again.",
    "AI替身 Backend" to "My AI Twin Backend",
    "Ollama 私有节点" to "Private Ollama node",
    "外情 RSS" to "Intelligence RSS",
    "邮件 IMAP" to "Email IMAP",
    "保存并测试" to "Save and test",
    "保存失败" to "Save failed",
    "连接配置读取失败" to "Could not load connection settings",
    "Backend 已加密保存" to "Backend settings saved with encryption",
    "Ollama 已加密保存" to "Ollama settings saved with encryption",
    "RSS 已加密保存" to "RSS settings saved with encryption",
    "IMAP 已加密保存" to "IMAP settings saved with encryption",
    "Ollama 探测完成" to "Ollama probe completed",
    "Ollama 探测失败" to "Ollama probe failed",
    "录音转文字地址（留空则不上传录音）" to "Speech-to-text URL (leave blank to keep audio on-device)",
    "录音在哪里转成文字" to "Where audio is transcribed",
    "我的电脑或可信局域网" to "My computer or trusted LAN",
    "我的私人服务器" to "My private server",
    "公共云语音服务" to "Public cloud speech service",
    "当前版本没有手机本地转写能力。只要填写转写地址，原始录音就会离开手机；AI替身会按你选择的位置如实记录。" to
        "This build has no on-device transcription. If a transcription URL is set, raw audio leaves the phone; My AI Twin records the selected processing location.",
    "仅同步脱敏最小片段" to "Sync only redacted minimal excerpts",
    "主动云分析（GPT-5.6 Sol）" to "Proactive cloud analysis (GPT-5.6 Sol)",
    "模型通道由后端决定：API Key 通道可用 Responses Pro；Codex 登录通道仅为 GPT-5.6 Sol 临时通道。L3 风险会再请求 Claude Code 复核。" to
        "The backend selects the model route. API-key routes can use Responses Pro; Codex sign-in is a temporary GPT-5.6 Sol route. L3 risks request an additional Claude Code review.",
    "模型" to "Model",
    "访问 Token" to "Access token",
    "目标领域" to "Goal domain",
    "目标原文" to "Goal in your own words",
    "源 URL" to "Source URL",
    "关键词（逗号分隔）" to "Keywords (comma-separated)",
    "匹配项作为威胁" to "Treat matches as threats",
    "主机" to "Host",
    "端口" to "Port",
    "邮箱" to "Email address",
    "认证方式" to "Authentication",
    "应用密码" to "App password",
    "应用专用密码" to "App-specific password",
    "TLS 加密" to "TLS encryption",
    "显示" to "Show",
    "隐藏" to "Hide",
    "本机 7B 能力" to "On-device 7B capability",
    "适合可选运行" to "Suitable for optional use",
    "建议使用私有节点" to "Private node recommended",
    "已连接，未发现模型" to "Connected; no models found",
    "模型暂不可用，请先在连接页配置 Backend 或 Ollama。" to "No model is available. Configure Backend or Ollama in Connections.",
    "问AI替身" to "Ask My AI Twin",
    "请AI替身作答" to "Ask for an answer",
    "你的原话" to "Your words",
    "问题" to "Question",
    "领域" to "Domain",
    "事业" to "Work",
    "关注关键词（逗号分隔）" to "Focus keywords (comma-separated)",
    "相关 App 包名（可选，逗号分隔）" to "Related app package names (optional, comma-separated)",
    "每周计划投入小时（可选）" to "Planned hours per week (optional)",
    "设为红线域" to "Mark as a red-line domain",
    "红线" to "Red line",
    "新增目标" to "Add goal",
    "私有节点优先" to "Prefer private node",
    "简报" to "Brief",
    "推送时机" to "Notification timing",
    "允许本次问题和最小上下文上云" to "Allow this question and minimal context in cloud analysis",
    "这条不准，给AI替身纠错" to "Correct this advice",
    "旧主人会话已经变化，请重新打开应用确认" to "The legacy owner session changed. Reopen the app to verify it.",
    "当前账号尚未允许内容采集；系统权限不会覆盖这个账号开关，当前没有读取屏幕内容。" to
        "This account has not allowed content collection. Android permissions do not override the account control, and no screen content is being read.",
    "你未允许屏幕采样；当前没有读取屏幕内容。需要时请重新启动并授权。" to
        "Screen capture was not authorized, so no screen content is being read. Start it again and authorize when needed.",
    "屏幕采样授权已经结束；Android 要求每次会话由你重新授权，当前没有读取屏幕内容。" to
        "Screen capture authorization ended. Android requires authorization for every session, and no screen content is being read.",
    "屏幕采样授权无效；当前没有读取屏幕内容，请重新启动并授权。" to
        "Screen capture authorization is invalid. No screen content is being read; start again and authorize.",
    "屏幕采样启动失败；当前没有读取屏幕内容，请重新授权后重试。" to
        "Screen capture failed to start. No screen content is being read; authorize and try again.",
    "屏幕采样没有有效授权；当前没有读取屏幕内容，请重新启动并授权。" to
        "Screen capture has no valid authorization. No screen content is being read; start again and authorize.",
    "语音" to "Voice",
    "已取消语音识别" to "Voice recognition canceled",
    "当前输入框禁止使用语音" to "Voice input is unavailable in this field",
    "语音结果未能写入，请重新说一次" to "The voice result could not be inserted. Please try again.",
    "停止普通话录音" to "Stop Mandarin recording",
    "取消普通话识别" to "Cancel Mandarin recognition",
    "普通话语音输入" to "Mandarin voice input",
    "正在打开麦克风…" to "Opening the microphone…",
    "请说普通话；说完后点“停止”" to "Speak Mandarin, then tap Stop",
    "正在识别普通话…" to "Recognizing Mandarin…",
    "离线中文词库正在加载，请稍候" to "Loading the offline Chinese lexicon…",
    "离线词库加载失败，可切换 EN" to "Offline lexicon failed to load; switch to EN",
    "正在加载离线中文词库…" to "Loading the offline Chinese lexicon…",
    "英文直接输入" to "Direct English input",
    "输入自然码双拼，空格上屏" to "Type Natural Code double pinyin; Space commits",
    "输入全拼，空格上屏" to "Type full pinyin; Space commits",
    "正在启动普通话语音…" to "Starting Mandarin voice input…",
    "正在听普通话…" to "Listening for Mandarin…",
    "语音结果：请选择后上屏" to "Voice results: choose one to insert",
    "隐私或专用字段：英文模式" to "Private or special field: English mode",
    "自然码双拼" to "Natural Code double pinyin",
    "全拼" to "Full pinyin",
    "中·全拼" to "ZH · Full Pinyin",
    "中·自然码" to "ZH · Natural Code",
    "EN·隐私" to "EN · Private",
    "Backend 未启用，反馈尚未记录" to "Backend disabled; feedback was not recorded",
    "Backend 未启用，方向尚未记录" to "Backend disabled; guidance was not recorded",
    "Backend 未启用，结果尚未记录" to "Backend disabled; outcome was not recorded",
    "谏言编号无效" to "Invalid advice ID",
    "反馈未送达，请检查 Backend 连接" to "Feedback was not delivered. Check the Backend connection.",
    "反馈已送达，但本地状态更新失败" to "Feedback was delivered, but local status could not be updated",
    "方向尚未送达，请检查 Backend 连接" to "Guidance was not delivered. Check the Backend connection.",
    "结果未送达，请检查 Backend 连接" to "Outcome was not delivered. Check the Backend connection.",
    "结果已送达，但本地状态更新失败" to "Outcome was delivered, but local status could not be updated",
    "请先写下希望AI替身调整的方向" to "Describe how you want My AI Twin to adjust",
    "已记录采纳" to "Adoption recorded",
    "已稍后处理；AI替身会按新的时点再提醒" to "Marked for later; My AI Twin will remind you at the new time",
    "已阅；本条不再提醒" to "Acknowledged; this advice will not be repeated",
    "言尽于此，已停止此条谏言" to "Topic stopped; this advice will not be repeated",
    "已记录事实错误并撤回谏言" to "Fact error recorded and advice withdrawn",
    "已记录预测错误并撤回谏言" to "Prediction error recorded and advice withdrawn",
    "已记录与目标无关并撤回谏言" to "Irrelevance recorded and advice withdrawn",
    "已记录时机错误并撤回谏言" to "Timing error recorded and advice withdrawn",
    "已记住你的方向，后续建言会据此调整" to "Guidance saved; future advice will adjust accordingly",
    "已核验：结果达成" to "Reviewed: outcome achieved",
    "已核验：结果未达成" to "Reviewed: outcome not achieved",
    "请先登录" to "Please sign in first",
    "登录已失效，请重新登录" to "Your session expired. Please sign in again.",
    "服务器连接未启用或地址不安全" to "The server connection is disabled or insecure",
    "服务器地址已改变，请重新登录" to "The server address changed. Sign in again.",
    "暂时无法确认登录状态" to "Sign-in status is temporarily unavailable",
    "暂时无法连接服务器" to "The server is temporarily unreachable",
    "服务器返回的账号信息无效" to "The server returned invalid account information",
    "登录设备与服务器会话不一致" to "This device does not match the server session",
    "服务器没有返回有效登录凭据" to "The server did not return valid sign-in credentials",
    "登录成功，但无法确认账号身份" to "Sign-in succeeded, but account identity could not be verified",
    "无法连接AI替身服务器，请检查网络后重试" to "Could not reach the My AI Twin server. Check the network and try again.",
    "无法建立本机安全标识，请重试" to "Could not create a secure device identity. Try again.",
    "旧主人会话的服务器地址无效，请退出后重新连接" to "The legacy owner server address is invalid. Exit and reconnect.",
    "旧主人会话已经变化，请返回后重新确认" to "The legacy owner session changed. Go back and confirm again.",
    "旧主人会话已经变化；新会话已撤销，请重试" to "The legacy owner session changed. The new session was revoked; try again.",
    "账号已验证，但无法确认服务器身份" to "The account was verified, but server identity could not be confirmed",
    "这个账号不属于当前旧主人数据；新会话已撤销，本机旧数据未改动" to
        "This account does not own the legacy data. The new session was revoked and local legacy data was not changed.",
    "无法领取主人账号；旧会话和本机数据保持不变，请检查网络后重试" to
        "Could not claim the owner account. The legacy session and local data are unchanged; check the network and try again.",
    "用户名或密码不正确" to "Incorrect username or password",
    "用户名或密码格式不符合要求" to "Username or password format is invalid",
    "邀请码无效、已使用或注册尚未开放" to "Invitation code is invalid or used, or registration is not open",
    "用户名已使用、邀请码已使用或注册状态冲突" to "Username or invitation code is already used, or registration state conflicts",
    "尝试次数过多，请稍后再试" to "Too many attempts. Try again later.",
    "服务器暂时不可用，请稍后再试" to "The server is temporarily unavailable. Try again later.",
    "账号暂时无法登录" to "This account cannot sign in right now",
    "账号状态冲突" to "Account state conflict",
    "端口必须是数字" to "Port must be numeric",
    "AI替身连接已停用，请先在连接页启用" to "My AI Twin connection is disabled. Enable it in Connections.",
    "请先在AI替身连接页配置授权" to "Configure authorization in My AI Twin Connections first",
    "请先在AI替身连接页配置语音识别地址" to "Configure a speech-recognition URL in My AI Twin Connections first",
    "语音识别地址必须使用 HTTPS" to "The speech-recognition URL must use HTTPS",
    "可信局域网语音地址必须是私有地址" to "A trusted-LAN speech URL must use a private address",
    "当前设备未安装可用的端侧普通话模型" to "No compatible on-device Mandarin model is installed",
    "语音识别服务正忙，请稍后重试" to "Speech recognition is busy. Try again shortly.",
    "服务器语音识别尚未启用" to "Server-side speech recognition is not enabled",
    "语音识别授权无效，请检查连接" to "Speech-recognition authorization is invalid. Check Connections.",
    "无法连接语音识别服务，请检查网络" to "Could not reach the speech-recognition service. Check the network.",
    "语音识别已取消" to "Speech recognition canceled",
    "没有听清，请重说一次" to "No speech match. Please say it again.",
    "语音识别结果异常，请重试" to "The speech-recognition result was invalid. Try again.",
    "语音识别失败，请重试" to "Speech recognition failed. Try again.",
    "麦克风暂时不可用，请重试" to "The microphone is temporarily unavailable. Try again.",
    "请先授予AI替身麦克风权限" to "Grant My AI Twin microphone permission first",
    "语音识别网络不可用" to "The speech-recognition network is unavailable",
    "语音识别正忙，请稍后重试" to "Speech recognition is busy. Try again shortly.",
    "语音识别服务暂时不可用" to "The speech-recognition service is temporarily unavailable",
    "当前语音服务不支持普通话" to "The current speech service does not support Mandarin",
    "无法读取AI替身语音连接配置" to "Could not read My AI Twin speech connection settings",
    "语音服务没有返回结果，请重试" to "The speech service returned no result. Try again.",
    "当前设备没有可用的端侧普通话语音服务" to "No compatible on-device Mandarin speech service is available",
    "无法初始化端侧普通话识别，请重试" to "Could not initialize on-device Mandarin recognition. Try again.",
    "无法启动端侧普通话识别，请重试" to "Could not start on-device Mandarin recognition. Try again.",
    "语音服务启动超时，请重试" to "The speech service timed out while starting. Try again.",
    "单次语音最长 20 秒，请重试" to "Voice input is limited to 20 seconds. Try again.",
    "没有检测到普通话，请靠近麦克风重试" to "No Mandarin speech was detected. Move closer to the microphone and try again.",
    "无法启动麦克风，请检查是否被其他应用占用" to "Could not start the microphone. Check whether another app is using it.",
    "麦克风没有开始录音，请重试" to "The microphone did not start recording. Try again.",
    "麦克风录音中断，请重试" to "Microphone recording was interrupted. Try again.",
    "麦克风权限已被撤销" to "Microphone permission was revoked",
    "麦克风录音失败，请重试" to "Microphone recording failed. Try again.",
    "个人词频清除失败" to "Could not clear personal word frequencies",
    "已清除个人词频" to "Personal word frequencies cleared",
    "持续录音" to "Continuous recording",
    "输入法语音" to "Keyboard voice input",
    "端侧普通话" to "On-device Mandarin",
    "AI替身普通话" to "My AI Twin Mandarin",
    "AI替身简报" to "My AI Twin briefs",
    "AI替身进言" to "My AI Twin advice",
    "AI替身警告" to "My AI Twin alerts",
    "你已采纳：" to "You adopted: ",
    "现在核验：" to "Review now: ",
    "打开AI替身记录结果，后续建言会据此校准。" to "Open My AI Twin to record the outcome so future advice can be calibrated.",
)
