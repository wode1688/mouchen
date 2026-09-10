from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


TRANSLATION_PENDING = "Translation pending"
TRANSLATION_UNAVAILABLE = "Translation unavailable"

_TRANSLATION_STATUSES = {"original", "ready", "pending", "unavailable"}
_HAN_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True, slots=True)
class AdviceDisplay:
    """One immutable, non-persistent projection of server-authored advice."""

    action: str
    first_step: str
    alternative: str | None
    prediction_outcome: str
    adopted_expected_result: str | None
    translation_status: str
    uses_translation: bool


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _original(advice: Mapping[str, Any]) -> AdviceDisplay:
    prediction = advice.get("prediction")
    prediction_outcome = (
        _text(prediction.get("outcome")) if isinstance(prediction, Mapping) else ""
    )
    alternative = advice.get("alternative")
    adopted = advice.get("adopted_expected_result")
    return AdviceDisplay(
        action=_text(advice.get("action")),
        first_step=_text(advice.get("first_step")),
        alternative=None if alternative is None else _text(alternative),
        prediction_outcome=prediction_outcome,
        adopted_expected_result=None if adopted is None else _text(adopted),
        translation_status="original",
        uses_translation=False,
    )


def _placeholder(
    message: str,
    status: str,
    original: AdviceDisplay,
) -> AdviceDisplay:
    return AdviceDisplay(
        action=message,
        first_step=message,
        alternative=message if original.alternative is not None else None,
        prediction_outcome=message,
        adopted_expected_result=(
            message if original.adopted_expected_result is not None else None
        ),
        translation_status=status,
        uses_translation=False,
    )


def _is_english(value: Any) -> bool:
    return str(value or "").strip().replace("_", "-").casefold().startswith("en")


def _contains_han_generated_text(original: AdviceDisplay) -> bool:
    return any(
        _HAN_TEXT.search(value or "")
        for value in (
            original.action,
            original.first_step,
            original.alternative,
            original.prediction_outcome,
            original.adopted_expected_result,
        )
    )


def project_advice(advice: Mapping[str, Any], locale: str) -> AdviceDisplay:
    """Return the visible fields without mutating or replacing the advice payload.

    English placeholders are deliberately complete projections: an incomplete or
    in-flight translation can never leak an original-language action or first step.
    """

    original = _original(advice)
    if not _is_english(locale):
        return original

    status = str(advice.get("display_translation_status") or "original").casefold()
    if status not in _TRANSLATION_STATUSES or status == "original":
        return (
            _placeholder(TRANSLATION_PENDING, "pending", original)
            if _contains_han_generated_text(original)
            else original
        )
    if status == "pending":
        return _placeholder(TRANSLATION_PENDING, status, original)
    if status == "unavailable":
        return _placeholder(TRANSLATION_UNAVAILABLE, status, original)

    display = advice.get("display")
    if not _is_english(advice.get("display_locale")) or not isinstance(display, Mapping):
        return _placeholder(TRANSLATION_UNAVAILABLE, "unavailable", original)

    required = ("action", "first_step", "prediction_outcome")
    if any(not _text(display.get(key)).strip() for key in required):
        return _placeholder(TRANSLATION_UNAVAILABLE, "unavailable", original)
    for key, original_value in (
        ("alternative", original.alternative),
        ("adopted_expected_result", original.adopted_expected_result),
    ):
        if original_value and not _text(display.get(key)).strip():
            return _placeholder(TRANSLATION_UNAVAILABLE, "unavailable", original)

    alternative = display.get("alternative")
    adopted = display.get("adopted_expected_result")
    translated = AdviceDisplay(
        action=_text(display.get("action")),
        first_step=_text(display.get("first_step")),
        alternative=None if alternative is None else _text(alternative),
        prediction_outcome=_text(display.get("prediction_outcome")),
        adopted_expected_result=None if adopted is None else _text(adopted),
        translation_status="ready",
        uses_translation=True,
    )
    return (
        _placeholder(TRANSLATION_UNAVAILABLE, "unavailable", original)
        if _contains_han_generated_text(translated)
        else translated
    )
