from __future__ import annotations

import json
import re
from typing import Any


class CloudPrivacyError(ValueError):
    """Raised when a cloud-bound request cannot be made privacy safe."""


CLOUD_PROVIDERS = frozenset(
    {
        "openai",
        "codex_cli",
        "anthropic",
        "anthropic_via_claude_code",
        "claude_code",
    }
)

# Android names the owner's double-opt-in full-context grant
# ``owner_full_context``.  Older backend clients used ``alpha.raw_cloud``.
# Keep the vocabulary centralized so restricted data is never accidentally
# promoted to raw merely because one call site recognizes a different label.
RESTRICTED_MINIMIZED_CONSENT_SCOPES = frozenset(
    {"alpha.minimized_context", "alpha.raw_cloud", "owner_full_context"}
)
RESTRICTED_RAW_CONSENT_SCOPES = frozenset(
    {"alpha.raw_cloud", "owner_full_context"}
)

MAX_CLOUD_PROMPT_CHARS = 3_000
MAX_CLOUD_CONTEXT_CHARS = 8_000
MAX_CLOUD_VALUE_CHARS = 1_200
MAX_CLOUD_ITEMS = 12
MAX_CLOUD_DEPTH = 5

_LOCAL_ONLY_KEYS = frozenset(
    {
        "raw",
        "raw_text",
        "raw_content",
        "payload",
        "body",
        "mail_body",
        "email_body",
        "html",
        "text",
        "notification_text",
        "notification_body",
        "ime_text",
        "committed_text",
        "accessibility_text",
        "visible_text",
        "accessibility_tree",
        "view_hierarchy",
        "screen_text",
        "screen_capture",
        "screenshot",
        "image",
        "image_data",
        "ocr",
        "audio",
        "audio_data",
        "recording",
        "transcript",
        "clipboard",
        "attachment",
        "attachments",
        "binary",
        "blob",
    }
)
_SAFE_SLICE_KEYS = frozenset(
    {
        "action",
        "alternative",
        "category",
        "current_action",
        "current_active_advice",
        "current_first_step",
        "domain",
        "event_source",
        "event_text",
        "evidence",
        "first_step",
        "goal_quote",
        "goal_title",
        "prediction",
        "prediction_outcome",
        "proposed_action",
        "recent_related_events",
        "recent_owner_context",
        "signal",
        "summary",
    }
)
_SECRET_KEY_PARTS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "private_key",
        "passcode",
        "otp",
        "pin",
        "session_id",
    }
)
_IDENTIFIER_KEYS = frozenset(
    {
        "email",
        "phone",
        "mobile",
        "address",
        "home_address",
        "name",
        "full_name",
        "contact",
        "account",
        "account_id",
        "device_id",
        "advertising_id",
        "imei",
        "imsi",
        "latitude",
        "longitude",
        "location",
    }
)

_PEM_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|pwd|"
    r"secret|authorization|cookie)\b\s*[\"']?\s*[:=]\s*[\"']?"
    r"(?:bearer\s+)?[^\s,，;；}\]]{4,}"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_SECRET_RE = re.compile(
    r"\b(?:sk|pk|ghp|github_pat|glpat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}(?![A-Za-z0-9._~+/=-])",
    re.IGNORECASE,
)
_MOUCHEN_TOKEN_RE = re.compile(
    r"\bmch_at_[0-9a-f]{32}\.[A-Za-z0-9_-]{40,}\b",
    re.IGNORECASE,
)
_LABELED_ONE_TIME_SECRET_RE = re.compile(
    r"(?i)(验证码|动态码|支付密码|交易密码|安全码|otp|one[ _-]?time[ _-]?code|"
    r"passcode|pin|cvv|cvc)\s*[:：=]?\s*\d{3,10}"
)
_PAYMENT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_URL_RE = re.compile(r"\b(?:https?|ftp)://[^\s<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<!\w)[A-Z]:\\(?:[^\\\s]+\\)*[^\s,，;；]*")
_HOME_PATH_RE = re.compile(r"(?i)(?<!\w)/(?:users|home)/[^/\s]+(?:/[^\s,，;；]*)?")
_IPV4_RE = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
)
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_UUID_RE = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-"
    r"[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}\b"
)
_COORDINATES_RE = re.compile(
    r"(?<!\d)-?\d{1,3}\.\d{4,}\s*[,，]\s*-?\d{1,3}\.\d{4,}(?!\d)"
)
_CN_ID_RE = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:1[3-9]\d(?:[ -]?\d){8}|"
    r"(?:\d{2,4}[ -])?\d{7,8})(?!\d)"
)
_LONG_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){9,19}(?!\d)")
_ONE_TIME_CODE_RE = re.compile(r"(?<!\d)\d{6,8}(?!\d)")
_LONG_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{24,}\b")
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_][A-Za-z0-9_.-]{1,63}\b")
_PERSON_LABEL_RE = re.compile(
    r"(?i)(姓名|联系人|收件人|发件人|name|contact|recipient|sender)\s*[:：=]\s*"
    r"[^\s,，;；]{2,80}"
)
_ADDRESS_LABEL_RE = re.compile(
    r"(?i)(地址|住址|收货地址|address)\s*[:：=]\s*[^\r\n;；]{3,160}"
)
_ACCOUNT_LABEL_RE = re.compile(
    r"(?i)(微信号|QQ号|账号|银行卡号|身份证号|device[_ -]?id|imei|imsi)\s*[:：=]\s*"
    r"[^\s,，;；]{3,100}"
)
_LICENSE_PLATE_RE = re.compile(r"(?<![\u4e00-\u9fffA-Z0-9])[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}(?![A-Z0-9])")

_RESIDUAL_UNSAFE_PATTERNS = (
    _PEM_RE,
    _SECRET_ASSIGNMENT_RE,
    _JWT_RE,
    _PROVIDER_SECRET_RE,
    _BEARER_RE,
    _URL_RE,
    _EMAIL_RE,
    _WINDOWS_PATH_RE,
    _HOME_PATH_RE,
    _IPV4_RE,
    _MAC_RE,
    _UUID_RE,
    _COORDINATES_RE,
    _CN_ID_RE,
    _PHONE_RE,
    _LONG_NUMBER_RE,
    _ONE_TIME_CODE_RE,
    _LONG_HEX_RE,
    _HANDLE_RE,
    _PERSON_LABEL_RE,
    _ADDRESS_LABEL_RE,
    _ACCOUNT_LABEL_RE,
    _LICENSE_PLATE_RE,
)


def is_cloud_provider(provider: str) -> bool:
    return provider.strip().lower() in CLOUD_PROVIDERS


def consent_allows_restricted_minimized(consent_scope: str) -> bool:
    return str(consent_scope or "").strip() in RESTRICTED_MINIMIZED_CONSENT_SCOPES


def consent_allows_restricted_raw(consent_scope: str) -> bool:
    return str(consent_scope or "").strip() in RESTRICTED_RAW_CONSENT_SCOPES


def redact_text(value: str, max_chars: int = MAX_CLOUD_VALUE_CHARS) -> str:
    """Best-effort deterministic redaction before a text slice can leave the backend."""

    redacted = str(value or "").replace("\x00", " ")
    replacements = (
        (_PEM_RE, "[secret]"),
        (_SECRET_ASSIGNMENT_RE, "[secret]"),
        (_JWT_RE, "[secret]"),
        (_PROVIDER_SECRET_RE, "[secret]"),
        (_BEARER_RE, "[secret]"),
        (_URL_RE, "[url]"),
        (_EMAIL_RE, "[email]"),
        (_WINDOWS_PATH_RE, "[path]"),
        (_HOME_PATH_RE, "[path]"),
        (_COORDINATES_RE, "[location]"),
        (_IPV4_RE, "[ip]"),
        (_MAC_RE, "[device]"),
        (_UUID_RE, "[id]"),
        (_CN_ID_RE, "[id]"),
        (_ACCOUNT_LABEL_RE, "[account]"),
        (_PERSON_LABEL_RE, "[person]"),
        (_ADDRESS_LABEL_RE, "[address]"),
        (_LICENSE_PLATE_RE, "[vehicle]"),
        (_PHONE_RE, "[phone]"),
        (_LONG_NUMBER_RE, "[number]"),
        (_ONE_TIME_CODE_RE, "[number]"),
        (_LONG_HEX_RE, "[id]"),
        (_HANDLE_RE, "[handle]"),
    )
    for pattern, replacement in replacements:
        redacted = pattern.sub(replacement, redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[: max(0, max_chars)]


def sanitize_cloud_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded data-minimized copy; never mutate the locally stored source."""

    if not isinstance(context, dict):
        raise CloudPrivacyError("cloud context must be an object")
    budget = [MAX_CLOUD_CONTEXT_CHARS]
    sanitized = _sanitize_value(context, key="context", depth=0, budget=budget)
    if not isinstance(sanitized, dict):
        raise CloudPrivacyError("cloud context could not be minimized")
    _assert_cloud_safe("", sanitized)
    return sanitized


def prepare_cloud_request(
    prompt: str,
    context: dict[str, Any],
    *,
    allow_raw: bool = False,
) -> tuple[str, dict[str, Any]]:
    if allow_raw:
        # "Raw" means that the owner has allowed personal context such as
        # names, conversations and URLs to leave the device.  It never grants
        # permission to export credentials, one-time codes, private keys or
        # payment-card numbers.
        raw_prompt = _redact_secrets_only(
            str(prompt or ""), MAX_CLOUD_PROMPT_CHARS
        )
        if not raw_prompt.strip():
            raise CloudPrivacyError("cloud prompt is empty")
        if not isinstance(context, dict):
            raise CloudPrivacyError("cloud context must be an object")
        budget = [MAX_CLOUD_CONTEXT_CHARS]
        raw_context = _bound_raw_value(context, depth=0, budget=budget)
        if not isinstance(raw_context, dict):
            raise CloudPrivacyError("raw cloud context could not be bounded")
        return raw_prompt, raw_context
    safe_prompt = redact_text(prompt, MAX_CLOUD_PROMPT_CHARS)
    if not safe_prompt:
        raise CloudPrivacyError("cloud prompt is empty after privacy filtering")
    safe_context = sanitize_cloud_context(context)
    _assert_cloud_safe(safe_prompt, safe_context)
    return safe_prompt, safe_context


def _bound_raw_value(value: Any, *, depth: int, budget: list[int]) -> Any:
    """Bound an authorized personal slice while always removing credentials."""

    if depth > MAX_CLOUD_DEPTH or budget[0] <= 0:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        text = str(value)
        if isinstance(value, int) and 13 <= len(text.lstrip("-")) <= 19:
            budget[0] -= len("[secret]")
            return "[secret]"
        budget[0] -= len(text)
        return value
    if isinstance(value, str):
        bounded = _redact_secrets_only(
            value, min(MAX_CLOUD_VALUE_CHARS, budget[0])
        )
        budget[0] -= len(bounded)
        return bounded
    if isinstance(value, bytes):
        return "[binary omitted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_CLOUD_ITEMS or budget[0] <= 0:
                result["_truncated"] = True
                break
            safe_key = str(key).replace("\x00", " ")[:80] or f"field_{index}"
            if _is_raw_secret_key(safe_key):
                result[safe_key] = "[secret]"
                budget[0] -= len("[secret]")
            else:
                result[safe_key] = _bound_raw_value(
                    item, depth=depth + 1, budget=budget
                )
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            _bound_raw_value(item, depth=depth + 1, budget=budget)
            for item in items[:MAX_CLOUD_ITEMS]
            if budget[0] > 0
        ]
        if len(items) > MAX_CLOUD_ITEMS:
            result.append("[truncated]")
        return result
    return _redact_secrets_only(
        str(value), min(MAX_CLOUD_VALUE_CHARS, budget[0])
    )


def _redact_secrets_only(value: str, max_chars: int) -> str:
    """Preserve opted-in personal context but never emit authentication data."""

    redacted = str(value or "").replace("\x00", " ")
    for pattern in (
        _PEM_RE,
        _SECRET_ASSIGNMENT_RE,
        _JWT_RE,
        _PROVIDER_SECRET_RE,
        _BEARER_RE,
        _MOUCHEN_TOKEN_RE,
        _LABELED_ONE_TIME_SECRET_RE,
        _PAYMENT_CARD_RE,
    ):
        redacted = pattern.sub("[secret]", redacted)
    return redacted[: max(0, max_chars)]


def _is_raw_secret_key(value: str) -> bool:
    folded = str(value or "").casefold()
    if any(
        marker in folded
        for marker in (
            "验证码",
            "动态码",
            "密码",
            "安全码",
            "银行卡",
            "card number",
            "card_number",
            "bank card",
            "cvv",
            "cvc",
        )
    ):
        return True
    return _contains_secret_key(_normalized_key(folded))


def _sanitize_value(value: Any, key: str, depth: int, budget: list[int]) -> Any:
    normalized_key = _normalized_key(key)
    if _is_local_only_key(normalized_key):
        return "[local-only]"
    if _contains_secret_key(normalized_key):
        return "[secret]"
    if normalized_key in _IDENTIFIER_KEYS:
        return f"[{normalized_key}]"
    if depth > MAX_CLOUD_DEPTH or budget[0] <= 0:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        text = str(value)
        if len(re.sub(r"\D", "", text)) >= 9:
            return "[number]"
        budget[0] -= len(text)
        return value
    if isinstance(value, str):
        limit = min(MAX_CLOUD_VALUE_CHARS, max(0, budget[0]))
        safe = redact_text(value, limit)
        budget[0] -= len(safe)
        return safe
    if isinstance(value, bytes):
        return "[local-only]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= MAX_CLOUD_ITEMS or budget[0] <= 0:
                result["_truncated"] = True
                break
            safe_key = _safe_key(raw_key, index)
            result[safe_key] = _sanitize_value(item, safe_key, depth + 1, budget)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            _sanitize_value(item, key, depth + 1, budget)
            for item in items[:MAX_CLOUD_ITEMS]
            if budget[0] > 0
        ]
        if len(items) > MAX_CLOUD_ITEMS:
            result.append("[truncated]")
        return result
    return "[unsupported]"


def _safe_key(value: Any, index: int) -> str:
    candidate = redact_text(str(value), 80)
    if not candidate or "[" in candidate:
        return f"field_{index}"
    normalized = _normalized_key(candidate)
    return normalized[:80] if normalized else f"field_{index}"


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _is_local_only_key(normalized_key: str) -> bool:
    if normalized_key in _SAFE_SLICE_KEYS:
        return False
    return (
        normalized_key in _LOCAL_ONLY_KEYS
        or normalized_key.startswith("raw_")
        or normalized_key.endswith("_blob")
        or normalized_key.endswith("_bytes")
        or any(
            normalized_key.startswith(f"{part}_")
            or normalized_key.endswith(f"_{part}")
            for part in _LOCAL_ONLY_KEYS
            if part != "text"
        )
    )


def _contains_secret_key(normalized_key: str) -> bool:
    return any(
        re.search(rf"(?:^|_){re.escape(part)}(?:_|$)", normalized_key) is not None
        for part in _SECRET_KEY_PARTS
    )


def _assert_cloud_safe(prompt: str, context: dict[str, Any]) -> None:
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    combined = f"{prompt}\n{serialized}"
    for marker in (
        "[secret]",
        "[local-only]",
        "[email]",
        "[phone]",
        "[number]",
        "[url]",
        "[path]",
        "[location]",
        "[ip]",
        "[device]",
        "[id]",
        "[account]",
        "[person]",
        "[address]",
        "[vehicle]",
        "[handle]",
    ):
        combined = combined.replace(marker, "")
    if any(pattern.search(combined) for pattern in _RESIDUAL_UNSAFE_PATTERNS):
        raise CloudPrivacyError("cloud privacy gate rejected residual sensitive data")
