from __future__ import annotations

import ctypes
import locale as system_locale
import re
import weakref
from typing import Any


SUPPORTED_LOCALES = ("zh-CN", "en-US")


def normalize_locale(value: object, *, fallback: str | None = None) -> str:
    normalized = str(value or "").strip().replace("_", "-").casefold()
    if normalized.startswith("zh"):
        return "zh-CN"
    if normalized.startswith("en"):
        return "en-US"
    if fallback in SUPPORTED_LOCALES:
        return str(fallback)
    return detect_system_locale()


def detect_system_locale() -> str:
    value = ""
    try:
        buffer = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            value = buffer.value
    except (AttributeError, OSError):
        pass
    if not value:
        try:
            value = system_locale.getlocale()[0] or ""
        except (TypeError, ValueError):
            value = ""
    return "zh-CN" if value.casefold().startswith("zh") else "en-US"


_locale = detect_system_locale()


def get_locale() -> str:
    return _locale


def set_locale(value: object) -> str:
    global _locale
    _locale = normalize_locale(value)
    return _locale


# This table contains interface chrome only. User text, captured text, goals and
# server-authored advice deliberately never enter this catalogue.
_ENGLISH: dict[str, str] = {
    "AI替身": "My AI Twin",
    "AI替身 · Windows 私测版": "My AI Twin · Windows Private Alpha",
    "AI替身 · 事业助手": "My AI Twin · Career Copilot",
    "事业助手 · Windows Private Alpha": "Career Copilot · Windows Private Alpha",
    "打开AI替身": "Open My AI Twin",
    "暂停 / 继续": "Pause / Resume",
    "退出": "Exit",
    "退出AI替身": "Exit My AI Twin",
    "AI替身已经在运行。": "My AI Twin is already running.",
    "领取AI替身主人账号": "Claim the My AI Twin owner account",
    "先领取主人账号": "Claim the owner account first",
    "领取主人账号": "Claim owner account",
    "本次暂时继续旧模式": "Continue legacy mode this time",
    "这台电脑仍在使用旧版单主人密钥。请为原有目标、记录和建言领取用户名与密码，以后即可在电脑和手机上登录同一账号。": "This computer still uses the legacy single-owner key. Claim a username and password for the existing goals, records and advice, then use the same account on computers and phones.",
    "领取成功会保留本机数据和已经授权的信息来源，不会把原有资料迁到其他账号。": "Claiming preserves local data and authorized sources. Existing information will not move to another account.",
    "在你作出选择前，AI替身不会观察、同步或推送。": "My AI Twin will not observe, sync or notify until you choose.",
    "临时继续只对本次启动有效；下次启动仍会询问，旧模式关闭后将无法继续使用。": "Continuing is valid for this launch only. You will be asked again next time, and legacy mode will eventually be retired.",
    "使用主人领取码为原有数据设置用户名和密码；如果已在其他设备领取，直接登录主人账号。": "Use the owner claim code to set a username and password for existing data. If already claimed on another device, sign in directly.",
    "同一账号可在电脑和手机上共享目标、建言与反馈。": "The same account shares goals, advice and feedback across computers and phones.",
    "普通邀请码不能领取原有数据；账号必须由服务器确认属于当前主人。": "A regular invite cannot claim existing data. The service must confirm this is the owner account.",
    "AI替身不会让您填写内部用户编号；账号归属由服务器登录会话确认。": "My AI Twin never asks for an internal user ID. Account ownership is confirmed by the signed-in session.",
    "领取失败：请确认主人领取码和用户名；普通邀请码不能领取原有数据": "Claim failed. Check the owner claim code and username; a regular invite cannot claim existing data.",
    "登录AI替身": "Sign in to My AI Twin",
    "登录": "Sign in",
    "已有主人账号，登录": "Sign in with an existing owner account",
    "领取并继续": "Claim and continue",
    "创建账号": "Create account",
    "服务地址": "Service address",
    "用户名": "Username",
    "密码": "Password",
    "注册时再次输入密码": "Re-enter password to register",
    "主人领取码（领取时必填）": "Owner claim code (required)",
    "邀请码（创建账号时填写）": "Invite code (for account creation)",
    "请输入用户名和密码": "Enter your username and password",
    "两次输入的密码不一致": "The passwords do not match",
    "密码至少需要 10 个字符": "Password must be at least 10 characters",
    "请输入主人领取码": "Enter the owner claim code",
    "正在创建账号…": "Creating account…",
    "正在登录…": "Signing in…",
    "用户名或密码不正确": "Incorrect username or password",
    "暂未开放自助注册，请联系管理员开通账号": "Self-service registration is unavailable. Contact an administrator.",
    "该用户名不可用，请更换后重试": "That username is unavailable. Choose another one.",
    "AI替身服务暂时不可用，请稍后重试": "My AI Twin is temporarily unavailable. Try again later.",
    "暂时无法连接AI替身服务，请检查服务地址和网络": "Cannot connect to My AI Twin. Check the service address and network.",
    "选择AI替身可以了解的信息": "Choose what My AI Twin may learn",
    "先决定AI替身可以了解什么": "Choose what My AI Twin may learn first",
    "新账号默认不采集、不上传。下面每项都可以单独开启，选择只对当前账号生效，以后可以在“信息来源”中随时关闭。": "New accounts collect and upload nothing by default. Enable each source separately for this account, and turn it off at any time under Sources.",
    "可见界面": "Visible interface",
    "浏览器活动": "Browser activity",
    "剪贴板文字": "Clipboard text",
    "授权文件内容": "Authorized file content",
    "主动云分析": "Proactive cloud analysis",
    "读取当前前台应用名称和无障碍接口中可见的文字；不读取密码框。": "Read the foreground app name and text visible through accessibility APIs; password fields are excluded.",
    "读取 Chrome / Edge 已访问页面的标题和网站域名，不保存完整网址。": "Read titles and domains from visited Chrome / Edge pages; full URLs are not stored.",
    "观察之后新复制的文字；密码、密钥和支付号码会在本机过滤。": "Observe newly copied text; passwords, keys and payment numbers are filtered locally.",
    "仅观察你之后在AI替身中主动选择的文件夹；当前尚未选择任何文件夹。": "Observe only folders you explicitly choose in My AI Twin; no folder is currently selected.",
    "允许已勾选来源触发 AI 分析。开启本项不会自动开启任何信息来源。": "Allow selected sources to trigger AI analysis. This does not enable any source by itself.",
    "勾选信息来源表示该来源的数据会加密同步到你的AI替身账号。云分析关闭时，不会把这些事件交给外部 AI 模型；“允许远程全文”仍需在高级设置中另行开启。": "Selected source data is encrypted and synced to your My AI Twin account. With cloud analysis off, events are not sent to external AI models. Remote full text remains a separate advanced setting.",
    "全部不开启，仅查看": "Keep everything off; view only",
    "确认并开始": "Confirm and start",
    "暂停观察": "Pause observation",
    "继续观察": "Resume observation",
    "后端检测中": "Checking service",
    "后端在线": "Service online",
    "后端离线": "Service offline",
    "总览": "Overview",
    "建言": "Advice",
    "目标": "Goals",
    "AI替身偏好": "My AI Twin preferences",
    "信息源": "Sources",
    "信息来源": "Sources",
    "活动": "Activity",
    "设置": "Settings",
    "近 24 小时信号": "Signals in last 24 hours",
    "待同步": "Pending sync",
    "累计建言": "Total advice",
    "当前问题": "Current problem",
    "关联目标": "Related goal",
    "提交分析": "Analyze",
    "最近建言": "Recent advice",
    "正在观察": "Observing",
    "守候启动中": "Starting observation",
    "级别": "Level",
    "领域": "Domain",
    "建议": "Advice",
    "状态": "Status",
    "查看建言": "View advice",
    "全部建言": "All advice",
    "详情": "Details",
    "建言详情": "Advice details",
    "回复与方向": "Response and direction",
    "请选择一条建言": "Select an advice item",
    "文字指导": "Written guidance",
    "告诉AI替身哪里需要调整、遗漏了什么，或你真正想要的方向": "Tell My AI Twin what to adjust, what it missed, or the direction you want",
    "采纳": "Adopt",
    "无关": "Irrelevant",
    "事实有误": "Fact error",
    "稍后处理": "Later",
    "知道了": "Got it",
    "指导": "Guidance",
    "发送指导": "Send guidance",
    "暂无建言": "No advice yet",
    "目标列表": "Goal list",
    "目标坐标": "Goal map",
    "刷新": "Refresh",
    "新增目标": "Add goal",
    "标题": "Title",
    "主公原话": "Your original words",
    "关键词": "Keywords",
    "应用进程": "App processes",
    "每周小时": "Hours per week",
    "清空": "Clear",
    "保存目标": "Save goal",
    "全局": "Global",
    "长期设置：告诉AI替身观察什么、多久开口。针对单条建言的反馈请继续在“建言”页使用文字指导。": "Long-term settings tell My AI Twin what to watch and how often to speak. For a specific item, continue using written guidance on the Advice page.",
    "指定目标": "Specific goal",
    "建议方向（AI替身重点观察和建议的角度）": "Advice direction (what My AI Twin should focus on)",
    "只用全局方向": "Use global direction only",
    "全局方向后追加（冲突时目标优先）": "Append to global direction (goal wins on conflict)",
    "只用目标方向": "Use goal direction only",
    "建议频率": "Advice frequency",
    "继承全局频率": "Inherit global frequency",
    "建议事件类型（AI替身据此主动出言；不影响采集）": "Advice event types (controls intervention, not collection)",
    "继承全局事件类型": "Inherit global event types",
    "保存": "Save",
    "恢复全局设置": "Restore global settings",
    "正在加载偏好…": "Loading preferences…",
    "（还没有目标，可先设置全局偏好）": "(No goals yet; set global preferences first)",
    "未保存的修改": "Unsaved changes",
    "当前范围有未保存的偏好修改，切换后将丢失。仍要切换吗？": "This scope has unsaved preference changes. Switching will discard them. Continue?",
    "删除该目标的覆盖设置并立即恢复全局偏好？": "Delete this goal override and restore global preferences now?",
    "正在保存偏好…": "Saving preferences…",
    "正在恢复全局设置…": "Restoring global settings…",
    "未知错误": "Unknown error",
    "采集开关": "Collection controls",
    "启用主动观察": "Enable proactive observation",
    "活动窗口与应用时长": "Active window and app usage",
    "当前应用可见文字": "Visible text in current app",
    "Chrome / Edge 浏览活动": "Chrome / Edge browsing activity",
    "剪贴板文本": "Clipboard text",
    "授权文件夹变化": "Authorized folder changes",
    "读取文本文件内容": "Read text file content",
    "授权文件夹": "Authorized folders",
    "添加文件夹": "Add folder",
    "移除": "Remove",
    "保存信息源": "Save sources",
    "时间": "Time",
    "来源": "Source",
    "类型": "Type",
    "同步": "Sync",
    "摘要": "Summary",
    "当前账号": "Current account",
    "采集间隔（秒）": "Collection interval (seconds)",
    "同步间隔（秒）": "Sync interval (seconds)",
    "主动复盘间隔（分钟）": "Proactive review interval (minutes)",
    "Windows 建言通知": "Windows advice notifications",
    "登录 Windows 后自动运行": "Run after signing in to Windows",
    "允许远程后端接收全文": "Allow remote service to receive full text",
    "界面与建言语言": "Interface and advice language",
    "界面立即切换，后续建言按此语言输出；该偏好同步到账号": "The interface switches immediately; future advice uses this language; this preference syncs to your account",
    "简体中文": "Simplified Chinese",
    "English": "English",
    "测试连接": "Test connection",
    "保存设置": "Save settings",
    "退出账号": "Sign out",
    "将撤销这台电脑的登录并清除本机缓存。是否继续？": "This signs out this computer and clears its local cache. Continue?",
    "请先退出账号，再更换服务地址。": "Sign out before changing the service address.",
    "旧版账号": "Legacy account",
    "设置已保存": "Settings saved",
    "正在保存语言偏好…": "Saving language preference…",
    "语言偏好已同步到账号": "Language preference synced to your account",
    "语言切换失败": "Language switch failed",
    "设置未保存": "Settings not saved",
    "退出账号": "Sign out",
    "账号切换已暂停": "Account switch paused",
    "账号领取已暂停": "Account claim paused",
    "连接设置错误": "Connection settings error",
    "正在连接": "Connecting",
    "无法提交": "Cannot submit",
    "问题已进入分析队列": "Problem added to the analysis queue",
    "选择授权文件夹": "Choose an authorized folder",
    "目标不完整": "Incomplete goal",
    "领域、标题和主公原话不能为空": "Domain, title and original words are required",
    "每周小时必须是数字": "Hours per week must be a number",
    "已同步": "Synced",
    "待处理": "Pending",
    "依据": "Basis",
    "第一步": "First step",
    "替代路径": "Alternative",
    "无": "None",
    "预测": "Prediction",
    "核验时间": "Verification time",
    "未选择建言": "No advice selected",
    "请先选择一条建言": "Select an advice item first",
    "建言已更新": "Advice updated",
    "这条建言已不在本地列表中": "This advice is no longer in the local list",
    "建言已处理": "Advice already handled",
    "仍可发送文字指导": "You can still send written guidance",
    "该建言已结束，仍可发送文字指导": "This advice is closed, but you can still send guidance",
    "回复为空": "Empty response",
    "请输入要告诉AI替身的内容": "Enter what you want to tell My AI Twin",
    "回复过长": "Response too long",
    "回复不能超过 2000 个字符": "Response cannot exceed 2,000 characters",
    "反馈": "Feedback",
    "尚无活动窗口": "No active window yet",
    "通知状态：暂无投递记录": "Notification status: no delivery record",
    "通知渠道不可用": "Notification channel unavailable",
    "Windows 未接受托盘通知": "Windows did not accept the tray notification",
    "应用内建言弹窗创建失败": "Could not create the in-app advice popup",
    "建言通知已关闭": "Advice notifications are disabled",
    "连接成功": "Connected",
    "已采纳，AI替身已记录": "Adopted; My AI Twin has recorded it",
    "已标记无关，后续会降低同类建言": "Marked irrelevant; similar advice will be reduced",
    "已标记事实有误，后续会提高证据要求": "Marked fact error; stronger evidence will be required",
    "已安排稍后提醒": "Reminder scheduled",
    "已知悉，本条不再提醒": "Acknowledged; this item will not be repeated",
    "指导已收到，后续建言会据此调整": "Guidance received; future advice will adapt",
    "反馈已记录": "Feedback recorded",
    "操作": "Operation",
    "连接失败": "Connection failed",
    "提交失败": "Submission failed",
    "反馈提交失败": "Feedback submission failed",
    "操作失败": "Operation failed",
    "已采纳": "Adopted",
    "已忽略": "Dismissed",
    "已反馈": "Feedback sent",
    "已撤回": "Withdrawn",
    "已核验": "Verified",
    "等待采集授权 · 当前未采集、未上传": "Waiting for collection consent · Nothing collected or uploaded",
    "信息来源已全部关闭 · 当前未采集、未上传": "All sources are off · Nothing collected or uploaded",
    "观察已暂停": "Observation paused",
    "后端离线 · 主动分析未运行": "Service offline · Proactive analysis is not running",
    "事件同步暂缓 · 云端存储空间已满，将自动重试": "Event sync paused · Cloud storage is full; retrying automatically",
    "主动守候中 · 分析状态暂不可用": "Standing by · Analysis status unavailable",
    "主动守候中": "Standing by",
    "主动守候中 · 最近已生成建言": "Standing by · Advice was generated recently",
    "主动守候中 · 最近一次未达到建言标准": "Standing by · Last analysis did not meet the advice threshold",
    "主动守候中 · 尚无分析任务": "Standing by · No analysis tasks yet",
    "OpenAI 凭据失效": "OpenAI credentials are invalid",
    "Codex 未登录": "Codex is not signed in",
    "Codex 模型不可用": "Codex model unavailable",
    "模型路由配置错误": "Model routing configuration error",
    "已达到最大重试次数": "Maximum retry count reached",
    "分析进程曾中断": "Analysis process was interrupted",
    "分析服务暂不可用": "Analysis service temporarily unavailable",
    "模型服务暂不可用": "Model service temporarily unavailable",
    "分析服务不可用": "Analysis service unavailable",
    "已保存": "Saved",
    "已恢复全局设置": "Global settings restored",
    "设置已在其他页面更新；输入已保留，再次保存将覆盖": "Settings changed elsewhere. Your input was kept; saving again will overwrite the newer version.",
    "Backend URL 必须使用 http:// 或 https://": "Backend URL must use http:// or https://",
    "Backend URL 不能包含账号信息、查询参数或片段": "Backend URL cannot contain credentials, a query or a fragment",
    "Backend URL 端口无效": "Backend URL port is invalid",
    "远程服务器必须使用 https://，避免账号密码和采集内容被窃听": "Remote services must use https:// to protect credentials and collected content",
    "登录模式无效": "Invalid sign-in mode",
    "旧版 User ID 不能为空": "Legacy User ID is required",
    "旧登录缺少服务器绑定，请重新登录": "The legacy session is not bound to a server. Sign in again.",
    "服务器未返回完整的登录会话": "The service returned an incomplete sign-in session",
    "必须先登录账号才能保存采集授权": "Sign in before saving collection consent",
    "问题不能为空": "Problem cannot be empty",
    "请先登录AI替身": "Sign in to My AI Twin first",
    "登录状态已失效，请重新登录": "Your session expired. Sign in again.",
    "服务器返回的账号会话不一致": "The service returned a mismatched account session",
    "该账号不属于这台电脑的原有数据，请使用主人账号或主人领取码": "This account does not own this computer's existing data. Use the owner account or owner claim code.",
    "Backend 返回了无法解析的数据": "The backend returned data that could not be parsed",
}


_CHINESE = {value: key for key, value in _ENGLISH.items()}

_PREFIXES: tuple[tuple[str, str], ...] = (
    ("依据：", "Basis: "),
    ("原始来源：", "Original source: "),
    ("证据：", "Evidence: "),
    ("建议：", "Advice: "),
    ("第一步：", "First step: "),
    ("替代路径：", "Alternative: "),
    ("预测：", "Prediction: "),
    ("核验时间：", "Verification time: "),
    ("采纳后的预期结果：", "Expected result if adopted: "),
    ("登录已失效，请重新登录：", "Session expired. Sign in again: "),
    ("本机登录已清除；服务器暂时未确认撤销：", "Local sign-in cleared; the server has not confirmed revocation: "),
    ("偏好加载失败:", "Failed to load preferences: "),
    ("保存失败:", "Save failed: "),
    ("语言切换失败：", "Language switch failed: "),
    ("通知失败：", "Notification failed: "),
    ("Windows 托盘通知异常：", "Windows tray notification error: "),
)


_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^正在分析 · 队列 (\d+) 项$"), "Analyzing · {0} queued", "正在分析 · 队列 {0} 项"),
    (re.compile(r"^分析排队中 · (\d+) 项$"), "Analysis queued · {0} items", "分析排队中 · {0} 项"),
    (re.compile(r"^分析排队中 · (\d+) 项 · 等待预算窗口$"), "Analysis queued · {0} items · Waiting for budget window", "分析排队中 · {0} 项 · 等待预算窗口"),
    (re.compile(r"^分析排队中 · (\d+) 项 · 其中 (\d+) 项等待预算$"), "Analysis queued · {0} items · {1} waiting for budget", "分析排队中 · {0} 项 · 其中 {1} 项等待预算"),
    (re.compile(r"^分析重试中 · (\d+) 项 · (.+)$"), "Retrying analysis · {0} items · {1}", "分析重试中 · {0} 项 · {1}"),
    (re.compile(r"^最近分析失败 · (.+)$"), "Recent analysis failed · {0}", "最近分析失败 · {0}"),
    (re.compile(r"^旧版账号 · (.+)$"), "Legacy account · {0}", "旧版账号 · {0}"),
    (re.compile(r"^已安排稍后处理至 (.+)$"), "Scheduled for later: {0}", "已安排稍后处理至 {0}"),
    (re.compile(r"^稍后至 (.+)$"), "Later: {0}", "稍后至 {0}"),
    (re.compile(r"^连接成功 · (.*)$"), "Connected · {0}", "连接成功 · {0}"),
    (re.compile(r"^连接失败 · (.*)$"), "Connection failed · {0}", "连接失败 · {0}"),
    (re.compile(r"^提交失败：(.*)$"), "Submission failed: {0}", "提交失败：{0}"),
    (re.compile(r"^正在记录(.+)\.\.\.$"), "Recording {0}...", "正在记录{0}..."),
)

_EN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^Analyzing · (\d+) queued$"), "正在分析 · 队列 {0} 项"),
    (re.compile(r"^Analysis queued · (\d+) items$"), "分析排队中 · {0} 项"),
    (re.compile(r"^Analysis queued · (\d+) items · Waiting for budget window$"), "分析排队中 · {0} 项 · 等待预算窗口"),
    (re.compile(r"^Analysis queued · (\d+) items · (\d+) waiting for budget$"), "分析排队中 · {0} 项 · 其中 {1} 项等待预算"),
    (re.compile(r"^Retrying analysis · (\d+) items · (.+)$"), "分析重试中 · {0} 项 · {1}"),
    (re.compile(r"^Recent analysis failed · (.+)$"), "最近分析失败 · {0}"),
    (re.compile(r"^Legacy account · (.+)$"), "旧版账号 · {0}"),
    (re.compile(r"^Scheduled for later: (.+)$"), "已安排稍后处理至 {0}"),
    (re.compile(r"^Later: (.+)$"), "稍后至 {0}"),
    (re.compile(r"^Connected · (.*)$"), "连接成功 · {0}"),
    (re.compile(r"^Connection failed · (.*)$"), "连接失败 · {0}"),
    (re.compile(r"^Submission failed: (.*)$"), "提交失败：{0}"),
)


def translate_text(value: Any, locale: str | None = None) -> Any:
    if not isinstance(value, str) or not value:
        return value
    target = normalize_locale(locale, fallback=_locale) if locale else _locale
    if target == "zh-CN":
        direct = _CHINESE.get(value)
        if direct is not None:
            return direct
        for pattern, chinese in _EN_PATTERNS:
            match = pattern.match(value)
            if match:
                return chinese.format(
                    *(translate_text(group, "zh-CN") for group in match.groups())
                )
        for chinese, english in _PREFIXES:
            if value.startswith(english):
                result = chinese + value[len(english):]
                return result.replace(" (input preserved)", "(输入已保留)")
        if "\nFirst step: " in value:
            return value.replace("\nFirst step: ", "\n第一步：", 1)
        for brand in ("My AI Twin L", "Mouchen L"):
            if value.startswith(brand):
                return "AI替身 L" + value[len(brand):]
        if value.startswith("Notification status: "):
            result = "通知状态：" + value[len("Notification status: "):]
            for english, chinese in (
                ("no delivery record", "暂无投递记录"),
                ("remind later", "稍后提醒"),
                ("acknowledged", "已确认"),
                ("in-app advice shown; awaiting acknowledgement", "应用内建言已显示，等待确认"),
                ("system tray received it; advice remains available", "系统托盘已接收；建言列表可随时查看"),
                ("some channels failed: ", "部分渠道失败："),
                ("adopted", "已采纳"),
                ("dismissed", "已忽略"),
                ("feedback sent", "已反馈"),
                ("withdrawn", "已撤回"),
                ("verified", "已核验"),
                ("could not create the in-app advice popup", "应用内建言弹窗创建失败"),
                ("advice notifications are disabled", "建言通知已关闭"),
                ("Windows did not accept the tray notification", "Windows 未接受托盘通知"),
                ("advice ", "建言"),
            ):
                result = result.replace(english, chinese)
            return result
        if value.startswith("Notification failed: "):
            return (
                "通知失败："
                + value[len("Notification failed: "):].replace("; advice was saved to the list", "；建言已保存在列表")
            )
        return value
    direct = _ENGLISH.get(value)
    if direct is not None:
        return direct
    for pattern, english, _chinese in _PATTERNS:
        match = pattern.match(value)
        if match:
            return english.format(
                *(translate_text(group, "en-US") for group in match.groups())
            )
    if value.startswith("AI替身 L"):
        return "My AI Twin L" + value[len("AI替身 L"):]
    if value.startswith("Mouchen L"):
        return "My AI Twin L" + value[len("Mouchen L"):]
    if value.startswith("通知状态："):
        result = "Notification status: " + value[len("通知状态："):]
        for chinese, english in (
            ("暂无投递记录", "no delivery record"),
            ("稍后提醒", "remind later"),
            ("已确认", "acknowledged"),
            ("应用内建言已显示，等待确认", "in-app advice shown; awaiting acknowledgement"),
            ("系统托盘已接收；建言列表可随时查看", "system tray received it; advice remains available"),
            ("部分渠道失败：", "some channels failed: "),
            ("已采纳", "adopted"),
            ("已忽略", "dismissed"),
            ("已反馈", "feedback sent"),
            ("已撤回", "withdrawn"),
            ("已核验", "verified"),
            ("应用内建言弹窗创建失败", "could not create the in-app advice popup"),
            ("建言通知已关闭", "advice notifications are disabled"),
            ("Windows 未接受托盘通知", "Windows did not accept the tray notification"),
            ("建言", "advice "),
        ):
            result = result.replace(chinese, english)
        return result
    if value.startswith("通知失败："):
        body = value[len("通知失败："):].replace(
            "；建言已保存在列表", "; advice was saved to the list"
        )
        error, separator, remainder = body.partition("; advice was saved to the list")
        translated_error = translate_text(error, "en-US")
        return "Notification failed: " + translated_error + (
            separator + remainder if separator else ""
        )
    if value.endswith("；仍可发送文字指导"):
        prefix = value[: -len("；仍可发送文字指导")]
        return f"{translate_text(prefix, 'en-US')}; you can still send written guidance"
    for chinese, english in _PREFIXES:
        if value.startswith(chinese):
            result = english + value[len(chinese):]
            return result.replace("(输入已保留)", " (input preserved)")
    if "\n第一步：" in value:
        return value.replace("\n第一步：", "\nFirst step: ", 1)
    return value


def tr(value: str, locale: str | None = None) -> str:
    return str(translate_text(value, locale))


_hooks_installed = False
_ui_variables: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()
_ui_variable_sources: dict[int, str] = {}
_original_variable_set: Any = None


def set_ui_variable(variable: Any, value: str) -> None:
    """Set and track an explicit UI-copy variable for future retranslation.

    Ordinary Tk variables are intentionally never translated.  Entry fields,
    goal text and any other user-controlled value therefore keep their exact
    bytes even when they happen to equal an interface catalogue entry.
    """
    translated = translate_text(value)
    if _original_variable_set is not None and hasattr(variable, "_tk"):
        _original_variable_set(variable, translated)
    else:
        variable.set(translated)
    identifier = id(variable)
    _ui_variables[identifier] = variable
    _ui_variable_sources[identifier] = value


def set_raw_widget_text(widget: Any, value: str) -> None:
    """Set server/user-authored text without passing it through UI translation."""

    setattr(widget, "_mouchen_raw_text", True)
    widget.tk.call(widget._w, "configure", "-text", value)


def install_tk_localization() -> None:
    """Translate Tk chrome at construction/configuration time.

    The hook only changes exact catalogue entries (plus a small set of status
    templates), so captured content, goal text and server-authored advice pass
    through untouched.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    import tkinter as tk
    from tkinter import filedialog, messagebox

    original_variable_set = tk.Variable.set
    original_configure = tk.Misc._configure
    original_title = tk.Wm.title

    def variable_set(self: Any, value: Any) -> None:
        # A normal set is content, not interface copy.  Explicit UI status
        # variables must opt back in through set_ui_variable().
        identifier = id(self)
        _ui_variables.pop(identifier, None)
        _ui_variable_sources.pop(identifier, None)
        original_variable_set(self, value)

    def configure(self: Any, cmd: Any, cnf: Any, kw: Any) -> Any:
        raw_text = bool(getattr(self, "_mouchen_raw_text", False))
        if isinstance(cnf, dict):
            cnf = dict(cnf)
            if "text" in cnf and not raw_text:
                cnf["text"] = translate_text(cnf["text"])
            if "values" in cnf and isinstance(cnf["values"], (list, tuple)):
                cnf["values"] = tuple(translate_text(item) for item in cnf["values"])
        if isinstance(kw, dict):
            kw = dict(kw)
            if "text" in kw and not raw_text:
                kw["text"] = translate_text(kw["text"])
            if "values" in kw and isinstance(kw["values"], (list, tuple)):
                kw["values"] = tuple(translate_text(item) for item in kw["values"])
        return original_configure(self, cmd, cnf, kw)

    def title(self: Any, string: str | None = None) -> Any:
        return original_title(self, translate_text(string) if string is not None else None)

    tk.Variable.set = variable_set
    tk.Misc._configure = configure
    tk.Wm.title = title

    for name in ("showinfo", "showwarning", "showerror", "askquestion", "askokcancel", "askretrycancel", "askyesno", "askyesnocancel"):
        original = getattr(messagebox, name)

        def localized_messagebox(title: str | None = None, message: str | None = None, _original: Any = original, **options: Any) -> Any:
            return _original(translate_text(title), translate_text(message), **options)

        setattr(messagebox, name, localized_messagebox)

    original_askdirectory = filedialog.askdirectory

    def askdirectory(**options: Any) -> str:
        if "title" in options:
            options["title"] = translate_text(options["title"])
        return original_askdirectory(**options)

    filedialog.askdirectory = askdirectory
    global _original_variable_set
    _original_variable_set = original_variable_set
    _hooks_installed = True


def retranslate(root: Any) -> None:
    """Apply the locale to widgets and explicitly registered UI variables."""
    try:
        title = root.title()
        root.title(translate_text(title))
    except Exception:
        pass
    for identifier, variable in list(_ui_variables.items()):
        try:
            source = _ui_variable_sources[identifier]
            translated = translate_text(source)
            if translated != variable.get():
                if _original_variable_set is not None:
                    _original_variable_set(variable, translated)
                else:
                    variable.set(translated)
        except Exception:
            _ui_variable_sources.pop(identifier, None)
            continue

    def visit(widget: Any) -> None:
        if not getattr(widget, "_mouchen_raw_text", False):
            try:
                current = widget.cget("text")
                translated = translate_text(current)
                if translated != current:
                    widget.configure(text=translated)
            except Exception:
                pass
        try:
            values = tuple(widget.cget("values"))
            translated_values = tuple(translate_text(item) for item in values)
            if translated_values != values:
                widget.configure(values=translated_values)
        except Exception:
            pass
        try:
            for column in widget.cget("columns"):
                current = widget.heading(column, "text")
                translated = translate_text(current)
                if translated != current:
                    widget.heading(column, text=translated)
        except Exception:
            pass
        try:
            for tab_id in widget.tabs():
                current = widget.tab(tab_id, "text")
                translated = translate_text(current)
                if translated != current:
                    widget.tab(tab_id, text=translated)
        except Exception:
            pass
        try:
            children = widget.winfo_children()
        except Exception:
            children = ()
        for child in children:
            visit(child)

    visit(root)
