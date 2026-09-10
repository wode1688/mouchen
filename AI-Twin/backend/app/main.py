from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
from ipaddress import ip_address
import os
import secrets
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .abuse_controls import (
    RequestBodyLimitMiddleware,
    account_step_up_attempts,
    authenticated_requests,
    authentication_hash_work,
    client_source_ip,
    commercial_proxy_configuration_ready,
    compact_json_bytes,
    event_rate,
    login_attempts,
    registration_attempts,
)

from .api.schemas import (
    AccountDeleteRequest,
    AccountExportRequest,
    AccountPasswordChangeRequest,
    AccountPreferencesUpdate,
    AccountPreferencesView,
    AdviceAttentionClaimRequest,
    AdviceAttentionCompleteRequest,
    AdviceAttentionFailRequest,
    AdvicePreferenceUpdate,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthSessionView,
    AuthTokenResponse,
    CharterUpdate,
    DemoSeedResponse,
    EventIngestResponse,
    ModelAnalyzeRequest,
    ModelAnalyzeResponse,
)
from .auth import AuthPrincipal, utc_now as auth_utc_now
from .account_export_admission import (
    AccountExportAdmission,
    AccountExportAdmissionUnavailable,
    AccountExportBusy,
)
from .domain.preferences import (
    DIRECTION_MODES,
    event_type_allowed,
    event_type_catalog_payload,
    frequency_policy_payload,
    is_internal_composite_event,
    is_manual_problem_event,
)
from .advice_refiner import ProactiveAdviceRefiner
from .advice_localization import AdviceLocalizationConsumer
from .analysis_queue import (
    AnalysisDrainResult,
    AnalysisQueueConsumer,
    AnalysisQueueProcessor,
    release_analysis_slot,
    try_acquire_analysis_slot,
)
from .context_retrieval import build_user_question_context
from .config import SecretConfigurationError, secret_value
from .local_mode import rules_only
from .relay_import import RelayEnvelope, import_request
from .domain.models import (
    AdviceLevel,
    ContextSnapshot,
    Event,
    EventCreate,
    FeedbackCreate,
    Goal,
    GoalCreate,
    OutcomeCreate,
    utc_now,
)
from .model_gateway import (
    ModelGateway,
    ModelUnavailable,
    choose_route,
    preferred_openai_provider,
)
from .privacy import consent_allows_restricted_raw
from .local_stt import local_stt_runtime_ready, router as local_stt_router
from .localization import (
    generated_values_match_locale,
    is_english,
    localized_model_locale_mismatch,
    model_output_language_instruction,
)
from .review_engine import GoalReviewEngine, ReviewTickResult
from .service import ProactiveService
from .storage import (
    AccountChangedConcurrently,
    AccountDeletionIncomplete,
    AccountExportTooLarge,
    AccountNotFound,
    AccountPasswordInvalid,
    AttentionConflict,
    CharterQuotaExceeded,
    DeviceQuotaExceeded,
    EventQuotaExceeded,
    FeedbackQuotaExceeded,
    GoalQuotaExceeded,
    PreferenceRevisionConflict,
    Repository,
)


repo = Repository()
service = ProactiveService(repo)


def _reserve_global_model_call(**kwargs):
    # Resolve ``repo`` at call time so tests and safe database swaps cannot
    # leave the gateway charging an obsolete tenant database.
    return repo.reserve_global_direct_model_call(**kwargs)


models = ModelGateway(global_budget_reserver=_reserve_global_model_call)
analysis_consumer: AnalysisQueueConsumer | None = None
advice_localization_consumer: AdviceLocalizationConsumer | None = None
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global advice_localization_consumer, analysis_consumer
    # These guards are intentionally process-local. Reset once per application
    # lifecycle (not per request); production has one lifespan, while isolated
    # test/application instances cannot inherit another instance's counters.
    login_attempts.reset()
    registration_attempts.reset()
    account_step_up_attempts.reset()
    event_rate.reset()
    authenticated_requests.reset()
    authentication_hash_work.reset()
    if not rules_only() and _environment_flag("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", default=True):
        analysis_consumer = AnalysisQueueConsumer(
            repo,
            service,
            models,
            batch_size=_bounded_environment_int(
                "MOUCHEN_ANALYSIS_BACKGROUND_BATCH_SIZE", 1, 1, 10
            ),
        )
        analysis_consumer.start()
        advice_localization_consumer = AdviceLocalizationConsumer(
            repo,
            models,
            batch_size=_bounded_environment_int(
                "MOUCHEN_ADVICE_LOCALIZATION_BACKGROUND_BATCH_SIZE", 1, 1, 10
            ),
        )
        advice_localization_consumer.start()
    try:
        yield
    finally:
        if advice_localization_consumer is not None:
            await advice_localization_consumer.stop()
            advice_localization_consumer = None
        if analysis_consumer is not None:
            await analysis_consumer.stop()
            analysis_consumer = None
        repo.close()


app = FastAPI(title="My AI Twin Private Alpha", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestBodyLimitMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(local_stt_router)


@app.middleware("http")
async def browser_security_headers(request: Request, call_next):
    """Keep the account PWA same-origin and prevent browser capability creep."""

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "worker-src 'self'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _configured_api_token() -> str | None:
    return secret_value("MOUCHEN_API_TOKEN") or secret_value(
        "MOUCHEN_API_BEARER_TOKEN"
    )


def _legacy_auth_enabled() -> bool:
    """Require an explicit, reversible migration switch for the static owner token."""

    return _environment_flag("MOUCHEN_LEGACY_AUTH_ENABLED", default=False)


_PUBLIC_AUTH_PATHS = frozenset({"/v1/auth/register", "/v1/auth/login"})


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, supplied = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not supplied:
        return None
    return supplied


@app.middleware("http")
async def optional_api_bearer(request: Request, call_next):
    """Authenticate once and attach an immutable server-derived principal."""

    is_api_path = request.url.path == "/v1" or request.url.path.startswith("/v1/")
    if not is_api_path or request.method == "OPTIONS":
        return await call_next(request)
    if not commercial_proxy_configuration_ready():
        return JSONResponse(
            status_code=503,
            content={"detail": "commercial proxy configuration is unavailable"},
        )
    if repo.auth_migration_blocked:
        return JSONResponse(
            status_code=503,
            content={"detail": "authentication migration requires operator action"},
        )
    if request.url.path in _PUBLIC_AUTH_PATHS:
        return await call_next(request)
    legacy_enabled = _legacy_auth_enabled()
    if legacy_enabled:
        try:
            legacy_token = _configured_api_token()
        except SecretConfigurationError:
            return JSONResponse(
                status_code=503,
                content={"detail": "server authentication is unavailable"},
            )
    else:
        legacy_token = None
    supplied_token = _bearer_token(request)
    principal = repo.authenticate_access_token(supplied_token) if supplied_token else None
    if principal is None and legacy_token and supplied_token:
        configured_user = os.getenv("MOUCHEN_SINGLE_USER_ID", "").strip()
        if configured_user and secrets.compare_digest(supplied_token, legacy_token):
            principal = AuthPrincipal(
                configured_user, None, "legacy-single-user",
                ("mouchen:read", "mouchen:write"), None, None, "legacy",
            )
        elif (
            request.client
            and request.client.host == "testclient"
            and secrets.compare_digest(supplied_token, legacy_token)
        ):
            test_user = request.headers.get("X-User-Id", "").strip() or "demo-user"
            principal = AuthPrincipal(
                test_user, None, "legacy-test-client",
                ("mouchen:read", "mouchen:write"), None, None, "legacy",
            )
    allow_unauthenticated = os.getenv(
        "MOUCHEN_ALLOW_UNAUTHENTICATED_PRIVATE_ALPHA", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if principal is None and legacy_enabled and not legacy_token and (
        allow_unauthenticated or _is_direct_loopback(request)
    ):
        # Legacy tokenless development is explicit and restricted to the one
        # configured tenant. Production never falls back to demo-user.
        configured_user = os.getenv("MOUCHEN_SINGLE_USER_ID", "").strip()
        if configured_user:
            principal = AuthPrincipal(
                configured_user, None, "legacy-single-user",
                ("mouchen:read", "mouchen:write"), None, None, "legacy",
            )
        elif request.client and request.client.host == "testclient" and not supplied_token:
            test_user = request.headers.get("X-User-Id", "").strip() or "demo-user"
            principal = AuthPrincipal(
                test_user, None, "legacy-test-client",
                ("mouchen:read", "mouchen:write"), None, None, "legacy",
            )
    if principal is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied_user = request.headers.get("X-User-Id", "").strip()
    if (
        principal.auth_kind == "legacy"
        and os.getenv("MOUCHEN_SINGLE_USER_ID", "").strip()
        and not supplied_user
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "X-User-Id is required for legacy single-user access"},
        )
    if supplied_user and not secrets.compare_digest(supplied_user, principal.user_id):
            return JSONResponse(
                status_code=403,
                content={"detail": "X-User-Id does not match authenticated user"},
            )
    request.state.auth_principal = principal
    admission = authenticated_requests.reserve(principal.user_id)
    if not admission.allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "authenticated request rate exceeded"},
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    return await call_next(request)


def _is_direct_loopback(request: Request) -> bool:
    if request.headers.get("forwarded") or request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def auth_principal(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "auth_principal", None)
    if principal is None:
        raise HTTPException(401, "authentication required")
    return principal


def user_id(principal: Annotated[AuthPrincipal, Depends(auth_principal)]) -> str:
    return principal.user_id


def _semantic_fast_lane_timeout() -> float:
    try:
        configured = float(os.getenv("MOUCHEN_SEMANTIC_FAST_LANE_TIMEOUT_SECONDS", "8"))
    except ValueError:
        configured = 8.0
    return max(1.0, min(30.0, configured))


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _enforce_goal_limits(
    body: GoalCreate,
    uid: str,
    *,
    goal_id: UUID | None = None,
) -> None:
    target_limit = _bounded_environment_int(
        "MOUCHEN_MAX_GOAL_TARGET_BYTES",
        16 * 1024,
        1024,
        256 * 1024,
    )
    if compact_json_bytes(body.target) > target_limit:
        raise HTTPException(413, "goal target is too large")
    # The count check happens atomically with insertion in Repository. Keeping
    # only payload validation here avoids a concurrent check/insert race.


def _goal_count_limit() -> int:
    return _bounded_environment_int(
        "MOUCHEN_MAX_GOALS_PER_USER",
        200,
        1,
        10_000,
    )


def _auth_expiry():
    days = _bounded_environment_int("MOUCHEN_AUTH_TOKEN_TTL_DAYS", 90, 1, 365)
    return auth_utc_now() + timedelta(days=days)


def _registration_code_authorization(
    body: AuthRegisterRequest,
) -> tuple[str, str] | None:
    mode = os.getenv("MOUCHEN_REGISTRATION_MODE", "closed").strip().casefold()
    if mode == "open":
        # Public self-service signup is deliberately a second, independent
        # switch.  A stray REGISTRATION_MODE=open must never turn an invite
        # beta into an unbounded account-creation endpoint.
        if not _environment_flag("MOUCHEN_ALLOW_OPEN_REGISTRATION"):
            raise HTTPException(503, "registration unavailable")
        return None
    if mode != "closed":
        raise HTTPException(503, "registration unavailable")
    try:
        configured = secret_value("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE")
    except SecretConfigurationError as exc:
        raise HTTPException(503, "registration unavailable") from exc
    supplied = (body.registration_code or "").strip()
    if not supplied:
        raise HTTPException(403, "registration unavailable")
    code_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    if configured and secrets.compare_digest(configured, supplied):
        return code_hash, "bootstrap"
    if repo.registration_invite_available(code_hash):
        return code_hash, "invite"
    raise HTTPException(403, "registration unavailable")


def _auth_response(issued) -> AuthTokenResponse:
    principal = issued.principal
    assert principal.expires_at is not None
    return AuthTokenResponse(
        access_token=issued.access_token,
        expires_at=principal.expires_at,
        session=AuthSessionView(
            user_id=principal.user_id,
            username=principal.username,
            device_id=principal.device_id,
            scopes=list(principal.scopes),
            expires_at=principal.expires_at,
            locale=repo.user_locale(principal.user_id),
        ),
    )


def _raw_cloud_approved(request_approved: bool, event: Event | None = None) -> bool:
    if not request_approved or not _environment_flag("MOUCHEN_ALLOW_RAW_CLOUD"):
        return False
    if event is not None and event.sensitivity.value == "restricted":
        return consent_allows_restricted_raw(event.consent_scope)
    return True


def _automatic_analysis_preference_block(event: Event) -> str | None:
    """Global-scope preference gate at ingest time.

    Only the global scope can be judged here (no goal is chosen yet); the
    goal-level filter runs again in the deterministic candidate builder and in
    discovery. Manual problems bypass event filtering, but never the global pause.
    """

    effective = repo.effective_preference(event.user_id, None)
    if effective.paused:
        return "preference_paused"
    if is_manual_problem_event(event):
        return None
    if is_internal_composite_event(event):
        return None
    if not event_type_allowed(effective.event_types, event.type):
        return "preference_event_type_disabled"
    return None


def _wake_analysis_consumer() -> None:
    if analysis_consumer is not None:
        analysis_consumer.wake()


def _wake_advice_localization_consumer() -> None:
    if advice_localization_consumer is not None:
        advice_localization_consumer.wake()


def _advice_api_payload(
    uid: str,
    advice,
    locale: str,
    *,
    display_envelope: dict | None = None,
) -> dict:
    payload = advice.model_dump(mode="json")
    payload.update(
        display_envelope
        if display_envelope is not None
        else repo.advice_display_envelope(uid, advice, locale)
    )
    return payload


def _event_is_live_for_automatic_analysis(event: Event) -> bool:
    """Only current observations may enter the automatic paid-model path."""

    if bool(event.facts.get("historical_backfill")):
        return False
    occurred_at = event.occurred_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=utc_now().tzinfo)
    max_age_seconds = _bounded_environment_int(
        "MOUCHEN_LIVE_ANALYSIS_MAX_EVENT_AGE_SECONDS",
        900,
        60,
        86_400,
    )
    return (utc_now() - occurred_at).total_seconds() <= max_age_seconds


def _analysis_job_is_waiting(user_id: str, event_id: UUID) -> bool:
    job = repo.get_analysis_job(user_id, event_id)
    return job is not None and job.status in {"pending", "retry", "running"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    try:
        model_provider = preferred_openai_provider()
    except ModelUnavailable:
        model_provider = "invalid"
    return {
        "status": "ok",
        "version": app.version,
        "mode": "private-alpha",
        "model_provider": model_provider,
        "runtime_contract": "proactive-analysis-v2",
    }


@app.get("/ready")
def ready():
    try:
        database_ready = repo.ready()
    except Exception:
        database_ready = False
    try:
        account_auth_ready = (
            database_ready
            and not repo.auth_migration_blocked
            and repo.credentialed_user_count() > 0
        )
    except Exception:
        account_auth_ready = False
    legacy_enabled = _legacy_auth_enabled()
    token_ready = False
    token_readable = True
    if legacy_enabled:
        try:
            token_ready = bool(_configured_api_token())
        except SecretConfigurationError:
            token_readable = False
    token_required = legacy_enabled and _environment_flag(
        "MOUCHEN_REQUIRE_API_TOKEN"
    )
    legacy_user_ready = bool(os.getenv("MOUCHEN_SINGLE_USER_ID", "").strip())
    legacy_ready = legacy_enabled and legacy_user_ready and token_readable and (
        token_ready or not token_required
    )
    bootstrap_ready = False
    if (
        database_ready
        and not repo.auth_migration_blocked
        and not account_auth_ready
        and not legacy_enabled
        and os.getenv("MOUCHEN_REGISTRATION_MODE", "closed").strip().casefold()
        == "closed"
    ):
        try:
            bootstrap_ready = (
                repo.total_user_count() == 0
                and bool(secret_value("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE"))
            )
        except Exception:
            bootstrap_ready = False
    proxy_ready = commercial_proxy_configuration_ready()
    authentication_ready = not repo.auth_migration_blocked and proxy_ready and (
        account_auth_ready or legacy_ready or bootstrap_ready
    )
    stt_enabled = _environment_flag("MOUCHEN_LOCAL_STT_ENABLED")
    stt_ready = local_stt_runtime_ready() if stt_enabled else True
    if account_auth_ready or legacy_ready:
        authentication_status = "ready"
    elif bootstrap_ready:
        authentication_status = "bootstrap-required"
    elif authentication_ready:
        authentication_status = "not-required"
    else:
        authentication_status = "unavailable"
    if not database_ready or not authentication_ready or not stt_ready:
        unavailable = {
            "status": "unavailable",
            "database": "ready" if database_ready else "unavailable",
            "authentication": authentication_status,
        }
        if stt_enabled:
            unavailable["speech_to_text"] = "ready" if stt_ready else "unavailable"
        return JSONResponse(
            status_code=503,
            content=unavailable,
        )
    ready_status = {
        "status": "ready",
        "database": "ready",
        "authentication": authentication_status,
    }
    if stt_enabled:
        ready_status["speech_to_text"] = "ready"
    return ready_status


@app.post("/v1/auth/register", response_model=AuthTokenResponse)
def register_account(body: AuthRegisterRequest, request: Request):
    rate = registration_attempts.reserve(client_source_ip(request))
    if not rate.allowed:
        raise HTTPException(
            429,
            "too many registration attempts",
            headers={"Retry-After": str(rate.retry_after)},
        )
    registration_authorization = _registration_code_authorization(body)
    registration_code_hash = (
        registration_authorization[0] if registration_authorization else None
    )
    registration_code_kind = (
        registration_authorization[1] if registration_authorization else None
    )
    admission = authentication_hash_work.acquire()
    if not admission.allowed:
        raise HTTPException(
            429,
            "authentication work is busy",
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    try:
        try:
            issued = repo.register_account(
                username=body.username,
                password=body.password,
                device_id=body.device_id,
                device_name=body.device_name,
                expires_at=_auth_expiry(),
                registration_code_hash=registration_code_hash,
                registration_code_kind=registration_code_kind,
                locale=body.locale,
            )
        finally:
            authentication_hash_work.release()
    except ValueError as exc:
        raise HTTPException(409, "registration unavailable") from exc
    return _auth_response(issued)


@app.post("/v1/auth/login", response_model=AuthTokenResponse)
def login_account(body: AuthLoginRequest, request: Request):
    # Forwarding headers are ignored unless the immediate peer is in the
    # operator-configured trusted proxy CIDRs. Independent account and source
    # windows prevent bypass by rotating only one side of the pair.
    client_key = client_source_ip(request)
    rate = login_attempts.reserve(body.username, client_key)
    if not rate.allowed:
        raise HTTPException(
            429,
            "too many login attempts",
            headers={"Retry-After": str(rate.retry_after)},
        )
    admission = authentication_hash_work.acquire()
    if not admission.allowed:
        raise HTTPException(
            429,
            "authentication work is busy",
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    try:
        try:
            issued = repo.login_account(
                username=body.username,
                password=body.password,
                device_id=body.device_id,
                device_name=body.device_name,
                client_key=client_key,
                expires_at=_auth_expiry(),
            )
        finally:
            authentication_hash_work.release()
    except ValueError as exc:
        raise HTTPException(401, "invalid credentials") from exc
    if issued is None:
        raise HTTPException(401, "invalid credentials")
    return _auth_response(issued)


@app.get("/v1/session", response_model=AuthSessionView)
def get_session(principal: Annotated[AuthPrincipal, Depends(auth_principal)]):
    return AuthSessionView(
        user_id=principal.user_id,
        username=principal.username,
        device_id=principal.device_id,
        scopes=list(principal.scopes),
        expires_at=principal.expires_at,
        locale=repo.user_locale(principal.user_id),
    )


@app.get("/v1/account/preferences", response_model=AccountPreferencesView)
def get_account_preferences(
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    try:
        return AccountPreferencesView.model_validate(
            repo.account_preferences(principal.user_id)
        )
    except AccountNotFound as exc:
        raise HTTPException(404, "account not found") from exc


@app.put("/v1/account/preferences", response_model=AccountPreferencesView)
def update_account_preferences(
    body: AccountPreferencesUpdate,
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    if principal.auth_kind != "session" or principal.token_id is None:
        raise HTTPException(403, "account session required")
    try:
        saved = repo.set_account_locale(principal.user_id, body.locale)
        if saved["locale"] == "en-US":
            repo.enqueue_advice_localizations(principal.user_id, "en-US")
            _wake_advice_localization_consumer()
        return AccountPreferencesView.model_validate(saved)
    except AccountNotFound as exc:
        raise HTTPException(404, "account not found") from exc


@app.post("/v1/auth/logout")
def logout(principal: Annotated[AuthPrincipal, Depends(auth_principal)]):
    repo.revoke_auth_token(principal.user_id, principal.token_id)
    return {"status": "logged_out"}


def _account_session_principal(
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
) -> AuthPrincipal:
    if principal.auth_kind != "session" or principal.token_id is None:
        raise HTTPException(403, "account session required")
    return principal


def _reserve_account_step_up(principal: AuthPrincipal, request: Request) -> str:
    source = client_source_ip(request)
    decision = account_step_up_attempts.reserve(principal.user_id, source)
    if not decision.allowed:
        raise HTTPException(
            429,
            "account verification is temporarily limited",
            headers={"Retry-After": str(max(1, decision.retry_after))},
        )
    return source


class _AccountExportResponse(Response):
    """Include client transfer in the durable lease deadline and cleanup."""

    media_type = "application/json"

    def __init__(self, prepared, lease) -> None:
        super().__init__(
            content=b"",
            media_type=self.media_type,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Content-Disposition": 'attachment; filename="mouchen-account-export.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )
        self.headers["Content-Length"] = str(prepared.bytes_written)
        self.prepared = prepared
        self.lease = lease

    async def __call__(self, scope, receive, send) -> None:
        remaining = max(
            0.001,
            (self.lease.expires_at - auth_utc_now()).total_seconds(),
        )
        try:
            await send(
                {"type": "http.response.start", "status": self.status_code,
                 "headers": self.raw_headers}
            )
            async with asyncio.timeout(remaining):
                with self.prepared.path.open("rb") as handle:
                    while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                        await send(
                            {"type": "http.response.body", "body": chunk,
                             "more_body": True}
                        )
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
        finally:
            self.prepared.cleanup()
            try:
                await asyncio.to_thread(self.lease.release)
            except AccountExportAdmissionUnavailable:
                pass


@app.post("/v1/account/export")
def export_account(
    request: Request,
    body: AccountExportRequest,
    principal: Annotated[AuthPrincipal, Depends(_account_session_principal)],
):
    step_up_source = _reserve_account_step_up(principal, request)
    hash_admission = authentication_hash_work.acquire()
    if not hash_admission.allowed:
        raise HTTPException(
            429,
            "authentication work is busy",
            headers={"Retry-After": str(max(1, hash_admission.retry_after))},
        )
    try:
        try:
            repo.verify_account_password(principal.user_id, body.current_password)
        finally:
            authentication_hash_work.release()
    except AccountPasswordInvalid as exc:
        raise HTTPException(403, "current password is invalid") from exc
    account_step_up_attempts.succeeded(principal.user_id, step_up_source)

    export_gate = AccountExportAdmission(repo.path)
    try:
        lease = export_gate.acquire(principal.user_id)
    except AccountExportBusy as exc:
        raise HTTPException(
            429,
            "account export is temporarily limited",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except AccountExportAdmissionUnavailable as exc:
        raise HTTPException(503, "account export is temporarily unavailable") from exc
    try:
        prepared = repo.prepare_account_export_file(principal.user_id)
    except AccountNotFound as exc:
        lease.release()
        raise HTTPException(401, "account no longer exists") from exc
    except AccountExportTooLarge as exc:
        lease.release()
        raise HTTPException(413, "account export exceeds the configured limit") from exc
    except BaseException:
        try:
            lease.release()
        except AccountExportAdmissionUnavailable:
            pass
        raise

    return _AccountExportResponse(prepared, lease)


@app.post("/v1/account/password", response_model=AuthTokenResponse)
def change_account_password(
    request: Request,
    body: AccountPasswordChangeRequest,
    principal: Annotated[AuthPrincipal, Depends(_account_session_principal)],
):
    step_up_source = _reserve_account_step_up(principal, request)
    admission = authentication_hash_work.acquire()
    if not admission.allowed:
        raise HTTPException(
            429,
            "authentication work is busy",
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    try:
        try:
            issued = repo.change_account_password(
                user_id=principal.user_id,
                token_id=principal.token_id,
                current_password=body.current_password,
                new_password=body.new_password,
                device_id=principal.device_id,
                expires_at=_auth_expiry(),
            )
        finally:
            authentication_hash_work.release()
    except AccountPasswordInvalid as exc:
        raise HTTPException(403, "current password is invalid") from exc
    except AccountChangedConcurrently as exc:
        raise HTTPException(409, "account changed; retry with a current session") from exc
    account_step_up_attempts.succeeded(principal.user_id, step_up_source)
    return _auth_response(issued)


@app.delete("/v1/account")
def delete_account(
    request: Request,
    body: AccountDeleteRequest,
    principal: Annotated[AuthPrincipal, Depends(_account_session_principal)],
):
    step_up_source = _reserve_account_step_up(principal, request)
    admission = authentication_hash_work.acquire()
    if not admission.allowed:
        raise HTTPException(
            429,
            "authentication work is busy",
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    try:
        try:
            repo.delete_account(
                user_id=principal.user_id,
                current_password=body.current_password,
            )
        finally:
            authentication_hash_work.release()
    except AccountPasswordInvalid as exc:
        raise HTTPException(403, "current password is invalid") from exc
    except AccountChangedConcurrently as exc:
        raise HTTPException(409, "account changed; retry with a current session") from exc
    except AccountDeletionIncomplete as exc:
        raise HTTPException(500, "account deletion could not be verified") from exc
    account_step_up_attempts.succeeded(principal.user_id, step_up_source)
    return {"status": "deleted"}


@app.get("/v1/capabilities")
def capabilities():
    return {
        "privateAlpha": [
            "calendar", "tasks", "email", "notifications", "usage_stats", "location",
            "health_connect", "accessibility_visible_text", "microphone_visible_session",
            "media_projection_per_session", "custom_ime", "user_export_import", "confirmed_actions",
        ],
        "hardBoundaries": [
            "no_sandbox_bypass", "no_secure_window_capture", "no_password_capture",
            "no_covert_recording", "no_unconfirmed_external_mutation", "no_wechat_private_db_access",
        ],
        "modelRoutes": {
            "onDevice": "capability probe only; no bundled 7B runtime",
            "privateNode": "authenticated Ollama",
            "cloud": "audited minimized slices by default; raw requires server and request approval",
        },
    }


@app.post("/v1/relay/import")
def relay_import(
    request: Request,
    body: RelayEnvelope,
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    if not rules_only() or not _is_direct_loopback(request):
        raise HTTPException(403, "relay import requires a local rules profile")
    if principal.auth_kind != "session":
        raise HTTPException(403, "relay import requires account session authentication")
    if body.operation == "event.create":
        admission = event_rate.reserve(f"{id(repo)}:{principal.user_id}")
        if not admission.allowed:
            raise HTTPException(429, "event ingest rate exceeded",
                                headers={"Retry-After": str(max(1, admission.retry_after))})
    return import_request(
        repo, service, principal.user_id, body,
        enforce_goal_limits=_enforce_goal_limits, goal_count_limit=_goal_count_limit(),
        max_facts_bytes=_bounded_environment_int("MOUCHEN_MAX_EVENT_FACTS_BYTES", 64 * 1024, 1024, 4 * 1024 * 1024),
        max_feedback=_bounded_environment_int("MOUCHEN_MAX_FEEDBACK_PER_USER", 10_000, 100, 1_000_000),
        max_guidance=_bounded_environment_int("MOUCHEN_MAX_GUIDANCE_PER_ADVICE", 20, 1, 1000),
        event_is_live=_event_is_live_for_automatic_analysis,
        analysis_preference_block=_automatic_analysis_preference_block,
    )


@app.post("/v1/goals", response_model=Goal)
def create_goal(body: GoalCreate, uid: str = Depends(user_id)):
    _enforce_goal_limits(body, uid)
    try:
        return repo.insert_goal(
            Goal(user_id=uid, **body.model_dump()),
            max_goals=_goal_count_limit(),
        )
    except GoalQuotaExceeded as exc:
        raise HTTPException(507, "goal storage quota exceeded") from exc


@app.put("/v1/goals/{goal_id}", response_model=Goal)
def sync_goal(
    goal_id: UUID,
    body: GoalCreate,
    uid: str = Depends(user_id),
):
    _enforce_goal_limits(body, uid, goal_id=goal_id)
    try:
        return repo.insert_goal(
            Goal(id=goal_id, user_id=uid, **body.model_dump()),
            max_goals=_goal_count_limit(),
        )
    except GoalQuotaExceeded as exc:
        raise HTTPException(507, "goal storage quota exceeded") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/goals", response_model=list[Goal])
def list_goals(uid: str = Depends(user_id)):
    return repo.list_goals(uid)


@app.put("/v1/charter/{domain}")
def set_charter(
    domain: str,
    body: CharterUpdate,
    uid: str = Depends(user_id),
):
    if not 1 <= len(domain) <= 80 or any(ord(char) < 32 for char in domain):
        raise HTTPException(422, "invalid charter domain")
    try:
        repo.set_charter(
            uid,
            domain,
            body.max_level,
            body.redline_authorized,
            max_charters=_bounded_environment_int(
                "MOUCHEN_MAX_CHARTERS_PER_USER", 50, 1, 1000
            ),
        )
    except CharterQuotaExceeded as exc:
        raise HTTPException(507, "charter storage quota exceeded") from exc
    return {"domain": domain, **body.model_dump(mode="json")}


@app.post("/v1/events", response_model=EventIngestResponse)
async def ingest_event(
    body: EventCreate,
    uid: str = Depends(user_id),
    proactive_cloud_approved: Annotated[
        bool, Header(alias="X-Proactive-Cloud-Approved")
    ] = False,
    semantic_fast_lane: Annotated[
        bool, Header(alias="X-Semantic-Fast-Lane")
    ] = False,
    raw_cloud_requested: Annotated[
        bool, Header(alias="X-Raw-Cloud-Approved")
    ] = False,
    sleeping: bool = Query(False),
    driving: bool = Query(False),
    in_meeting: bool = Query(False),
    quiet_hours: bool = Query(False),
):
    max_facts_bytes = _bounded_environment_int(
        "MOUCHEN_MAX_EVENT_FACTS_BYTES",
        64 * 1024,
        1024,
        4 * 1024 * 1024,
    )
    if compact_json_bytes(body.facts) > max_facts_bytes:
        raise HTTPException(413, "event facts are too large")
    rate = event_rate.reserve(f"{id(repo)}:{uid}")
    if not rate.allowed:
        raise HTTPException(
            429,
            "event ingest rate exceeded",
            headers={"Retry-After": str(rate.retry_after)},
        )
    event = Event(user_id=uid, **body.model_dump())
    try:
        return await _ingest_event_unmetered(
            event,
            uid=uid,
            proactive_cloud_approved=proactive_cloud_approved,
            semantic_fast_lane=semantic_fast_lane,
            raw_cloud_requested=raw_cloud_requested,
            sleeping=sleeping,
            driving=driving,
            in_meeting=in_meeting,
            quiet_hours=quiet_hours,
        )
    except EventQuotaExceeded as exc:
        raise HTTPException(507, "event storage quota exceeded") from exc


async def _ingest_event_unmetered(
    event: Event,
    *,
    uid: str,
    proactive_cloud_approved: bool,
    semantic_fast_lane: bool,
    raw_cloud_requested: bool,
    sleeping: bool,
    driving: bool,
    in_meeting: bool,
    quiet_hours: bool,
):
    if rules_only():
        proactive_cloud_approved = False
    if not _event_is_live_for_automatic_analysis(event):
        repo.insert_event(event)
        return EventIngestResponse(event=event, evaluation=None, queued=False)
    if _automatic_analysis_preference_block(event) is not None:
        # The event is still recorded — disabling an event type or pausing
        # proactive advice never stops collection — but no deterministic
        # candidate and no new automatic analysis job may be created from it.
        repo.insert_event(event)
        return EventIngestResponse(event=event, evaluation=None, queued=False)
    raw_cloud_approved = _raw_cloud_approved(raw_cloud_requested, event)
    context = ContextSnapshot(
        sleeping=sleeping,
        driving=driving,
        in_meeting=in_meeting,
        quiet_hours=quiet_hours,
    )
    evaluation = service.ingest(event, context)
    if proactive_cloud_approved:
        if evaluation is not None:
            original_evaluation = evaluation
            deterministic_goal_id = (
                original_evaluation.advice.goal_id
                if original_evaluation.advice is not None
                else None
            )
            if semantic_fast_lane:
                if try_acquire_analysis_slot():
                    try:
                        evaluation = await asyncio.wait_for(
                            ProactiveAdviceRefiner(repo, models).refine(
                                evaluation,
                                uid,
                                allow_raw_cloud=raw_cloud_approved,
                            ),
                            timeout=_semantic_fast_lane_timeout(),
                        )
                    except TimeoutError:
                        evaluation = service.discard_unreviewed(original_evaluation)
                    finally:
                        release_analysis_slot()
                else:
                    evaluation = service.discard_unreviewed(evaluation)
            else:
                # Deterministic L1/L2 advice is already useful and cheap. A
                # warning remains invisible until its mandatory model review;
                # queue that event instead of blocking a phone backlog upload.
                evaluation = service.discard_unreviewed(evaluation)
            needs_async_review = (
                evaluation.decision == "hold"
                and evaluation.advice is None
                and any(
                    reason in evaluation.reason_codes
                    for reason in {
                        "L3_SECOND_OPINION_REQUIRED",
                        "AI_REFINEMENT_UNAVAILABLE",
                        "MODEL_OUTPUT_LOCALE_MISMATCH",
                    }
                )
            )
            if needs_async_review:
                repo.enqueue_analysis_job(
                    uid,
                    event.event_id,
                    job_kind="refine_deterministic",
                    goal_id=deterministic_goal_id,
                    cloud_approved=True,
                    raw_cloud_approved=raw_cloud_approved,
                )
                _wake_analysis_consumer()
        else:
            queued = repo.enqueue_analysis_job(
                uid,
                event.event_id,
                job_kind="discover",
                cloud_approved=True,
                raw_cloud_approved=raw_cloud_approved,
            )
            if queued and semantic_fast_lane:
                if try_acquire_analysis_slot():
                    try:
                        drained = await asyncio.wait_for(
                            AnalysisQueueProcessor(repo, service, models).process_event(
                                uid,
                                event.event_id,
                                context,
                            ),
                            timeout=_semantic_fast_lane_timeout(),
                        )
                        evaluation = drained.evaluations[0] if drained.evaluations else None
                    except TimeoutError:
                        evaluation = None
                    finally:
                        release_analysis_slot()
            _wake_analysis_consumer()
    else:
        evaluation = service.discard_unreviewed(evaluation)
    return EventIngestResponse(
        event=event,
        evaluation=evaluation,
        queued=_analysis_job_is_waiting(uid, event.event_id),
    )


@app.post("/v1/reviews/run", response_model=ReviewTickResult)
async def run_goal_review(
    uid: str = Depends(user_id),
    proactive_cloud_approved: Annotated[
        bool, Header(alias="X-Proactive-Cloud-Approved")
    ] = False,
    raw_cloud_requested: Annotated[
        bool, Header(alias="X-Raw-Cloud-Approved")
    ] = False,
    sleeping: bool = Query(False),
    driving: bool = Query(False),
    in_meeting: bool = Query(False),
    quiet_hours: bool = Query(False),
):
    """Run one idempotent goal review over the latest evidence window."""

    context = ContextSnapshot(
        sleeping=sleeping,
        driving=driving,
        in_meeting=in_meeting,
        quiet_hours=quiet_hours,
    )
    if proactive_cloud_approved and not try_acquire_analysis_slot():
        raise HTTPException(
            429,
            "semantic analysis is already running",
            headers={"Retry-After": "3"},
        )
    try:
        try:
            return await GoalReviewEngine(repo, service, models).tick(
                uid,
                cloud_approved=proactive_cloud_approved,
                raw_cloud_approved=_raw_cloud_approved(raw_cloud_requested),
                context=context,
            )
        except EventQuotaExceeded as exc:
            raise HTTPException(507, "event storage quota exceeded") from exc
    finally:
        if proactive_cloud_approved:
            release_analysis_slot()


@app.post("/v1/analysis/drain", response_model=AnalysisDrainResult)
async def drain_analysis_jobs(
    uid: str = Depends(user_id),
    proactive_cloud_approved: Annotated[
        bool, Header(alias="X-Proactive-Cloud-Approved")
    ] = False,
    limit: int = Query(5, ge=1, le=10),
    sleeping: bool = Query(False),
    driving: bool = Query(False),
    in_meeting: bool = Query(False),
    quiet_hours: bool = Query(False),
):
    """Safely retry due semantic jobs after an explicit standing cloud approval."""

    if not proactive_cloud_approved:
        raise HTTPException(409, "semantic analysis requires proactive cloud approval")
    if not try_acquire_analysis_slot():
        raise HTTPException(
            429,
            "semantic analysis is already running",
            headers={"Retry-After": "3"},
        )
    try:
        return await AnalysisQueueProcessor(repo, service, models).drain(
            uid,
            ContextSnapshot(
                sleeping=sleeping,
                driving=driving,
                in_meeting=in_meeting,
                quiet_hours=quiet_hours,
            ),
            limit=limit,
        )
    finally:
        release_analysis_slot()


@app.get("/v1/analysis/status")
def analysis_status(
    uid: str = Depends(user_id),
):
    return repo.analysis_status(uid)


@app.post("/v1/analysis/backfill")
def backfill_analysis_jobs(
    uid: str = Depends(user_id),
    proactive_cloud_approved: Annotated[
        bool, Header(alias="X-Proactive-Cloud-Approved")
    ] = False,
    raw_cloud_requested: Annotated[
        bool, Header(alias="X-Raw-Cloud-Approved")
    ] = False,
    dry_run: bool = Query(True),
    limit: int = Query(25, ge=1, le=100),
):
    """Explicitly queue a bounded newest-first historical semantic backfill."""

    if not dry_run and not proactive_cloud_approved:
        raise HTTPException(409, "backfill requires proactive cloud approval")
    candidates = [
        event
        for event in repo.analysis_backfill_candidates(uid, limit=limit)
        if _automatic_analysis_preference_block(event) is None
    ]
    selected_ids = [str(event.event_id) for event in candidates]
    enqueued = 0
    if not dry_run:
        for event in candidates:
            if repo.enqueue_analysis_job(
                uid,
                event.event_id,
                job_kind="discover",
                cloud_approved=True,
                raw_cloud_approved=_raw_cloud_approved(raw_cloud_requested, event),
            ):
                enqueued += 1
        if enqueued:
            _wake_analysis_consumer()
    return {
        "dry_run": dry_run,
        "hard_limit": 100,
        "selected": len(selected_ids),
        "enqueued": enqueued,
        "event_ids": selected_ids,
    }


@app.get("/v1/advice")
def list_advice(
    limit: int = Query(100, ge=1, le=500),
    uid: str = Depends(user_id),
):
    locale = repo.user_locale(uid)
    advice_items = repo.list_advice(uid, limit)
    display_envelopes = repo.advice_display_envelopes(uid, advice_items, locale)
    return [
        _advice_api_payload(
            uid,
            advice,
            locale,
            display_envelope=display_envelopes[str(advice.id)],
        )
        for advice in advice_items
    ]


@app.post("/v1/advice-attention/claim")
def claim_advice_attention(
    body: AdviceAttentionClaimRequest,
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    uid = principal.user_id
    if principal.auth_kind == "session" and body.device_id != principal.device_id:
        raise HTTPException(403, "device does not match authenticated session")
    try:
        claim = repo.claim_advice_attention(
            uid,
            body.device_id,
            platform=body.platform,
            app_version=body.app_version,
        )
    except DeviceQuotaExceeded as exc:
        raise HTTPException(507, "device storage quota exceeded") from exc
    except AttentionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if claim is None:
        return {"status": "none"}
    return {
        "status": "claimed",
        "claim_token": claim.claim_token,
        "lease_expires_at": claim.lease_expires_at,
        "delivery_number": claim.delivery_number,
        "advice": _advice_api_payload(uid, claim.advice, repo.user_locale(uid)),
    }


@app.post("/v1/advice-attention/{advice_id}/complete")
def complete_advice_attention(
    advice_id: UUID,
    body: AdviceAttentionCompleteRequest,
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    uid = principal.user_id
    if principal.auth_kind == "session" and body.device_id != principal.device_id:
        raise HTTPException(403, "device does not match authenticated session")
    if not repo.get_advice(uid, advice_id):
        raise HTTPException(404, "advice not found")
    try:
        return repo.complete_advice_attention(
            uid,
            advice_id,
            body.device_id,
            body.claim_token,
        )
    except AttentionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/advice-attention/{advice_id}/fail")
def fail_advice_attention(
    advice_id: UUID,
    body: AdviceAttentionFailRequest,
    principal: Annotated[AuthPrincipal, Depends(auth_principal)],
):
    uid = principal.user_id
    if principal.auth_kind == "session" and body.device_id != principal.device_id:
        raise HTTPException(403, "device does not match authenticated session")
    if not repo.get_advice(uid, advice_id):
        raise HTTPException(404, "advice not found")
    try:
        return repo.fail_advice_attention(
            uid,
            advice_id,
            body.device_id,
            body.claim_token,
            reason=body.reason,
        )
    except AttentionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/v1/advice/{advice_id}/feedback")
def feedback(
    advice_id: UUID,
    body: FeedbackCreate,
    uid: str = Depends(user_id),
):
    if not repo.get_advice(uid, advice_id):
        raise HTTPException(404, "advice not found")
    try:
        advice = repo.record_feedback(
            uid,
            advice_id,
            body,
            max_feedback_per_user=_bounded_environment_int(
                "MOUCHEN_MAX_FEEDBACK_PER_USER", 10_000, 100, 1_000_000
            ),
            max_guidance_per_advice=_bounded_environment_int(
                "MOUCHEN_MAX_GUIDANCE_PER_ADVICE", 20, 1, 1000
            ),
        )
    except FeedbackQuotaExceeded as exc:
        raise HTTPException(507, "feedback storage quota exceeded") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "status": "recorded",
        "advice": _advice_api_payload(uid, advice, repo.user_locale(uid)),
    }


@app.get("/v1/advice-preferences")
def get_advice_preferences(
    uid: str = Depends(user_id),
):
    """Everything one settings screen needs in one call: the global scope, all
    goal overrides, each goal's resolved effective view, plus the authoritative
    event catalog / account-language labels / frequency-policy table — clients render
    from this payload and never copy catalogs or numbers."""

    global_pref = repo.global_preference(uid)
    overrides = repo.list_goal_preferences(uid)
    overrides_by_goal = {
        str(item.goal_id): item for item in overrides if item.goal_id is not None
    }
    goals_payload = []
    for goal in repo.current_goals(uid):
        effective = repo.effective_preference(uid, goal.id)
        goals_payload.append(
            {
                "goal_id": str(goal.id),
                "title": goal.title,
                "domain": goal.domain,
                "has_override": str(goal.id) in overrides_by_goal,
                "effective": effective.model_dump(mode="json"),
            }
        )
    locale = repo.user_locale(uid)
    return {
        "global": global_pref.model_dump(mode="json"),
        "goal_overrides": [item.model_dump(mode="json") for item in overrides],
        "goals": goals_payload,
        "event_type_catalog": event_type_catalog_payload(locale),
        "frequency_policies": frequency_policy_payload(locale),
        "direction_modes": list(DIRECTION_MODES),
    }


@app.put("/v1/advice-preferences/global")
def put_global_advice_preference(
    body: AdvicePreferenceUpdate,
    uid: str = Depends(user_id),
):
    # The global scope is the concrete baseline every goal inherits from:
    # "inherit" placeholders are goal-scope semantics and rejected here.
    if body.frequency_mode is None:
        raise HTTPException(
            422,
            {"code": "global_frequency_required", "message": "global scope requires a concrete frequency_mode"},
        )
    if body.event_types is None:
        raise HTTPException(
            422,
            {"code": "global_event_types_required", "message": "global scope requires a concrete event_types list"},
        )
    try:
        saved = repo.upsert_advice_preference(
            uid,
            goal_id=None,
            direction=body.direction,
            direction_mode="replace",  # global has no inheritance semantics
            frequency_mode=body.frequency_mode,
            event_types=body.event_types,
            expected_revision=body.revision,
        )
    except PreferenceRevisionConflict as exc:
        raise HTTPException(
            409,
            {
                "code": "preference_revision_conflict",
                "message": str(exc),
                "current": exc.current.model_dump(mode="json"),
            },
        ) from exc
    return {"status": "saved", "preference": saved.model_dump(mode="json")}


@app.put("/v1/advice-preferences/goals/{goal_id}")
def put_goal_advice_preference(
    goal_id: UUID,
    body: AdvicePreferenceUpdate,
    uid: str = Depends(user_id),
):
    if repo.get_goal(uid, goal_id) is None:
        # Another user's goal is indistinguishable from a missing one.
        raise HTTPException(404, {"code": "goal_not_found", "message": "goal not found"})
    try:
        saved = repo.upsert_advice_preference(
            uid,
            goal_id=goal_id,
            direction=body.direction,
            direction_mode=body.direction_mode,
            frequency_mode=body.frequency_mode,
            event_types=body.event_types,
            expected_revision=body.revision,
        )
    except PreferenceRevisionConflict as exc:
        raise HTTPException(
            409,
            {
                "code": "preference_revision_conflict",
                "message": str(exc),
                "current": exc.current.model_dump(mode="json"),
            },
        ) from exc
    effective = repo.effective_preference(uid, goal_id)
    return {
        "status": "saved",
        "preference": saved.model_dump(mode="json"),
        "effective": effective.model_dump(mode="json"),
    }


@app.delete("/v1/advice-preferences/goals/{goal_id}")
def delete_goal_advice_preference(
    goal_id: UUID,
    uid: str = Depends(user_id),
):
    if repo.get_goal(uid, goal_id) is None:
        raise HTTPException(404, {"code": "goal_not_found", "message": "goal not found"})
    removed = repo.delete_goal_preference(uid, goal_id)
    # Deleting an override immediately restores the inherited global view.
    effective = repo.effective_preference(uid, goal_id)
    return {
        "status": "deleted" if removed else "absent",
        "effective": effective.model_dump(mode="json"),
    }


@app.post("/v1/advice/{advice_id}/outcome")
def outcome(
    advice_id: UUID,
    body: OutcomeCreate,
    uid: str = Depends(user_id),
):
    advice = repo.get_advice(uid, advice_id)
    if not advice:
        raise HTTPException(404, "advice not found")
    try:
        repo.record_outcome(uid, advice, body)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "verified"}


@app.get("/v1/trust/{domain}/{level}")
def trust(
    domain: str,
    level: AdviceLevel,
    uid: str = Depends(user_id),
):
    summary = repo.trust_summary(uid, domain, level)
    return {
        "domain": domain,
        "level": level.name,
        "judged": summary.judged,
        "correct": summary.correct,
        "precision_lower_bound": summary.precision_lower_bound,
        "brier": summary.brier,
        "utility": summary.utility,
        "timing": summary.timing,
        "score": summary.score,
        "catastrophic_errors": summary.catastrophic_errors,
    }


@app.post("/v1/model/analyze", response_model=ModelAnalyzeResponse)
async def analyze(
    body: ModelAnalyzeRequest,
    uid: str = Depends(user_id),
    raw_cloud_requested: Annotated[
        bool, Header(alias="X-Raw-Cloud-Approved")
    ] = False,
):
    if rules_only():
        raise HTTPException(409, "this profile uses local rules; model calls are disabled")
    route = choose_route(body.level, body.force_private_7b, body.purpose)
    if route.provider not in {"template", "ollama"} and not body.outbound_approved:
        raise HTTPException(409, "cloud analysis requires outbound_approved=true")
    admission = repo.admit_direct_model_request(uid, route.provider)
    if not admission.allowed:
        raise HTTPException(
            429,
            "direct model request limit exceeded",
            headers={"Retry-After": str(max(1, admission.retry_after))},
        )
    try:
        return await _run_admitted_model_analysis(
            body,
            uid,
            raw_cloud_requested,
            route,
        )
    finally:
        if admission.lease_id is not None:
            repo.release_direct_model_lease(admission.lease_id)


async def _run_admitted_model_analysis(
    body: ModelAnalyzeRequest,
    uid: str,
    raw_cloud_requested: bool,
    route,
) -> ModelAnalyzeResponse:
    locale = repo.user_locale(uid)
    model_context = {**body.redacted_context, "response_locale": locale}
    if body.purpose.strip().casefold() == "user_question":
        model_context = build_user_question_context(
            repo,
            uid,
            body.prompt,
            body.redacted_context,
        )
        model_context["response_locale"] = locale
    budget_reserved = False
    language_instruction = model_output_language_instruction(locale)
    model_prompt = f"{body.prompt}\n\n{language_instruction}"
    unavailable_message = (
        "The model is temporarily unavailable. Proactive analysis has fallen back to structured guidance; please try again later."
        if is_english(locale)
        else "模型暂不可用，主动分析已降级为结构化建议，请稍后重试。"
    )
    configuration_message = (
        "The model is unavailable. The proactive engine has fallen back to structured guidance. Check the model connection settings."
        if is_english(locale)
        else "模型不可用，主动引擎已降级为结构化建议。请检查模型连接配置。"
    )
    if route.provider not in {"template", "ollama"}:
        route = replace(
            route,
            raw_cloud_approved=_raw_cloud_approved(raw_cloud_requested),
        )
        prepare_payload = getattr(models, "prepare_cloud_payload", None)
        if callable(prepare_payload):
            model_prompt, model_context = prepare_payload(
                model_prompt,
                model_context,
                allow_raw=route.raw_cloud_approved,
            )
        try:
            reserve_paid_call = getattr(models, "reserve_paid_call", None)
            if callable(reserve_paid_call):
                budget_reserved = bool(
                    reserve_paid_call(route.provider, uid, body.purpose)
                )
        except ModelUnavailable as exc:
            if exc.reason_code in {
                "global_model_budget_exhausted",
                "user_model_budget_exhausted",
            }:
                retry_after = max(1, int(exc.retry_after_seconds or 1))
                raise HTTPException(
                    429,
                    "global model budget exhausted",
                    headers={"Retry-After": str(retry_after)},
                ) from exc
            return ModelAnalyzeResponse(
                provider="template",
                model="deterministic-v1",
                content=unavailable_message,
                degraded=True,
            )
        repo.audit_cloud_slice(
            uid,
            route.provider,
            route.model,
            body.purpose,
            model_context,
            prompt=model_prompt,
        )
    try:
        if getattr(models, "supports_pre_reserved_budget", False):
            content = await models.generate(
                route,
                model_prompt,
                model_context,
                user_id=uid,
                global_budget_reserved=budget_reserved,
                payload_prepared=route.provider not in {"template", "ollama"},
            )
        else:
            content = await models.generate(
                route,
                model_prompt,
                model_context,
                user_id=uid,
            )
    except ModelUnavailable as exc:
        if exc.reason_code in {
            "global_model_budget_exhausted",
            "user_model_budget_exhausted",
        }:
            retry_after = max(1, int(exc.retry_after_seconds or 1))
            raise HTTPException(
                429,
                "global model budget exhausted",
                headers={"Retry-After": str(retry_after)},
            ) from exc
        return ModelAnalyzeResponse(
            provider="template",
            model="deterministic-v1",
            content=unavailable_message,
            degraded=True,
        )
    except Exception:
        return ModelAnalyzeResponse(
            provider="template",
            model="deterministic-v1",
            content=configuration_message,
            degraded=True,
        )

    if not generated_values_match_locale(locale, content):
        return ModelAnalyzeResponse(
            provider="template",
            model="deterministic-v1",
            content=localized_model_locale_mismatch(locale),
            degraded=True,
        )

    review = None
    if route.second_opinion and body.outbound_approved:
        try:
            review_context = dict(model_context)
            if route.raw_cloud_approved:
                review_context["_raw_cloud_approved"] = True
            review_provider, review_model = models.second_opinion_route()
            prepare_payload = getattr(models, "prepare_cloud_payload", None)
            review_prompt = model_prompt
            if callable(prepare_payload):
                review_prompt, review_context = prepare_payload(
                    review_prompt,
                    review_context,
                    allow_raw=route.raw_cloud_approved,
                )
            reserve_paid_call = getattr(models, "reserve_paid_call", None)
            review_budget_reserved = False
            if callable(reserve_paid_call):
                review_budget_reserved = bool(
                    reserve_paid_call(
                        review_provider,
                        uid,
                        f"{body.purpose}.second_opinion",
                    )
                )
            repo.audit_cloud_slice(
                uid,
                review_provider,
                review_model,
                f"{body.purpose}.second_opinion",
                review_context,
                prompt=review_prompt,
            )
            if getattr(models, "supports_pre_reserved_budget", False):
                review = await models.second_opinion(
                    review_prompt,
                    review_context,
                    user_id=uid,
                    global_budget_reserved=review_budget_reserved,
                    payload_prepared=True,
                )
            else:
                review = await models.second_opinion(review_prompt, review_context)
            if review is not None and not generated_values_match_locale(locale, review):
                review = None
        except Exception:
            # The independent review is advisory. Once the primary model has
            # answered, review routing, auditing, or execution failures must
            # never discard that successful answer.
            review = None
    return ModelAnalyzeResponse(
        provider=route.provider,
        model=route.model,
        content=content,
        second_opinion=review,
    )


@app.post("/v1/demo/seed", response_model=DemoSeedResponse)
def seed_demo(uid: str = Depends(user_id)):
    if not _environment_flag("MOUCHEN_DEMO_SEED_ENABLED"):
        raise HTTPException(404, "not found")
    demo_goal = Goal(
        user_id=uid,
        domain="work",
        title="完成AI替身内测",
        quote="未来两周最重要的事情是完成AI替身可运行内测版",
        target={"weekly_hours": 12},
    )
    _enforce_goal_limits(
        GoalCreate(
            domain=demo_goal.domain,
            title=demo_goal.title,
            quote=demo_goal.quote,
            target=demo_goal.target,
        ),
        uid,
    )
    try:
        goal = repo.insert_goal(demo_goal, max_goals=_goal_count_limit())
    except GoalQuotaExceeded as exc:
        raise HTTPException(507, "goal storage quota exceeded") from exc
    try:
        repo.set_charter(
            uid,
            "work",
            AdviceLevel.L3,
            False,
            max_charters=_bounded_environment_int(
                "MOUCHEN_MAX_CHARTERS_PER_USER", 50, 1, 1000
            ),
        )
    except CharterQuotaExceeded as exc:
        raise HTTPException(507, "charter storage quota exceeded") from exc
    event = Event(
        user_id=uid,
        source="demo.usage",
        type="time.allocation",
        # Keep the offline demo at L2. L3 is intentionally unavailable until
        # its mandatory model review has completed.
        facts={"domain": "work", "goal_id": str(goal.id), "actual_hours": 4, "expected_hours": 12},
        confidence=0.96,
    )
    evaluation = service.ingest(event)
    if evaluation is None:
        raise HTTPException(500, "demo detector did not produce advice")
    return DemoSeedResponse(goal=goal, event=event, evaluation=evaluation)
