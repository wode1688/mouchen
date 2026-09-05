package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONArray
import org.json.JSONObject

/**
 * Conservative, on-device routing only. A match does not publish advice; it only lets a durable
 * event bypass the oldest-first bulk queue so the backend can make the final, goal-aware decision.
 */
internal object UrgencyClassifier {
    fun classify(event: LocalEventEntity): UrgencyCategory? {
        if (event.type !in SUPPORTED_TYPES) return null
        val text = eventText(event)
        if (text.isBlank()) return null

        if (event.type in HEALTH_SOURCE_TYPES &&
            hasActionableHealthMatch(text)
        ) {
            return UrgencyCategory.URGENT_HEALTH
        }
        if (hasActionableMatch(ACCOUNT_SECURITY, SECURITY_RESOLVED_OR_TEST, text)) {
            return UrgencyCategory.ACCOUNT_SECURITY
        }
        if (hasActionableMatch(DEADLINE, DEADLINE_RESOLVED, text)) {
            return UrgencyCategory.DEADLINE
        }
        if (isExplicitOwnerRequest(event)) return UrgencyCategory.OWNER_REQUEST
        return null
    }

    internal fun eventText(event: LocalEventEntity): String {
        val payload = runCatching { JSONObject(event.payloadJson) }.getOrNull() ?: return ""
        val fields = FIELDS_BY_TYPE[event.type].orEmpty()
        val parts = ArrayList<String>(fields.size)
        fields.forEach { field -> appendText(payload.opt(field), parts) }
        return parts.asSequence()
            .flatMap { it.lineSequence() }
            .map { it.replace(HORIZONTAL_WHITESPACE, " ").trim() }
            .filter(String::isNotEmpty)
            .joinToString("\n")
            .take(MAX_TEXT_CHARACTERS)
    }

    private fun isExplicitOwnerRequest(event: LocalEventEntity): Boolean {
        if (event.type != "ui.visible_text") return false
        val payload = runCatching { JSONObject(event.payloadJson) }.getOrNull() ?: return false
        return payload.optBoolean("analysis_requested", false) &&
            payload.optString("context") in EXPLICIT_OWNER_CONTEXTS
    }

    /** Negation is local to a clause; an old/resolved fact cannot suppress a new fact after a turn. */
    private fun hasActionableMatch(positive: Regex, suppression: Regex, text: String): Boolean {
        val fragments = actionClauseFragments(text)
        return fragments.indices.any { index ->
            val fragment = fragments[index].text
            if (!positive.containsMatchIn(fragment) || suppression.containsMatchIn(fragment)) {
                return@any false
            }
            // Natural language often puts a short resolution tail after a comma: "异常登录，已
            // 确认是我". Keep that tail attached logically, while never carrying it across a
            // contrasting/new-fact fragment such as "本次检测到异地登录".
            val next = fragments.getOrNull(index + 1)
            val resolvedByTail = next != null &&
                next.canResolvePrevious &&
                RESOLUTION_TAIL_LEAD.containsMatchIn(next.text) &&
                suppression.containsMatchIn(next.text)
            !resolvedByTail
        }
    }

    private fun hasActionableHealthMatch(text: String): Boolean {
        val fragments = actionClauseFragments(text)
        return fragments.indices.any { index ->
            val fragment = fragments[index].text
            if (!URGENT_HEALTH.containsMatchIn(fragment)) return@any false
            val mentioned = healthSignalKeys(fragment).ifEmpty { setOf(HEALTH_SIGNAL_GENERIC) }
            val suppressed = suppressedHealthSignalKeys(fragment)
            val actionable = mentioned - suppressed
            if (actionable.isEmpty()) return@any false

            val next = fragments.getOrNull(index + 1)
            val tailSuppressed = if (
                next != null &&
                next.canResolvePrevious &&
                RESOLUTION_TAIL_LEAD.containsMatchIn(next.text)
            ) {
                suppressedHealthSignalKeys(next.text)
            } else {
                emptySet()
            }
            !tailSuppressed.containsAll(actionable)
        }
    }

    internal fun actionFragments(text: String): List<String> =
        actionClauseFragments(text).map(ActionFragment::text)

    private fun actionClauseFragments(text: String): List<ActionFragment> {
        val output = mutableListOf<ActionFragment>()
        var start = 0
        var canResolvePrevious = false
        CLAUSE_BOUNDARY.findAll(text).forEach { boundary ->
            appendTurnFragments(
                text.substring(start, boundary.range.first),
                canResolvePrevious,
                output,
            )
            canResolvePrevious = boundary.value.all { it == ',' || it == '，' }
            start = boundary.range.last + 1
        }
        appendTurnFragments(text.substring(start), canResolvePrevious, output)
        return output
    }

    private fun appendTurnFragments(
        value: String,
        canResolvePrevious: Boolean,
        output: MutableList<ActionFragment>,
    ) {
        value.split(TURN_BOUNDARY)
            .map(String::trim)
            .filter(String::isNotEmpty)
            .forEachIndexed { index, part ->
                output += ActionFragment(
                    text = part,
                    canResolvePrevious = canResolvePrevious && index == 0,
                )
            }
    }

    private fun suppressedHealthSignalKeys(text: String): Set<String> =
        HEALTH_NEGATION.findAll(text)
            .flatMap { healthSignalKeys(it.value).asSequence() }
            .toSet()

    private fun healthSignalKeys(text: String): Set<String> = buildSet {
        HEALTH_SIGNAL_PATTERNS.forEach { (key, pattern) ->
            if (pattern.containsMatchIn(text)) add(key)
        }
    }

    private fun appendText(value: Any?, output: MutableList<String>) {
        when (value) {
            null, JSONObject.NULL -> Unit
            is String -> value.trim().takeIf(String::isNotEmpty)?.let(output::add)
            is JSONArray -> {
                for (index in 0 until value.length()) appendText(value.opt(index), output)
            }
            is JSONObject -> {
                val keys = value.keys()
                while (keys.hasNext()) appendText(value.opt(keys.next()), output)
            }
            else -> Unit
        }
    }

    private val SUPPORTED_TYPES = setOf(
        "notification.posted",
        "ui.visible_text",
        "ime.text_committed",
        "speech.transcript",
        "mail.received",
        "calendar.scheduled",
    )

    private val HEALTH_SOURCE_TYPES = setOf(
        "notification.posted",
        "ui.visible_text",
        "ime.text_committed",
        "speech.transcript",
    )

    private val FIELDS_BY_TYPE = mapOf(
        "notification.posted" to listOf(
            "title",
            "text",
            "big_text",
            "sub_text",
            "summary_text",
            "text_lines",
        ),
        "ui.visible_text" to listOf("visible_text"),
        "ime.text_committed" to listOf("text", "committed_text"),
        "speech.transcript" to listOf("transcript", "text", "segments"),
        "mail.received" to listOf("subject", "body"),
        "calendar.scheduled" to listOf("title", "description", "location", "organizer"),
    )

    private val EXPLICIT_OWNER_CONTEXTS = setOf(
        "owner_self_report",
        "shared_text",
        "shared_image",
        "conversation_import",
    )

    private val SEVERE_SYMPTOM =
        "(?:胸痛|胸口(?:剧痛|疼痛)|呼吸困难|喘不过气|无法呼吸|意识不清|失去意识|昏迷|晕厥|晕倒|严重出血|大量出血|chest pain|difficulty breathing|can't breathe|cannot breathe|unconscious|fainted|severe bleeding)"
    private val URGENT_HEALTH = Regex(
        "(?:救命|快叫救护车|(?:打|拨打|叫)120|需要急救)|" +
            "(?:我|本人|现在|突然|正在).{0,10}$SEVERE_SYMPTOM|" +
            "\\b(?:i|i'm|im|now|currently|suddenly)\\b.{0,10}$SEVERE_SYMPTOM|" +
            "(?:他|她|孩子|老人|患者|有人).{0,8}(?:突然)?$SEVERE_SYMPTOM|" +
            "$SEVERE_SYMPTOM.{0,8}(?:了|现在|突然|救命|怎么办|需要急救)",
        RegexOption.IGNORE_CASE,
    )
    private val HEALTH_NEGATION = Regex(
        "(?:没有|并无|未发现|未出现|否认|\\bno\\b).{0,8}$SEVERE_SYMPTOM|" +
            "(?:测试|演练|示例|科普).{0,16}$SEVERE_SYMPTOM|" +
            "(?:deny|denies|denied|without).{0,8}$SEVERE_SYMPTOM",
        RegexOption.IGNORE_CASE,
    )

    private val ACCOUNT_SECURITY = Regex(
        "(?:异地|可疑|异常|未经授权|未授权|非本人).{0,12}(?:登录|登入|访问|操作)|" +
            "(?:登录|登入|访问|操作).{0,12}(?:异地|可疑|异常|未经授权|未授权|非本人)|" +
            "(?:账号|帐号|账户).{0,10}(?:被盗|盗用|锁定|冻结)|" +
            "(?:不是本人|非本人).{0,8}(?:修改密码|更改密码|登录|操作)|" +
            "(?:unauthori[sz]ed|suspicious).{0,12}(?:sign-?in|login|access|activity)|" +
            "account.{0,8}(?:locked|suspended|compromised)",
        RegexOption.IGNORE_CASE,
    )
    private val SECURITY_RESOLVED_OR_TEST = Regex(
        "(?:误报|测试|演练|示例).{0,18}(?:登录|账号|帐号|账户)|" +
            "(?:没有|并无|未发现|未检测到|不是|并非).{0,12}(?:异地|可疑|异常|未经授权|未授权|非本人).{0,12}(?:登录|登入|访问|操作)|" +
            "(?:已核实|已确认).{0,10}(?:本人|是我)|" +
            "(?:风险|异常).{0,8}(?:已解除|已处理|已解决)|" +
            "\\b(?:no|not)\\b.{0,16}(?:unauthori[sz]ed|suspicious).{0,12}(?:sign-?in|login|access|activity)|" +
            "(?:confirmed|verified).{0,16}(?:as )?(?:me|mine|authorized)|" +
            "(?:login|sign-?in).{0,12}(?:was|is) mine|false positive|test alert",
        RegexOption.IGNORE_CASE,
    )

    private val DEADLINE = Regex(
        """(?:已逾期|已经逾期|已过期|已经过期|即将到期|最后期限|今日截止|今天截止|明日截止|明天截止)|截止.{0,8}(?:今天|今日|明天|明日|本周|[0-9一二三四五六七八九十]+小时)|\b(?:overdue|past due|deadline (?:today|tomorrow)|expires? (?:today|tomorrow|soon))\b""",
        RegexOption.IGNORE_CASE,
    )
    private val DEADLINE_RESOLVED = Regex(
        "(?:未逾期|尚未到期|无需).{0,12}(?:逾期|到期|截止)|" +
            "(?:没有|并无|未发现|不存在|不是|并非).{0,8}(?:今天|今日|明天|明日)截止(?:的)?(?:任务|事项|安排)?|" +
            "(?:已完成|已提交|已交付|已延期|延期至).{0,16}(?:截止|期限|deadline)?|" +
            "\\b(?:no|not)\\b.{0,12}\\bdeadline (?:today|tomorrow)\\b|" +
            "(?:completed|submitted|delivered|postponed|rescheduled).{0,16}(?:deadline)?",
        RegexOption.IGNORE_CASE,
    )

    private val HORIZONTAL_WHITESPACE = Regex("[\\t\\x0B\\f\\r ]+")
    private val CLAUSE_BOUNDARY = Regex("[\\n。！？!?；;，,]+")
    private val TURN_BOUNDARY = Regex(
        "(?=但(?:是)?|不过|然而|本次|这次|" +
            "(?i:\\b(?:but|however|yet|and now|this time|a new)\\b))|" +
            "(?i:\\band\\s+(?=(?:task|project|item)\\b))|" +
            "(?<=已完成|已提交|已交付)\\s+(?=(?:任务|项目|事项)?[^\\s，。]{1,12}(?:今天|今日|明天|明日)截止)",
    )
    private val RESOLUTION_TAIL_LEAD = Regex(
        "^(?:(?:但(?:是)?|不过|然而|(?i:but|however|yet))\\s*)?" +
            "(?:已|已经|确认|核实|没有|并无|未|无需|" +
            "(?i:(?:confirmed|verified|completed|submitted|delivered|resolved|" +
            "false positive|no longer|no|not|without)\\b))",
    )
    private val HEALTH_SIGNAL_PATTERNS = listOf(
        "chest" to Regex("(?:胸痛|胸口(?:剧痛|疼痛)|chest pain)", RegexOption.IGNORE_CASE),
        "breathing" to Regex(
            "(?:呼吸困难|喘不过气|无法呼吸|difficulty breathing|can't breathe|cannot breathe)",
            RegexOption.IGNORE_CASE,
        ),
        "consciousness" to Regex(
            "(?:意识不清|失去意识|昏迷|晕厥|晕倒|unconscious|fainted)",
            RegexOption.IGNORE_CASE,
        ),
        "bleeding" to Regex("(?:严重出血|大量出血|severe bleeding)", RegexOption.IGNORE_CASE),
    )
    private const val MAX_TEXT_CHARACTERS = 16_000
    private const val HEALTH_SIGNAL_GENERIC = "emergency"

    private data class ActionFragment(
        val text: String,
        val canResolvePrevious: Boolean,
    )
}

internal enum class UrgencyCategory(val wireValue: String) {
    URGENT_HEALTH("urgent_health"),
    ACCOUNT_SECURITY("account_security"),
    DEADLINE("deadline"),
    OWNER_REQUEST("owner_request"),
}
