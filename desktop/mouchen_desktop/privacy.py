from __future__ import annotations

import re
from typing import Any

from .settings import AppSettings


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")
_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_LONG_ID = re.compile(r"\b[A-Za-z0-9_-]{20,}\b")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|OPENSSH PRIVATE KEY)-----.*?-----END [^-]+-----",
    re.IGNORECASE | re.DOTALL,
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b"
    r"\s*[:=]\s*([^\s,;]{4,})"
)
_TOKEN_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+)?(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}|"
    r"xox[baprs]-[a-z0-9-]{16,}|AIza[a-z0-9_-]{20,}|"
    r"mch_at_[0-9a-f]{32}\.[a-z0-9_-]{40,}|"
    r"eyJ[a-z0-9_-]{10,}\.[a-z0-9._-]{10,})\b"
)
_ONE_TIME_SECRET = re.compile(
    r"(?i)(验证码|动态码|支付密码|交易密码|安全码|otp|one[ _-]?time[ _-]?code|"
    r"passcode|pin|cvv|cvc)\s*[:：=]?\s*\d{3,10}"
)


def redact_text(value: str, limit: int = 2_000) -> str:
    result = _PRIVATE_KEY.sub("[private-key]", value)
    result = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[secret]", result)
    result = _TOKEN_VALUE.sub("[token]", result)
    result = _ONE_TIME_SECRET.sub("[secret]", result)
    result = _CARD.sub("[payment-number]", result)
    result = _EMAIL.sub("[email]", result)
    result = _PHONE.sub("[phone]", result)
    result = _URL.sub("[url]", result)
    result = _CODE.sub("[number]", result)
    result = _LONG_ID.sub("[id]", result)
    result = _WINDOWS_PATH.sub("[path]", result)
    return result[:limit]


def prepare_outbound(event: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    full_local_context = settings.is_local_backend() or settings.allow_remote_full_context
    facts = event.get("facts", {})
    # Payment data, credentials and private keys are never sent, even when the
    # owner explicitly enables full remote context.
    facts = _redact_value(facts, preserve_personal=full_local_context)
    return {
        "source": str(event.get("source", "windows.desktop"))[:80],
        "type": str(event.get("type", "ui.visible_text")),
        "occurred_at": event.get("occurred_at"),
        "facts": facts,
        "entities": list(event.get("entities", []))[:20],
        "confidence": float(event.get("confidence", 1.0)),
        "sensitivity": event.get("sensitivity", "sensitive"),
        "consent_scope": (
            "owner_full_context"
            if settings.is_local_backend()
            else "alpha.raw_cloud"
            if settings.allow_remote_full_context
            else "alpha.minimized_context"
            if settings.proactive_cloud_enabled
            else event.get("consent_scope", "windows.private_alpha")
        ),
        "evidence_ref": event.get("evidence_ref") or f"windows:{event['local_id']}",
    }


def _redact_value(value: Any, depth: int = 0, preserve_personal: bool = False) -> Any:
    if depth >= 5:
        return "[nested]"
    if isinstance(value, str):
        if preserve_personal:
            return _redact_secrets(value)
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(key)[:80]: _redact_value(item, depth + 1, preserve_personal)
            for key, item in list(value.items())[:30]
            if str(key).casefold() not in {"full_path", "absolute_path", "token", "password"}
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, depth + 1, preserve_personal) for item in list(value)[:20]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact_text(str(value))


def _redact_secrets(value: str, limit: int = 12_000) -> str:
    result = _PRIVATE_KEY.sub("[private-key]", value)
    result = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[secret]", result)
    result = _TOKEN_VALUE.sub("[token]", result)
    result = _ONE_TIME_SECRET.sub("[secret]", result)
    result = _CARD.sub("[payment-number]", result)
    return result[:limit]
