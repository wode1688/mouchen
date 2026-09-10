from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


DEFAULT_LOCALE = "zh-CN"
SUPPORTED_LOCALES = (DEFAULT_LOCALE, "en-US")

ADVICE_DISPLAY_KEYS = (
    "action",
    "first_step",
    "alternative",
    "prediction_outcome",
    "adopted_expected_result",
)
_ADVICE_DISPLAY_LIMITS = {
    "action": 2000,
    "first_step": 1000,
    "alternative": 2000,
    "prediction_outcome": 1000,
    "adopted_expected_result": 1000,
}
_ADVICE_DISPLAY_OPTIONAL_KEYS = frozenset(
    {"alternative", "adopted_expected_result"}
)

_HAN_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

_LOCALE_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-sg": "zh-CN",
    "en": "en-US",
    "en-us": "en-US",
}


def normalize_locale(value: str | None, *, strict: bool = True) -> str:
    """Return one stable locale used by every My AI Twin client and prompt."""

    raw = str(value or "").strip()
    if not raw:
        if strict:
            raise ValueError("locale is required")
        return DEFAULT_LOCALE
    normalized = _LOCALE_ALIASES.get(raw.casefold())
    if normalized is None:
        if strict:
            raise ValueError(f"unsupported locale: {raw}")
        return DEFAULT_LOCALE
    return normalized


def is_english(locale: str | None) -> bool:
    return normalize_locale(locale, strict=False) == "en-US"


def model_output_language_instruction(locale: str | None) -> str:
    """Language rule appended to model prompts without changing JSON keys.

    Evidence quotes remain verbatim because they are later checked against the
    source event. Translating them would break traceability and could turn a
    paraphrase into apparent evidence.
    """

    if is_english(locale):
        return (
            "OUTPUT LANGUAGE: Write every user-facing generated value in natural, "
            "concise English (category, action, first_step, "
            "alternative, prediction_outcome, adopted_expected_result, and reason). "
            "issue_subject is a non-user-facing canonical identity: always return it "
            "as a stable lower-case ASCII English phrase and never translate it; the "
            "selected locale must not change issue_subject. "
            "Keep JSON keys and protocol values unchanged. evidence_quote must remain "
            "an exact verbatim substring of event_text in its original language; never "
            "translate, paraphrase, or normalize evidence_quote."
        )
    return (
        "输出语言：所有面向用户的新生成内容均使用自然、简洁的中文（包括 category、"
        "action、first_step、alternative、prediction_outcome、"
        "adopted_expected_result 和 reason）；JSON 键名与协议值保持不变。"
        "issue_subject 不是面向用户的内容，必须始终使用稳定的小写 ASCII 英文短语，"
        "不得翻译，且不得随 zh-CN/en-US 切换而变化。"
        "evidence_quote 必须逐字保留 event_text 中的原始语言，不得翻译、改写或归一化。"
    )


def generated_values_match_locale(
    locale: str | None,
    *values: object,
) -> bool:
    """Conservatively reject a model answer written in the wrong language.

    The check applies only to newly generated user-facing prose.  Evidence and
    owner-provided titles are intentionally checked elsewhere (or excluded),
    because they must remain verbatim and can legitimately use another script.
    English-mode generated prose must contain at least one Latin letter and no
    Han characters. Product names, evidence, and owner-provided text are not
    included in ``values``, so allowing Han here would leak Chinese generated
    output into an account that explicitly requested English-only output.
    """

    text = " ".join(str(value or "").strip() for value in values).strip()
    if not text:
        return False
    han_count = len(_HAN_CHARACTER.findall(text))
    latin_count = len(_LATIN_LETTER.findall(text))
    if is_english(locale):
        return latin_count > 0 and han_count == 0
    # Chinese output commonly contains product names, code, URLs, and quoted
    # English source material. The regression being guarded here is the
    # lossy en-US -> Chinese path; zh-CN remains prompt-directed so valid
    # technical answers are not discarded by a crude script heuristic.
    return True


def advice_display_source(advice: Any) -> dict[str, str | None]:
    """Return only model-authored prose that may be localized for display.

    Goal quotes and evidence deliberately have no path into this projection.
    Keeping this allow-list beside the locale validator gives storage, the
    worker, and API serialization one byte-stable source contract.
    """

    def value(name: str) -> Any:
        if isinstance(advice, Mapping):
            return advice.get(name)
        return getattr(advice, name, None)

    prediction = value("prediction")
    if isinstance(prediction, Mapping):
        prediction_outcome = prediction.get("outcome")
    else:
        prediction_outcome = getattr(prediction, "outcome", None)

    return {
        "action": str(value("action") or ""),
        "first_step": str(value("first_step") or ""),
        "alternative": (
            str(value("alternative"))
            if value("alternative") is not None
            else None
        ),
        "prediction_outcome": str(prediction_outcome or ""),
        "adopted_expected_result": (
            str(value("adopted_expected_result"))
            if value("adopted_expected_result") is not None
            else None
        ),
    }


def advice_display_source_hash(advice: Any) -> str:
    serialized = json.dumps(
        advice_display_source(advice),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def advice_display_source_matches_locale(advice: Any, locale: str | None) -> bool:
    source = advice_display_source(advice)
    return generated_values_match_locale(
        locale,
        *(value for value in source.values() if value is not None),
    )


def validate_advice_display_translation(
    source: Mapping[str, str | None],
    translated: Any,
    locale: str | None,
) -> dict[str, str | None]:
    """Validate one untrusted model translation against the source contract."""

    if not isinstance(translated, Mapping):
        raise ValueError("advice translation must be an object")
    if set(translated) != set(ADVICE_DISPLAY_KEYS):
        raise ValueError("advice translation keys do not match the display contract")
    result: dict[str, str | None] = {}
    for key in ADVICE_DISPLAY_KEYS:
        source_value = source.get(key)
        candidate = translated.get(key)
        if key in _ADVICE_DISPLAY_OPTIONAL_KEYS and source_value is None:
            if candidate is not None:
                raise ValueError(f"{key} must remain null")
            result[key] = None
            continue
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(f"{key} must be non-empty text")
        normalized = candidate.strip()
        if len(normalized) > _ADVICE_DISPLAY_LIMITS[key]:
            raise ValueError(f"{key} exceeds the display limit")
        result[key] = normalized
    if not generated_values_match_locale(
        locale,
        *(value for value in result.values() if value is not None),
    ):
        raise ValueError("advice translation does not match the requested locale")
    return result


def localized_model_locale_mismatch(locale: str | None) -> str:
    if is_english(locale):
        return (
            "The model returned content in the wrong language, so My AI Twin did "
            "not publish it. Please try again."
        )
    return "模型返回了错误语言的内容，AI替身未发布该结果。请重试。"


def localized_observation(source: str, quote: str, locale: str | None) -> str:
    if is_english(locale):
        return f"Observed from {source}: {quote}"
    return f"从 {source} 观察到：{quote}"
