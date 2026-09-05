from __future__ import annotations

import asyncio
import os
import threading
from typing import Literal

from pydantic import BaseModel, Field

from .advice_refiner import ProactiveAdviceRefiner, ProactiveIssueDiscoverer
from .domain.models import AdviceLevel, AdviceStatus, ContextSnapshot, EvaluationResult
from .domain.preferences import (
    event_type_allowed,
    is_internal_composite_event,
    is_manual_problem_event,
)
from .model_gateway import ModelGateway, ModelUnavailable
from .service import ProactiveService
from .storage import AnalysisJobClaim, Repository


ANALYSIS_SINGLE_FLIGHT = threading.Lock()


def _evaluation_terminal_reason(
    evaluation: EvaluationResult,
    *,
    prefix: str = "evaluation",
) -> str:
    """Persist the gate decision together with its inspectable reason codes."""

    base = f"{prefix}_{evaluation.decision}"
    codes = [str(code).strip() for code in evaluation.reason_codes if str(code).strip()]
    return f"{base}__{'__'.join(dict.fromkeys(codes))}" if codes else base


def try_acquire_analysis_slot() -> bool:
    return ANALYSIS_SINGLE_FLIGHT.acquire(blocking=False)


def release_analysis_slot() -> None:
    ANALYSIS_SINGLE_FLIGHT.release()


class _ReservedModelGateway:
    """Count provider attempts against a claim before they can leave process."""

    def __init__(
        self,
        repository: Repository,
        gateway: ModelGateway,
        reservation_id: int | None,
        user_id: str,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._reservation_id = reservation_id
        self._user_id = user_id

    def __getattr__(self, name: str):
        return getattr(self._gateway, name)

    def _record_call(self) -> None:
        if self._reservation_id is not None:
            self._repository.record_analysis_model_call(self._reservation_id)

    def reserve_paid_call(
        self,
        provider: str,
        user_id: str | None,
        purpose: str,
    ) -> bool:
        # The queue claim already reserved the worst-case two calls in the
        # same SQLite transaction. Actual use is recorded immediately before
        # each provider attempt by ``generate``/``second_opinion`` below.
        del user_id, purpose
        return str(provider or "").strip().casefold() not in {
            "",
            "template",
            "ollama",
            "disabled",
        }

    async def generate(self, *args, **kwargs):
        route = kwargs.get("route") or (args[0] if args else None)
        if getattr(route, "provider", None) in {"openai", "codex_cli"}:
            self._record_call()
        if getattr(self._gateway, "supports_pre_reserved_budget", False):
            kwargs["global_budget_reserved"] = True
        else:
            kwargs.pop("global_budget_reserved", None)
            kwargs.pop("payload_prepared", None)
        return await self._gateway.generate(*args, **kwargs)

    async def second_opinion(self, *args, **kwargs):
        provider, _ = self._gateway.second_opinion_route()
        if provider != "disabled":
            self._record_call()
        if getattr(self._gateway, "supports_pre_reserved_budget", False):
            kwargs["global_budget_reserved"] = True
            kwargs.setdefault("user_id", self._user_id)
        else:
            kwargs.pop("global_budget_reserved", None)
            kwargs.pop("payload_prepared", None)
            kwargs.pop("user_id", None)
        return await self._gateway.second_opinion(*args, **kwargs)


class AnalysisDrainResult(BaseModel):
    user_id: str
    attempted: int = 0
    published: int = 0
    no_intervention: int = 0
    retried: int = 0
    failed: int = 0
    evaluations: list[EvaluationResult] = Field(default_factory=list)


class AnalysisQueueProcessor:
    """Drain authorized durable model jobs without duplicating advice."""

    def __init__(
        self,
        repository: Repository,
        service: ProactiveService,
        gateway: ModelGateway,
        *,
        retry_base_seconds: float = 15.0,
        hourly_model_call_limit: int | None = None,
        daily_model_call_limit: int | None = None,
        global_hourly_model_call_limit: int | None = None,
        global_daily_model_call_limit: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.repository = repository
        self.service = service
        self.gateway = gateway
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.max_attempts = _bounded_max_attempts(max_attempts)
        self.hourly_model_call_limit = _bounded_budget_limit(
            hourly_model_call_limit,
            "MOUCHEN_ANALYSIS_MODEL_CALLS_PER_HOUR",
            12,
        )
        self.daily_model_call_limit = _bounded_budget_limit(
            daily_model_call_limit,
            "MOUCHEN_ANALYSIS_MODEL_CALLS_PER_DAY",
            288,
        )
        self.global_hourly_model_call_limit = _bounded_global_budget_limit(
            global_hourly_model_call_limit,
            "MOUCHEN_GLOBAL_MODEL_CALLS_PER_HOUR",
            120,
        )
        self.global_daily_model_call_limit = _bounded_global_budget_limit(
            global_daily_model_call_limit,
            "MOUCHEN_GLOBAL_MODEL_CALLS_PER_DAY",
            600,
        )

    async def process_event(
        self,
        user_id: str,
        event_id,
        context: ContextSnapshot | None = None,
    ) -> AnalysisDrainResult:
        claim = self.repository.claim_analysis_job(
            user_id,
            event_id,
            hourly_call_limit=self.hourly_model_call_limit,
            daily_call_limit=self.daily_model_call_limit,
            global_hourly_call_limit=self.global_hourly_model_call_limit,
            global_daily_call_limit=self.global_daily_model_call_limit,
            max_attempts=self.max_attempts,
        )
        if claim is None:
            return AnalysisDrainResult(user_id=user_id)
        return await self._process_claim(claim, context or ContextSnapshot())

    async def drain(
        self,
        user_id: str,
        context: ContextSnapshot | None = None,
        *,
        limit: int = 5,
    ) -> AnalysisDrainResult:
        aggregate = AnalysisDrainResult(user_id=user_id)
        gate_context = context or ContextSnapshot()
        for _ in range(max(1, min(10, limit))):
            claim = self.repository.claim_analysis_job(
                user_id,
                hourly_call_limit=self.hourly_model_call_limit,
                daily_call_limit=self.daily_model_call_limit,
                global_hourly_call_limit=self.global_hourly_model_call_limit,
                global_daily_call_limit=self.global_daily_model_call_limit,
                max_attempts=self.max_attempts,
            )
            if claim is None:
                break
            current = await self._process_claim(claim, gate_context)
            aggregate.attempted += current.attempted
            aggregate.published += current.published
            aggregate.no_intervention += current.no_intervention
            aggregate.retried += current.retried
            aggregate.failed += current.failed
            aggregate.evaluations.extend(current.evaluations)
        return aggregate

    async def _process_claim(
        self,
        claim: AnalysisJobClaim,
        context: ContextSnapshot,
    ) -> AnalysisDrainResult:
        result = AnalysisDrainResult(user_id=claim.job.user_id, attempted=1)
        # A pending/retry job re-reads the LATEST preferences immediately before
        # any model work: a pause or event-type disablement that arrived while
        # the job waited completes it here with zero model calls. The reserved
        # model-call budget is settled (unused) exactly like the normal path.
        preference_block = self._preference_block(claim)
        if preference_block is not None:
            try:
                self._complete_no_intervention(claim, preference_block)
                result.no_intervention = 1
            finally:
                if claim.reservation_id is not None:
                    self.repository.settle_analysis_model_reservation(
                        claim.reservation_id,
                        claim.job.user_id,
                        claim.job.event_id,
                        attempt=claim.job.attempts,
                    )
            return result
        gateway = _ReservedModelGateway(
            self.repository,
            self.gateway,
            claim.reservation_id,
            claim.job.user_id,
        )
        try:
            if claim.job.job_kind == "refine_deterministic":
                return await self._refine_deterministic(
                    claim,
                    context,
                    result,
                    gateway,
                )
            return await self._discover(claim, context, result, gateway)
        except asyncio.CancelledError:
            self._retry(claim, "analysis_cancelled")
            raise
        except ModelUnavailable as exc:
            reason = exc.reason_code or "model_unavailable"
            if exc.retryable:
                self._retry_or_fail(claim, reason, result)
            else:
                if self._fail(claim, reason):
                    result.failed = 1
            return result
        except Exception:
            self._retry_or_fail(claim, "analysis_processing_unavailable", result)
            return result
        finally:
            if claim.reservation_id is not None:
                self.repository.settle_analysis_model_reservation(
                    claim.reservation_id,
                    claim.job.user_id,
                    claim.job.event_id,
                    attempt=claim.job.attempts,
                )

    async def _discover(
        self,
        claim: AnalysisJobClaim,
        context: ContextSnapshot,
        result: AnalysisDrainResult,
        gateway: ModelGateway,
    ) -> AnalysisDrainResult:
        attempt = await ProactiveIssueDiscoverer(
            self.repository,
            gateway,
        ).discover_attempt(
            claim.event,
            claim.job.user_id,
            allow_restricted_minimized=claim.job.cloud_approved,
            allow_raw_cloud=claim.job.raw_cloud_approved,
            raise_model_unavailable=True,
        )
        if attempt.disposition == "retry":
            self._retry_or_fail(claim, attempt.reason_code, result)
            return result
        if attempt.disposition == "no_intervention" or attempt.candidate is None:
            self._complete_no_intervention(
                claim,
                attempt.reason_code or "model_declined",
            )
            result.no_intervention = 1
            return result

        evaluation = self.service.evaluate(
            attempt.candidate,
            context,
            l3_review_passed=True,
        )
        evaluation = evaluation.model_copy(
            update={"reason_codes": evaluation.reason_codes + ["AI_DISCOVERED"]}
        )
        self._finish_evaluation(claim, evaluation, result)
        return result

    async def _refine_deterministic(
        self,
        claim: AnalysisJobClaim,
        context: ContextSnapshot,
        result: AnalysisDrainResult,
        gateway: ModelGateway,
    ) -> AnalysisDrainResult:
        candidate = self.service.candidate_for_event(
            claim.event,
            claim.job.goal_id,
        )
        if candidate is None or candidate.requested_level < AdviceLevel.L3:
            self._retry_or_fail(
                claim,
                "deterministic_candidate_unavailable",
                result,
            )
            return result

        evaluation = self.service.evaluate(candidate, context)
        if evaluation.decision == "merge":
            self._finish_evaluation(claim, evaluation, result)
            return result
        if evaluation.decision != "publish" or evaluation.advice is None:
            self._complete_no_intervention(
                claim,
                _evaluation_terminal_reason(
                    evaluation,
                    prefix="deterministic_evaluation",
                ),
            )
            result.no_intervention = 1
            result.evaluations.append(evaluation)
            return result
        if evaluation.advice.status != AdviceStatus.PROVISIONAL:
            self.repository.delete_advice(
                evaluation.advice.user_id,
                evaluation.advice.id,
            )
            self._retry_or_fail(
                claim,
                "deterministic_provisional_required",
                result,
            )
            return result

        reviewed = await ProactiveAdviceRefiner(
            self.repository,
            gateway,
        ).refine(
            evaluation,
            claim.job.user_id,
            allow_raw_cloud=claim.job.raw_cloud_approved,
            raise_model_unavailable=True,
        )
        if "MODEL_OUTPUT_LOCALE_MISMATCH" in reviewed.reason_codes:
            self._retry_or_fail(
                claim,
                "model_output_locale_mismatch",
                result,
            )
            result.evaluations.append(reviewed)
            return result
        if "AI_REFINEMENT_UNAVAILABLE" in reviewed.reason_codes:
            self._retry_or_fail(
                claim,
                "deterministic_refinement_unavailable",
                result,
            )
            result.evaluations.append(reviewed)
            return result
        if "AI_REVIEW_REJECTED" in reviewed.reason_codes:
            self._complete_no_intervention(claim, "deterministic_review_rejected")
            result.no_intervention = 1
            result.evaluations.append(reviewed)
            return result
        self._finish_evaluation(claim, reviewed, result)
        return result

    def _finish_evaluation(
        self,
        claim: AnalysisJobClaim,
        evaluation: EvaluationResult,
        result: AnalysisDrainResult,
    ) -> None:
        if evaluation.decision in {"publish", "merge"}:
            self.repository.complete_analysis_job(
                claim.job.user_id,
                claim.job.event_id,
                "published",
                "advice_published" if evaluation.decision == "publish" else "advice_merged",
                attempt=claim.job.attempts,
            )
            result.published = 1
        else:
            self._complete_no_intervention(
                claim,
                _evaluation_terminal_reason(evaluation),
            )
            result.no_intervention = 1
        result.evaluations.append(evaluation)

    def _preference_block(self, claim: AnalysisJobClaim) -> str | None:
        """PREFERENCE_PAUSED / PREFERENCE_EVENT_TYPE_DISABLED before model work.

        Uses the job's goal when it has one, otherwise the global scope (a
        discovery job picks its goal later; the goal-level filter runs again
        inside discovery before any model call for that goal). Manually
        submitted problems and internal review composites are exempt from the
        direct event-type check, never from a pause... except manual problems,
        which the user explicitly requested and which bypass frequency gates.
        """

        event = claim.event
        if is_manual_problem_event(event):
            return None
        effective = self.repository.effective_preference(
            claim.job.user_id, claim.job.goal_id
        )
        if effective.paused:
            return "PREFERENCE_PAUSED"
        if is_internal_composite_event(event):
            return None
        if not event_type_allowed(effective.event_types, event.type):
            return "PREFERENCE_EVENT_TYPE_DISABLED"
        return None

    def _complete_no_intervention(self, claim: AnalysisJobClaim, reason: str) -> None:
        self.repository.complete_analysis_job(
            claim.job.user_id,
            claim.job.event_id,
            "no_intervention",
            reason,
            attempt=claim.job.attempts,
        )

    def _retry_or_fail(
        self,
        claim: AnalysisJobClaim,
        reason: str,
        result: AnalysisDrainResult,
    ) -> None:
        outcome = self._retry(claim, reason)
        if outcome == "retried":
            result.retried = 1
        elif outcome == "failed":
            result.failed = 1

    def _retry(
        self,
        claim: AnalysisJobClaim,
        reason: str,
    ) -> Literal["retried", "failed"] | None:
        if claim.job.attempts >= self.max_attempts:
            failed = self._fail(
                claim,
                "analysis_max_attempts_exhausted",
                failure_reason=reason,
            )
            return "failed" if failed else None
        exponent = min(8, max(0, claim.job.attempts - 1))
        delay = min(3_600.0, self.retry_base_seconds * (2**exponent))
        retried = self.repository.retry_analysis_job(
            claim.job.user_id,
            claim.job.event_id,
            reason,
            attempt=claim.job.attempts,
            delay_seconds=delay,
        )
        return "retried" if retried else None

    def _fail(
        self,
        claim: AnalysisJobClaim,
        reason: str,
        *,
        failure_reason: str | None = None,
    ) -> bool:
        return self.repository.complete_analysis_job(
            claim.job.user_id,
            claim.job.event_id,
            "no_intervention",
            reason,
            attempt=claim.job.attempts,
            failure_reason=failure_reason or reason,
        )


class AnalysisQueueConsumer:
    """One stoppable process-local consumer for authorized jobs."""

    def __init__(
        self,
        repository: Repository,
        service: ProactiveService,
        gateway: ModelGateway,
        *,
        batch_size: int = 1,
        poll_seconds: float = 2.0,
        failure_backoff_seconds: float = 5.0,
    ) -> None:
        self.repository = repository
        self.processor = AnalysisQueueProcessor(repository, service, gateway)
        self.batch_size = max(1, min(10, batch_size))
        self.poll_seconds = max(0.1, min(60.0, poll_seconds))
        self.failure_backoff_seconds = max(0.1, min(60.0, failure_backoff_seconds))
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self.repository.recheck_budget_deferred_analysis_jobs()
            self._task = asyncio.create_task(self._run(), name="mouchen-analysis-consumer")

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
            try:
                user_id = self.repository.next_due_analysis_user()
            except Exception:
                failure_delay = self.failure_backoff_seconds
                continue
            if user_id is None:
                await self._wait(self.poll_seconds)
                continue
            if not try_acquire_analysis_slot():
                await self._wait(0.1)
                continue
            try:
                await self.processor.drain(user_id, limit=self.batch_size)
            except asyncio.CancelledError:
                raise
            except Exception:
                failure_delay = self.failure_backoff_seconds
            finally:
                release_analysis_slot()

    async def _wait(self, delay: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def _bounded_budget_limit(
    explicit: int | None,
    environment_name: str,
    default: int,
) -> int:
    value = explicit
    if value is None:
        try:
            value = int(os.getenv(environment_name, str(default)))
        except ValueError:
            value = default
    return max(1, min(10_000, int(value)))


def _bounded_global_budget_limit(
    explicit: int | None,
    environment_name: str,
    default: int,
) -> int:
    value = explicit
    if value is None:
        try:
            value = int(os.getenv(environment_name, str(default)))
        except ValueError:
            value = default
    return max(1, min(10_000_000, value))


def _bounded_max_attempts(explicit: int | None) -> int:
    value = explicit
    if value is None:
        try:
            value = int(os.getenv("MOUCHEN_ANALYSIS_MAX_ATTEMPTS", "5"))
        except ValueError:
            value = 5
    return max(1, min(20, int(value)))
