from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from .analysis_queue import release_analysis_slot, try_acquire_analysis_slot
from .domain.models import AdviceLevel
from .localization import (
    advice_display_source,
    validate_advice_display_translation,
)
from .model_gateway import ModelGateway, ModelUnavailable, choose_route
from .privacy import CloudPrivacyError
from .storage import AdviceLocalizationClaim, Repository


_TRANSLATION_PROMPT = """Translate only the supplied My AI Twin display fields into natural, concise English.
Do not add advice, facts, source claims, explanations, or markdown. Preserve numbers, product names,
URLs, and protocol meaning. Return exactly one JSON object with these keys and no others:
action, first_step, alternative, prediction_outcome, adopted_expected_result.
Keep a null source value null. Every non-null value must be English text."""


@dataclass
class AdviceLocalizationDrainResult:
    attempted: int = 0
    ready: int = 0
    retried: int = 0
    unavailable: int = 0
    stale: int = 0


class AdviceLocalizationProcessor:
    """Translate a bounded set of durable jobs without touching source advice."""

    def __init__(
        self,
        repository: Repository,
        gateway: ModelGateway,
        *,
        retry_base_seconds: float = 30.0,
        max_attempts: int | None = None,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.max_attempts = _bounded_max_attempts(max_attempts)

    async def drain(self, *, limit: int = 1) -> AdviceLocalizationDrainResult:
        result = AdviceLocalizationDrainResult()
        for _ in range(max(1, min(10, int(limit)))):
            claim = self.repository.claim_next_advice_localization(
                max_attempts=self.max_attempts
            )
            if claim is None:
                break
            result.attempted += 1
            outcome = await self._process_claim(claim)
            setattr(result, outcome, getattr(result, outcome) + 1)
        return result

    async def _process_claim(self, claim: AdviceLocalizationClaim) -> str:
        route = choose_route(
            AdviceLevel.L2,
            force_private_7b=_environment_flag(
                "MOUCHEN_ADVICE_LOCALIZATION_FORCE_PRIVATE_7B"
            ),
            purpose="advice_translation",
        )
        provider = route.provider
        model = route.model
        source = advice_display_source(claim.advice)
        context = {"response_locale": claim.locale, **source}
        prompt = _TRANSLATION_PROMPT
        budget_reserved = False
        try:
            if provider not in {"template", "ollama"}:
                prepare = getattr(self.gateway, "prepare_cloud_payload", None)
                if not callable(prepare):
                    raise CloudPrivacyError(
                        "model gateway cannot prepare a private cloud payload"
                    )
                prompt, context = prepare(prompt, context, allow_raw=False)
                reserve = getattr(self.gateway, "reserve_paid_call", None)
                if callable(reserve):
                    budget_reserved = bool(
                        reserve(provider, claim.user_id, "advice_translation")
                    )
                self.repository.audit_cloud_slice(
                    claim.user_id,
                    provider,
                    model,
                    "advice_translation",
                    context,
                    prompt=prompt,
                )
            if getattr(self.gateway, "supports_pre_reserved_budget", False):
                content = await self.gateway.generate(
                    route,
                    prompt,
                    context,
                    user_id=claim.user_id,
                    global_budget_reserved=budget_reserved,
                    payload_prepared=provider not in {"template", "ollama"},
                )
            else:
                content = await self.gateway.generate(
                    route,
                    prompt,
                    context,
                    user_id=claim.user_id,
                )
            translated = validate_advice_display_translation(
                source,
                json.loads(str(content).strip()),
                claim.locale,
            )
            if self.repository.complete_advice_localization(
                claim,
                translated,
                provider=provider,
                model=model,
            ):
                return "ready"
            return "stale"
        except ModelUnavailable as exc:
            base_delay = self._retry_delay(claim.attempts)
            delay = max(base_delay, float(exc.retry_after_seconds or 0))
            return self._record_failure(
                claim,
                exc.reason_code,
                provider=provider,
                model=model,
                retryable=bool(exc.retryable),
                delay_seconds=delay,
            )
        except CloudPrivacyError:
            return self._record_failure(
                claim,
                "localization_privacy_rejected",
                provider=provider,
                model=model,
                retryable=False,
            )
        except (TypeError, ValueError):
            return self._record_failure(
                claim,
                "localization_invalid_response",
                provider=provider,
                model=model,
                retryable=True,
                delay_seconds=self._retry_delay(claim.attempts),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._record_failure(
                claim,
                "localization_internal_error",
                provider=provider,
                model=model,
                retryable=True,
                delay_seconds=self._retry_delay(claim.attempts),
            )

    def _record_failure(
        self,
        claim: AdviceLocalizationClaim,
        reason: str,
        *,
        provider: str,
        model: str,
        retryable: bool,
        delay_seconds: float = 0.0,
    ) -> str:
        status = self.repository.fail_advice_localization(
            claim,
            reason,
            provider=provider,
            model=model,
            retryable=retryable,
            delay_seconds=delay_seconds,
            max_attempts=self.max_attempts,
        )
        if status == "pending":
            return "retried"
        if status == "unavailable":
            return "unavailable"
        return "stale"

    def _retry_delay(self, attempt: int) -> float:
        exponent = min(8, max(0, int(attempt) - 1))
        return min(3_600.0, self.retry_base_seconds * (2**exponent))


class AdviceLocalizationConsumer:
    """One stoppable process-local consumer for historical translations."""

    def __init__(
        self,
        repository: Repository,
        gateway: ModelGateway,
        *,
        batch_size: int = 1,
        poll_seconds: float = 2.0,
        failure_backoff_seconds: float = 5.0,
    ) -> None:
        self.repository = repository
        self.processor = AdviceLocalizationProcessor(repository, gateway)
        self.batch_size = max(1, min(10, int(batch_size)))
        self.poll_seconds = max(0.1, min(60.0, float(poll_seconds)))
        self.failure_backoff_seconds = max(
            0.1, min(60.0, float(failure_backoff_seconds))
        )
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self.repository.enqueue_current_english_advice_localizations()
            self._task = asyncio.create_task(
                self._run(), name="mouchen-advice-localization-consumer"
            )
            self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None

    async def _run(self) -> None:
        failure_delay = 0.0
        while not self._stopping:
            if failure_delay:
                await self._wait(failure_delay)
                failure_delay = 0.0
                continue
            if not try_acquire_analysis_slot():
                await self._wait(0.1)
                continue
            try:
                result = await self.processor.drain(limit=self.batch_size)
            except asyncio.CancelledError:
                raise
            except Exception:
                failure_delay = self.failure_backoff_seconds
                continue
            finally:
                release_analysis_slot()
            if result.attempted == 0:
                await self._wait(self.poll_seconds)

    async def _wait(self, delay: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def _bounded_max_attempts(explicit: int | None) -> int:
    value = explicit
    if value is None:
        try:
            value = int(os.getenv("MOUCHEN_ADVICE_LOCALIZATION_MAX_ATTEMPTS", "5"))
        except ValueError:
            value = 5
    return max(1, min(20, int(value)))


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
