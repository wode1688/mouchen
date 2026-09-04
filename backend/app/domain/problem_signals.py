from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..localization import is_english, localized_observation
from .models import AdviceCandidate, AdviceLevel, Event, Evidence, Goal, Prediction, utc_now


SUPPORTED_EVENT_TYPES = {
    "notification.posted",
    "ui.visible_text",
    "ime.text_committed",
    "speech.transcript",
    "mail.received",
    "calendar.scheduled",
    "app.foreground_session",
    "message.sms",
    "shared.text",
    "thought.note",
}

_NEGATED = re.compile(
    r"(?:没有|未发现|无)(?:任何)?(?:错误|异常|风险|问题)|"
    r"(?:测试|检查|验证).{0,8}(?:通过|成功)|"
    r"\b(?:resolved|fixed|no (?:error|issue|problem))\b",
    re.IGNORECASE,
)

# These surfaces frequently contain somebody else's words or hypothetical
# material. They remain useful for semantic goal/context review, but a keyword
# inside them must never become a deterministic claim that the owner has the
# described problem.
_CONTEXT_ONLY_CONTENT_KINDS = frozenset(
    {"search", "web_page", "video", "document", "audio"}
)
_DIRECT_CONTENT_SPEAKERS = frozenset({"user", "system"})
_NON_DIRECT_EVIDENCE_STRENGTHS = frozenset({"contextual", "inferred"})

# Legacy observations created before content-attribution metadata existed can
# still be present in a user's durable queue.  A code editor or document view
# is context, not proof that the described exception/deadline happened to the
# owner.  These hints deliberately fail closed into semantic review instead of
# turning source text into a deterministic runtime claim.
_CODE_OR_DOCUMENT_SURFACE = re.compile(
    r"(?:\b(?:code(?:\.exe)?|visual studio(?: code)?|pycharm|intellij|"
    r"android studio|notepad\+\+|sublime text|vim|emacs|jupyter)\b|"
    r"\.(?:py|js|ts|tsx|jsx|java|kt|swift|go|rs|cs|cpp|c|h|md|rst|txt|docx?|pdf)\b)",
    re.IGNORECASE,
)
_CODE_TEXT = re.compile(
    r"```|(?:^|\s)(?:def|class|function|import|from|const|let|var|try|except|"
    r"raise|throw)\s+[A-Za-z_$]|(?:^|\s)(?:if|for|while)\s*\([^\n]{0,120}\)\s*\{",
    re.IGNORECASE,
)
_NEAR_TERM_DEADLINE = re.compile(
    r"逾期|已过期|即将到期|最后期限|"
    r"截止.{0,8}(?:今天|今日|明天|本周|小时)|"
    r"\b(?:overdue|past\s+due|due\s+(?:today|tomorrow|soon)|"
    r"(?:deadline|due\s+date)(?:\s+(?:is|was))?\s*(?:today|tomorrow|soon|"
    r"approaching|overdue|past\s+due)|expires?\s+(?:today|tomorrow|soon))\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")

_META_GOAL = re.compile(
    r"(?:谋臣|mouchen).{0,36}(?:主动|proactive|发现问题|建言|建议|提醒|介入|了解我)|"
    r"(?:主动|proactive|发现问题|建言|建议|提醒|介入|了解我).{0,36}(?:谋臣|mouchen)",
    re.IGNORECASE,
)
_MOUCHEN_PRODUCT_CONTEXT = re.compile(
    r"\b(?:mouchen|com\.mouchen)\b|谋臣(?:应用|app|客户端|后端|模型|建言|提醒|介入|功能|系统)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProblemRule:
    category: str
    pattern: re.Pattern[str]
    level: AdviceLevel
    urgency: float
    impact: float
    horizon_hours: int
    action: str
    first_step: str
    alternative: str
    prediction: str
    adopted_expected_result: str
    adopted_confidence: float


PROBLEM_RULES = (
    ProblemRule(
        category="account_security",
        pattern=re.compile(
            r"(?:异地|可疑|异常|未经授权).{0,12}(?:登录|访问|操作)|"
            r"(?:账户|账号).{0,8}(?:被锁|锁定|冻结|盗用)|"
            r"\b(?:unauthori[sz]ed|suspicious (?:sign-?in|login)|account (?:locked|suspended))\b",
            re.IGNORECASE,
        ),
        level=AdviceLevel.L3,
        urgency=0.97,
        impact=0.95,
        horizon_hours=2,
        action="立即核验安全提醒是否由本人触发；若不是，冻结相关操作并修改凭据",
        first_step="不要点击消息内链接，从对应 App 或官网独立进入安全中心查看登录记录",
        alternative="若确认由本人触发，标记为已核验并保留该记录",
        prediction="若提醒并非本人操作且不处理，账户风险可能在两小时内继续扩大",
        adopted_expected_result="两小时内已从官方安全中心确认登录归属；若非本人操作，已完成冻结相关操作和凭据修改",
        adopted_confidence=0.90,
    ),
    ProblemRule(
        category="payment_failure",
        pattern=re.compile(
            r"(?:支付|付款|扣款|充值|提现|转账).{0,12}(?:失败|拒绝|异常|未成功)|"
            r"(?:余额|额度).{0,8}不足|欠费|逾期账单|"
            r"\b(?:payment (?:failed|declined)|insufficient funds|past due invoice)\b",
            re.IGNORECASE,
        ),
        level=AdviceLevel.L2,
        urgency=0.86,
        impact=0.82,
        horizon_hours=12,
        action="先确认失败金额、对象和截止时间，再选择补款、换通道或联系对方",
        first_step="从官方 App 打开该笔记录，核对状态、失败原因和是否已实际扣款",
        alternative="若状态无法确认，先暂停重复支付并联系官方客服核账",
        prediction="若不核对，可能出现逾期、重复支付或服务中断",
        adopted_expected_result="十二小时内已确认该笔款项的真实状态，并取得补款、换通道或官方核账中的一个明确结果",
        adopted_confidence=0.84,
    ),
    ProblemRule(
        category="deadline_risk",
        pattern=re.compile(
            r"逾期|已过期|即将到期|最后期限|"
            r"截止.{0,8}(?:今天|明天|本周|小时)|"
            r"\b(?:overdue|past due|due (?:today|tomorrow|soon)|"
            r"(?:deadline|due date)(?: (?:is|was))? (?:today|tomorrow|soon|"
            r"approaching|overdue|past due)|expires? (?:today|tomorrow|soon))\b",
            re.IGNORECASE,
        ),
        level=AdviceLevel.L2,
        urgency=0.84,
        impact=0.78,
        horizon_hours=24,
        action="立即确认截止时间和最小可交付结果，先保住关键节点",
        first_step="打开原始通知或邮件，写下准确截止时间、责任人和最小交付物",
        alternative="若无法按时完成，立即提出缩小范围或明确改期",
        prediction="若今天不处理，该事项更可能逾期或被动失约",
        adopted_expected_result="二十四小时内已确认准确截止时间和最小交付物，并完成交付或取得明确改期确认",
        adopted_confidence=0.82,
    ),
    ProblemRule(
        category="service_disruption",
        pattern=re.compile(
            r"(?:航班|列车|订单|预约|会议).{0,12}(?:取消|延误|改期)|"
            r"(?:快递|物流|服务).{0,12}(?:异常|中断|延误)|"
            r"\b(?:cancelled|canceled|delayed|service interruption)\b",
            re.IGNORECASE,
        ),
        level=AdviceLevel.L2,
        urgency=0.82,
        impact=0.76,
        horizon_hours=12,
        action="确认变化是否影响后续安排，并立即建立一个可执行的替代路径",
        first_step="从官方入口核对最新状态、影响时间和可改签或替换选项",
        alternative="若暂时没有替代项，先通知受影响的人并设置下一次核验时间",
        prediction="若不调整，后续安排可能在十二小时内发生连锁延误",
        adopted_expected_result="十二小时内已从官方入口确认最新状态，并确定替代安排或通知所有受影响的人及下次核验时间",
        adopted_confidence=0.80,
    ),
    ProblemRule(
        category="operation_failure",
        pattern=re.compile(
            r"(?:操作|任务|构建|部署|连接|同步|登录|上传|下载|提交|安装).{0,14}(?:失败|异常|错误|超时|中断)|"
            r"(?:无法|不能).{0,14}(?:打开|连接|登录|提交|运行|完成|安装)|"
            r"\b(?:failed|error|exception|timeout|unavailable)\b",
            re.IGNORECASE,
        ),
        level=AdviceLevel.L2,
        urgency=0.72,
        impact=0.72,
        horizon_hours=24,
        action="保留完整错误证据，先恢复最小可用路径，再定位最近一次变化",
        first_step="打开失败详情，记录错误码、发生时间和最后一次成功操作",
        alternative="若当前事项有截止时间，先切换到人工或备用通道",
        prediction="若不保留现场并继续重复尝试，定位成本会继续增加",
        adopted_expected_result="二十四小时内已记录错误码和最后成功点，并恢复最小可用路径或确认备用通道有效",
        adopted_confidence=0.78,
    ),
    ProblemRule(
        category="urgent_health",
        pattern=re.compile(r"胸痛|呼吸困难|意识不清|晕厥|严重出血", re.IGNORECASE),
        level=AdviceLevel.L3,
        urgency=1.0,
        impact=1.0,
        horizon_hours=1,
        action="立即联系当地急救服务或可信任的人，不等待应用内判断",
        first_step="停止当前活动，拨打当地急救电话并说明症状与位置",
        alternative="若无法自行呼叫，让身边的人代为联系并保持有人陪同",
        prediction="延迟专业处置可能在一小时内扩大健康风险",
        adopted_expected_result="一小时内已联系当地急救服务或可信任的人，并由专业人员或现场陪同者接手处置",
        adopted_confidence=0.95,
    ),
)


_ENGLISH_RULE_TEXT: dict[str, dict[str, str]] = {
    "account_security": {
        "action": "Verify whether you triggered the security alert; if not, freeze the affected activity and change the credentials immediately.",
        "first_step": "Do not use links in the alert. Open the official app or website independently and review the security-center login history.",
        "alternative": "If the activity was yours, mark it verified and keep the record.",
        "prediction": "If the activity was unauthorized and remains untreated, the account exposure may grow within two hours.",
        "adopted_expected_result": "Within two hours, the login source is verified through the official security center; if unauthorized, affected activity is frozen and credentials are changed.",
    },
    "payment_failure": {
        "action": "Confirm the failed amount, counterparty, and deadline, then choose to fund the account, switch channels, or contact the counterparty.",
        "first_step": "Open the transaction in the official app and verify its status, failure reason, and whether funds were actually charged.",
        "alternative": "If the status is unclear, pause repeat payments and ask official support to reconcile the transaction.",
        "prediction": "Without verification, this may cause a missed deadline, duplicate payment, or service interruption.",
        "adopted_expected_result": "Within twelve hours, the true payment status is confirmed and there is a clear result from funding, switching channels, or official reconciliation.",
    },
    "deadline_risk": {
        "action": "Confirm the exact deadline and minimum acceptable deliverable now, and protect the critical milestone first.",
        "first_step": "Open the original notice or email and write down the exact deadline, owner, and minimum deliverable.",
        "alternative": "If on-time completion is not feasible, propose a smaller scope or obtain an explicit extension immediately.",
        "prediction": "If this is not handled today, it is more likely to become overdue or an unplanned broken commitment.",
        "adopted_expected_result": "Within twenty-four hours, the exact deadline and minimum deliverable are confirmed, and the item is delivered or an explicit extension is secured.",
    },
    "service_disruption": {
        "action": "Confirm how the change affects downstream plans and establish one executable alternative now.",
        "first_step": "Use the official source to verify the latest status, affected period, and rebooking or replacement options.",
        "alternative": "If no alternative is available yet, notify affected people and set the next verification time.",
        "prediction": "Without an adjustment, downstream plans may suffer cascading delays within twelve hours.",
        "adopted_expected_result": "Within twelve hours, the latest official status is confirmed and either an alternative is arranged or every affected person and the next review time are recorded.",
    },
    "operation_failure": {
        "action": "Preserve complete error evidence, restore the smallest working path, then identify the most recent change.",
        "first_step": "Open the failure details and record the error code, occurrence time, and last successful operation.",
        "alternative": "If a deadline is involved, switch to a manual or backup channel first.",
        "prediction": "Repeating attempts without preserving the failure state will continue to increase diagnosis cost.",
        "adopted_expected_result": "Within twenty-four hours, the error code and last successful point are recorded, and the minimum working path is restored or a backup channel is verified.",
    },
    "urgent_health": {
        "action": "Contact local emergency services or a trusted person immediately; do not wait for an in-app assessment.",
        "first_step": "Stop the current activity, call the local emergency number, and state the symptoms and location.",
        "alternative": "If you cannot call, ask someone nearby to contact emergency services and remain with you.",
        "prediction": "Delaying professional care may increase the health risk within one hour.",
        "adopted_expected_result": "Within one hour, local emergency services or a trusted person has been contacted and a professional or on-site companion has taken over.",
    },
}


def _rule_text(rule: ProblemRule, field: str, locale: str) -> str:
    if is_english(locale):
        translated = _ENGLISH_RULE_TEXT.get(rule.category, {}).get(field)
        if translated:
            return translated
    return str(getattr(rule, field))


_STRUCTURED_SUBJECT_ID_FIELDS = (
    "entity_id",
    "entity_key",
    "object_id",
    "object_key",
    "resource_id",
    "resource_key",
    "project_id",
    "task_id",
    "order_id",
    "invoice_id",
    "booking_id",
    "ticket_id",
    "case_id",
    "transaction_id",
    "document_id",
    "conversation_id",
    "thread_id",
)
_STRUCTURED_SUBJECT_NAME_FIELDS = (
    "entity_name",
    "object_name",
    "project_name",
    "project",
    "task_name",
    "order_name",
    "invoice_name",
    "booking_name",
    "ticket_title",
    "resource",
    "repository",
    "repo",
    "document",
    "file_path",
    "conversation",
    "thread",
    "merchant",
    "counterparty",
    "sender",
)
_VOLATILE_ENTITY_TYPES = frozenset(
    {
        "amount",
        "balance",
        "count",
        "currency",
        "date",
        "datetime",
        "deadline",
        "duration",
        "money",
        "number",
        "percentage",
        "quantity",
        "status",
        "time",
        "urgency",
    }
)
_VOLATILE_SUBJECT_WORDS = re.compile(
    r"\b(?:today|tomorrow|yesterday|tonight|now|currently|current|latest|new|"
    r"still|again|urgent|urgently|immediately|asap|status|code|amount|balance|"
    r"failed|failure|declined|rejected|error|exception|timeout|unavailable|"
    r"blocked|overdue|past\s+due|cancelled|canceled|delayed|locked|suspended|"
    r"unauthori[sz]ed|suspicious|this\s+week|this\s+month)\b|"
    r"今天|今日|明天|昨天|今晚|现在|当前|目前|最新|仍然|再次|紧急|立即|马上|尽快|"
    r"本周|这周|本月|这个月|状态|代码|金额|余额|失败|拒绝|异常|错误|超时|中断|"
    r"不可用|受阻|逾期|到期|取消|延误|锁定|冻结|未授权|可疑",
    re.IGNORECASE,
)
_GENERIC_CATEGORY_WORDS = {
    "account_security": re.compile(r"\bsecurity\s+alert\b|安全提醒", re.IGNORECASE),
    "payment_failure": re.compile(r"\bpayment\b|支付", re.IGNORECASE),
    "deadline_risk": re.compile(r"\bdeadline\b|截止", re.IGNORECASE),
    "service_disruption": re.compile(r"\bservice\b|服务", re.IGNORECASE),
    "operation_failure": re.compile(r"\boperation\b|操作", re.IGNORECASE),
    "urgent_health": re.compile(r"\bemergency\b|急症", re.IGNORECASE),
}
_STABLE_LABELED_IDENTIFIER = re.compile(
    r"(?P<label>"
    r"\b(?:order|invoice|project|ticket|case|booking)\s*"
    r"(?:(?:id|no\.?|number)\s*)?#?"
    r"|\bcard\s*(?:ending(?:\s+in)?|last\s*(?:four|4)?(?:\s+digits?)?|tail)"
    r"|(?:订单|发票|项目|工单|预订)(?:号|编号)"
    r"|(?:卡尾号|卡后四位|卡末四位)"
    r")\s*[:：#-]?\s*"
    r"(?P<value>[a-z0-9\u3400-\u9fff][a-z0-9\u3400-\u9fff_-]{1,39})",
    re.IGNORECASE,
)


def event_text(event: Event, limit: int = 12_000) -> str:
    if event.type not in SUPPORTED_EVENT_TYPES:
        return ""
    facts = event.facts
    fields: list[Any] = []
    if event.type == "notification.posted":
        fields.extend(
            (
                facts.get("title"),
                facts.get("text"),
                facts.get("big_text"),
                facts.get("sub_text"),
                facts.get("summary_text"),
                facts.get("text_lines"),
            )
        )
    elif event.type == "ui.visible_text":
        fields.append(facts.get("visible_text"))
    elif event.type == "ime.text_committed":
        fields.extend((facts.get("text"), facts.get("committed_text")))
    elif event.type == "speech.transcript":
        fields.extend((facts.get("transcript"), facts.get("text"), facts.get("segments")))
    elif event.type == "mail.received":
        fields.extend((facts.get("subject"), facts.get("body")))
    elif event.type == "calendar.scheduled":
        fields.extend(
            (
                facts.get("title"),
                facts.get("description"),
                facts.get("location"),
                facts.get("organizer"),
            )
        )
    elif event.type == "app.foreground_session":
        fields.extend((facts.get("app_label"), facts.get("package")))
    elif event.type == "message.sms":
        fields.extend((facts.get("body"), facts.get("text"), facts.get("message")))
    elif event.type == "shared.text":
        fields.extend(
            (
                facts.get("title"),
                facts.get("text"),
                facts.get("shared_text"),
                facts.get("caption"),
            )
        )
    elif event.type == "thought.note":
        fields.extend(
            (
                facts.get("title"),
                facts.get("text"),
                facts.get("note"),
                facts.get("transcript"),
            )
        )

    parts = list(_flatten_text(fields))
    normalized = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return normalized[:limit]


def choose_goal(event: Event, goals: list[Goal]) -> tuple[Goal | None, float]:
    if not goals:
        return None, 0.0
    explicit_domain = str(event.facts.get("domain", "")).strip().casefold()
    if explicit_domain:
        domain_goals = [goal for goal in goals if goal.domain.casefold() == explicit_domain]
        if not domain_goals:
            return None, 0.0
        if len(domain_goals) == 1:
            return domain_goals[0], 1.0
        text = event_text(event).casefold()
        package_name = str(event.facts.get("package", "")).casefold()
        chosen = max(
            domain_goals,
            key=lambda goal: (
                _goal_score(goal, text, package_name),
                not _is_meta_goal(goal),
            ),
        )
        return chosen, 1.0

    text = event_text(event).casefold()
    package_name = str(event.facts.get("package", "")).casefold()
    scored = [(goal, _goal_score(goal, text, package_name)) for goal in goals]
    best_goal, best_score = max(
        scored,
        key=lambda item: (item[1], not _is_meta_goal(item[0])),
    )
    if best_score >= 3.0:
        return best_goal, min(1.0, 0.72 + best_score / 20)
    if len(goals) == 1:
        return goals[0], 0.82
    for goal in sorted(goals, key=_is_meta_goal):
        if goal.domain.casefold() in {"general", "生活总控", "总控", "全局"}:
            return goal, 0.80
    return None, 0.0


def detect_problem(
    event: Event,
    goal: Goal,
    relevance: float,
    locale: str = "zh-CN",
) -> AdviceCandidate | None:
    if not deterministic_intervention_allowed(event):
        return None
    text = event_text(event)
    if not text or _NEGATED.search(text):
        return None
    rule = next((candidate for candidate in PROBLEM_RULES if candidate.pattern.search(text)), None)
    if rule is None:
        return None
    if not _rule_has_direct_semantic_evidence(event, text, rule.category):
        return None
    if rule.category == "urgent_health" and goal.domain.casefold() not in {"健康", "health", "医疗"}:
        return None

    summary = _summary(event, text)
    observed = localized_observation(event.source, summary, locale)
    source_key = str(event.facts.get("package") or event.facts.get("account") or event.source)
    digest = hashlib.sha256(f"{source_key}|{rule.category}|{summary}".encode("utf-8")).hexdigest()[:16]
    issue_object = _problem_subject(event, text, rule.category)
    topic_basis = f"{goal.id}|{source_key.casefold()}|{rule.category.casefold()}"
    if issue_object:
        topic_basis = f"{topic_basis}|{issue_object}"
    topic_digest = hashlib.sha256(
        topic_basis.encode("utf-8")
    ).hexdigest()[:24]
    return AdviceCandidate(
        user_id=event.user_id,
        domain=goal.domain,
        requested_level=rule.level,
        goal_id=goal.id,
        goal_quote=goal.quote,
        evidence=[
            Evidence(
                event_id=event.event_id,
                source=event.source,
                fact=observed[:1000],
                observed_at=event.occurred_at,
                confidence=event.confidence,
            )
        ],
        action=_rule_text(rule, "action", locale),
        first_step=_rule_text(rule, "first_step", locale),
        alternative=_rule_text(rule, "alternative", locale),
        prediction=Prediction(
            outcome=_rule_text(rule, "prediction", locale),
            deadline=utc_now() + timedelta(hours=rule.horizon_hours),
            confidence=min(0.92, 0.68 + rule.urgency * 0.18),
        ),
        adopted_expected_result=_rule_text(rule, "adopted_expected_result", locale),
        adopted_confidence=rule.adopted_confidence,
        urgency=rule.urgency,
        impact=rule.impact,
        novelty=0.82,
        relevance=relevance,
        context_fit=1.0,
        interruption_cost=0.18 if rule.level >= AdviceLevel.L3 else 0.12,
        dedupe_key=f"problem:{rule.category}:{digest}",
        topic_key=f"problem:{topic_digest}",
        issue_subject=(f"{rule.category}:{issue_object}" if issue_object else None),
    )


def deterministic_intervention_allowed(event: Event) -> bool:
    """Return whether source metadata supports deterministic problem rules.

    Missing metadata keeps compatibility with existing structured notification,
    calendar, message, and self-report events. New content collectors must set
    the attribution fields so documents, searches, videos, and other people's
    chat messages cannot be mistaken for owner facts merely because they contain
    words such as "failed", "deadline", or "risk".
    """

    facts = event.facts
    if str(facts.get("resolution_state", "")).strip().casefold() == "resolved":
        return False

    content_kind = str(facts.get("content_kind", "")).strip().casefold()
    speaker = str(facts.get("speaker", "")).strip().casefold()
    evidence_strength = str(facts.get("evidence_strength", "")).strip().casefold()

    if content_kind in _CONTEXT_ONLY_CONTENT_KINDS:
        return False
    if evidence_strength in _NON_DIRECT_EVIDENCE_STRENGTHS:
        return False
    if speaker and speaker not in _DIRECT_CONTENT_SPEAKERS:
        return False
    return True


def _rule_has_direct_semantic_evidence(event: Event, text: str, category: str) -> bool:
    """Require runtime/near-term evidence for the two noisiest rule families."""

    if category not in {"operation_failure", "deadline_risk"}:
        return True
    if _looks_like_code_or_document(event, text):
        return False
    if category == "deadline_risk":
        if _has_materially_future_date(event, text):
            return False
        return _NEAR_TERM_DEADLINE.search(text) is not None

    facts = event.facts
    content_kind = str(facts.get("content_kind", "")).strip().casefold()
    speaker = str(facts.get("speaker", "")).strip().casefold()
    evidence_strength = str(facts.get("evidence_strength", "")).strip().casefold()
    if speaker in _DIRECT_CONTENT_SPEAKERS and evidence_strength == "direct":
        return True
    if content_kind in {"system_notice", "user_input"}:
        return True
    if any(
        str(facts.get(name, "")).strip()
        for name in ("error_code", "failure_code", "exception_type", "failed_operation")
    ):
        return True
    # Notifications are direct system observations in the legacy event model;
    # self reports are direct owner observations.  Generic visible/shared/mail
    # text without attribution must go through semantic review instead.
    return event.type in {
        "notification.posted",
        "ime.text_committed",
        "speech.transcript",
        "thought.note",
    }


def _looks_like_code_or_document(event: Event, text: str) -> bool:
    facts = event.facts
    content_kind = str(facts.get("content_kind", "")).strip().casefold()
    if content_kind in _CONTEXT_ONLY_CONTENT_KINDS:
        return True
    surface = " ".join(
        str(facts.get(name, "") or "")
        for name in (
            "package",
            "process",
            "process_name",
            "app_label",
            "window_title",
            "title",
            "file_path",
            "document",
        )
    )
    if _CODE_OR_DOCUMENT_SURFACE.search(surface):
        return True
    return event.type in {"ui.visible_text", "shared.text", "mail.received"} and bool(
        _CODE_TEXT.search(text)
    )


def _has_materially_future_date(event: Event, text: str) -> bool:
    """Return true when the only explicit date is more than 48 hours away."""

    now = utc_now()
    parsed: list[datetime] = []
    values = [text]
    values.extend(
        str(event.facts.get(name, "") or "")
        for name in ("deadline", "due_at", "expires_at", "end_at")
    )
    for value in values:
        for year, month, day in _ISO_DATE.findall(value):
            try:
                parsed.append(
                    datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
                )
            except ValueError:
                continue
    return bool(parsed) and min(parsed) > now + timedelta(hours=48)


def _problem_subject(event: Event, text: str, category: str) -> str:
    """Extract stable issue identity without dates, amounts or status values."""

    explicit = _normalized_summary_subject(event.facts.get("issue_subject"), category)
    if explicit:
        return f"explicit:{explicit}"

    entities = _entity_subject(event.facts.get("entities"))
    if entities:
        return f"entities:{entities}"

    for field in _STRUCTURED_SUBJECT_ID_FIELDS:
        component = _structured_subject_component(
            event.facts.get(field),
            keep_numbers=True,
        )
        if component:
            return f"{field}:{component}"
    for field in _STRUCTURED_SUBJECT_NAME_FIELDS:
        component = _structured_subject_component(
            event.facts.get(field),
            keep_numbers=False,
        )
        if component:
            return f"{field}:{component}"

    normalized = _normalized_summary_subject(text, category)
    return f"summary:{normalized}" if normalized else ""


def _entity_subject(value: Any) -> str:
    candidates: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            component = _structured_subject_component(item, keep_numbers=False)
            if component:
                candidates.append(component)
            return

        entity_type = str(item.get("type") or item.get("kind") or "").casefold()
        if entity_type in _VOLATILE_ENTITY_TYPES:
            return
        if any(key in item for key in ("id", "key", "name", "label", "text", "value")):
            for key in ("id", "key", "name", "label", "text", "value"):
                component = _structured_subject_component(
                    item.get(key),
                    keep_numbers=key in {"id", "key"},
                )
                if component:
                    prefix = _structured_subject_component(entity_type, keep_numbers=False)
                    candidates.append(f"{prefix}:{component}" if prefix else component)
                    return
            return
        for key in sorted(item):
            if str(key).casefold() in _VOLATILE_ENTITY_TYPES:
                continue
            component = _structured_subject_component(item[key], keep_numbers=False)
            if component:
                candidates.append(f"{str(key).casefold()}:{component}")

    visit(value)
    return "|".join(sorted(set(candidates)))[:200]


def _structured_subject_component(value: Any, *, keep_numbers: bool) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [
            _structured_subject_component(item, keep_numbers=keep_numbers)
            for item in value
        ]
        return "-".join(part for part in parts if part)[:160]
    if isinstance(value, dict):
        parts = [
            _structured_subject_component(value[key], keep_numbers=keep_numbers)
            for key in sorted(value)
            if str(key).casefold() not in _VOLATILE_ENTITY_TYPES
        ]
        return "-".join(part for part in parts if part)[:160]
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    if not keep_numbers:
        text = re.sub(r"\d+(?:[.,:/-]\d+)*\s*%?", " ", text)
    text = re.sub(r"[^\w\u3400-\u9fff]+", "-", text, flags=re.UNICODE)
    return text.strip("-_")[:160]


def protect_labeled_identifiers(text: str) -> tuple[str, dict[str, str]]:
    """Hide stable labelled IDs while volatile numeric values are removed."""

    replacements: dict[str, str] = {}

    def alpha_key(index: int) -> str:
        value = index + 1
        result = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(ord("a") + remainder) + result
        return result

    def replace(match: re.Match[str]) -> str:
        raw_label = match.group("label")
        raw_identifier = match.group("value")
        label = raw_label.casefold()
        # A bare noun in prose (for example, "order failed") is not an ID.
        # Preserve a direct value only when it contains a digit; alphabetic
        # identifiers must carry an explicit id/no/#/tail-style marker.
        explicit_marker = bool(
            re.search(
                r"\b(?:id|no\.?|number|ending|last|tail)\b|#|号|编号|后四位|末四位",
                raw_label,
                re.IGNORECASE,
            )
        )
        if not explicit_marker and not re.search(r"\d", raw_identifier):
            return match.group(0)
        if "order" in label or "订单" in label:
            canonical_label = "order-id"
        elif "invoice" in label or "发票" in label:
            canonical_label = "invoice-id"
        elif "project" in label or "项目" in label:
            canonical_label = "project-id"
        elif "card" in label or "卡" in label:
            canonical_label = "card-tail"
        elif "ticket" in label or "工单" in label:
            canonical_label = "ticket-id"
        elif "booking" in label or "预订" in label:
            canonical_label = "booking-id"
        else:
            canonical_label = "case-id"
        identifier = re.sub(
            r"[^a-z0-9\u3400-\u9fff_-]+",
            "-",
            raw_identifier.casefold(),
        ).strip("-_")
        token = f"stableidentifiertoken{alpha_key(len(replacements))}"
        replacements[token] = f"{canonical_label}-{identifier}"
        return f" {token} "

    return _STABLE_LABELED_IDENTIFIER.sub(replace, text), replacements


def _normalized_summary_subject(value: Any, category: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text, stable_identifiers = protect_labeled_identifiers(text)
    # A labelled object ID is a stronger identity key than the surrounding
    # notification wording. Titles, status prose and retry counts may all
    # change while the real-world order/invoice/project/card stays the same.
    if stable_identifiers:
        return "-".join(sorted(set(stable_identifiers.values())))[:160]
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(
        r"\b\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b|"
        r"\b\d{1,2}:\d{2}(?::\d{2})?\b",
        " ",
        text,
    )
    text = re.sub(
        r"(?:[$¥€£]\s*)?\d+(?:[.,:/-]\d+)*\s*"
        r"(?:%|元|美元|人民币|usd|cny|rmb|hours?|hrs?|days?|天|小时)?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = _VOLATILE_SUBJECT_WORDS.sub(" ", text)
    category_words = _GENERIC_CATEGORY_WORDS.get(category)
    if category_words is not None:
        text = category_words.sub(" ", text)
    for token, identifier in stable_identifiers.items():
        text = text.replace(token, identifier)
    text = re.sub(
        r"\b(?:the|a|an|is|are|was|were|with|for|from|at|on|in|"
        r"remains?|needs?|review|alert|notification)\b",
        " ",
        text,
    )
    text = re.sub(r"\b(?:usd|cny|rmb)\b|人民币|美元|金额|小时|天", " ", text)
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text).strip("-_")[:160]


def _goal_score(goal: Goal, text: str, package_name: str) -> float:
    if not text:
        return 0.0
    target = goal.target
    score = 0.0
    keywords = target.get("keywords", [])
    if isinstance(keywords, str):
        keywords = re.split(r"[,，\n]+", keywords)
    for keyword in keywords if isinstance(keywords, list) else []:
        normalized = str(keyword).strip().casefold()
        if normalized and normalized in text:
            score += 5.0
    packages = target.get("packages", [])
    if isinstance(packages, str):
        packages = re.split(r"[,，\n]+", packages)
    for package in packages if isinstance(packages, list) else []:
        normalized = str(package).strip().casefold()
        if normalized and normalized in package_name:
            score += 6.0
    title = goal.title.strip().casefold()
    if len(title) >= 2 and title in text:
        score += 5.0
    terms = _meaningful_terms(f"{goal.title} {goal.quote}")
    score += min(4.0, sum(1.0 for term in terms if term in text))
    if _is_meta_goal(goal):
        # A goal describing how the assistant itself should behave is a policy
        # coordinate, not the destination of ordinary business/life evidence.
        # Keep it selectable for genuine Mouchen product work, but make it lose
        # to any concrete goal for unrelated observations.
        score *= (
            0.50
            if _MOUCHEN_PRODUCT_CONTEXT.search(f"{text} {package_name}")
            else 0.15
        )
    return score


def _is_meta_goal(goal: Goal) -> bool:
    target = goal.target if isinstance(goal.target, dict) else {}
    if target.get("meta_goal") is True or target.get("goal_kind") == "meta":
        return True
    return _META_GOAL.search(f"{goal.title} {goal.quote}") is not None


def _meaningful_terms(value: str) -> set[str]:
    stop = {"今天", "本周", "目标", "完成", "重要", "事情", "当前", "自己", "需要", "问题"}
    terms = {word.casefold() for word in re.findall(r"[A-Za-z0-9_\-]{3,}", value)}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        if len(chunk) <= 4:
            terms.add(chunk)
        else:
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return {term for term in terms if term not in stop}


def _summary(event: Event, text: str) -> str:
    title = str(event.facts.get("title") or event.facts.get("subject") or "").strip()
    if title and text.startswith(title):
        remainder = text[len(title) :].strip(" ：:-")
        return f"{title}：{remainder[:260]}" if remainder else title[:320]
    return text[:320]


def _flatten_text(values: Iterable[Any]) -> Iterable[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            yield value.strip()
        elif isinstance(value, (list, tuple)):
            yield from _flatten_text(value)
        elif isinstance(value, dict):
            yield from _flatten_text(value.values())
