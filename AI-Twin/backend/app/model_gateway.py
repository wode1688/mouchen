from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import SecretConfigurationError, secret_value
from .domain.models import AdviceLevel
from .localization import is_english
from .privacy import prepare_cloud_request


class ModelUnavailable(RuntimeError):
    """A provider failure with safe, machine-readable retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "transient",
        reason_code: str = "model_unavailable",
        retryable: bool = True,
        provider: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.reason_code = reason_code
        self.retryable = retryable
        self.provider = provider
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds

    def safe_details(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            "provider": self.provider,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class OpenAICredentialProbe:
    ready: bool
    reason_code: str
    retryable: bool
    status_code: int | None = None
    request_id: str | None = None


_IS_WINDOWS = os.name == "nt"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_OPENAI_API_STYLES = {"responses", "chat_completions"}
_MODEL_PROVIDERS = {"auto", "openai", "codex_cli", "rules"}
_REVIEW_PROVIDERS = {"auto", "claude_code", "anthropic", "openai", "disabled"}
_OPENAI_REQUEST_ID = re.compile(r"(?:req|request)[_-][A-Za-z0-9][A-Za-z0-9._:-]{0,119}")
_OPENAI_QUOTA_MARKERS = {
    "account_deactivated",
    "billing_error",
    "billing_hard_limit_reached",
    "billing_not_active",
    "credits_exhausted",
    "insufficient_quota",
    "usage_limit_reached",
}
_OPENAI_AUTH_MARKERS = {
    "authentication_error",
    "invalid_api_key",
    "invalid_authentication",
}
_CODEX_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please log in",
    "login required",
    "authentication required",
    "unauthorized",
)
_CODEX_MODEL_FAILURE_MARKERS = (
    "unsupported model",
    "unknown model",
    "model_not_found",
    "model is not supported",
    "model is unsupported",
    "model does not exist",
    "model isn't available",
    "model is not available",
)
_CODEX_NETWORK_FAILURE_MARKERS = (
    "network",
    "connection",
    "timed out",
    "timeout",
    "dns",
    "certificate",
    "proxy",
    "temporarily unavailable",
    "tls",
)
_CLAUDE_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please log in",
    "login required",
    "authentication failed",
    "authentication_failed",
    "invalid api key",
    "oauth token has expired",
    "token expired",
    "api error: 401",
    "api error: 403",
    "unauthorized",
)
_CLAUDE_MODEL_FAILURE_MARKERS = (
    "unsupported model",
    "unknown model",
    "model_not_found",
    "model is not supported",
    "model is unsupported",
    "model does not exist",
    "model isn't available",
    "model is not available",
    "invalid model",
)
_CLAUDE_NETWORK_FAILURE_MARKERS = (
    "network",
    "connection",
    "econnrefused",
    "econnreset",
    "enotfound",
    "etimedout",
    "fetch failed",
    "socket hang up",
    "timed out",
    "timeout",
    "dns",
    "certificate",
    "proxy",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "tls",
    "api error: 408",
    "api error: 409",
    "api error: 425",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 504",
    "api error: 529",
)
_CLAUDE_RATE_LIMIT_MARKERS = (
    "api error: 429",
    "hit your limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "usage limit",
)
_OPENAI_SECOND_OPINION_PREFIX = """You are an independent second-opinion critic.
Do not continue or polish the first model's answer. Re-evaluate the quoted evidence,
goal, proposed action, predicted consequence, and alternative from scratch. Return only
the JSON verdict required by the review prompt."""


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _model_max_output_tokens() -> int:
    """Return a hard per-call output ceiling for every model provider."""

    try:
        configured = int(os.getenv("MOUCHEN_MODEL_MAX_OUTPUT_TOKENS", "2048"))
    except ValueError:
        configured = 2048
    return max(128, min(8192, configured))


def _reject_host_cli_for_commercial_tenants(provider: str) -> None:
    """Keep tenant prompts away from host agents which can inspect local files."""

    if not _environment_flag("MOUCHEN_COMMERCIAL_MULTI_USER"):
        return
    raise ModelUnavailable(
        "host model CLI is disabled for commercial multi-user deployments",
        category="configuration",
        reason_code=f"{provider}_disabled_for_multi_user",
        retryable=False,
        provider=(
            "anthropic_via_claude_code" if provider == "claude_code" else provider
        ),
    )


def _openai_api_style() -> str:
    style = os.getenv("OPENAI_API_STYLE", "responses").strip().lower()
    if style not in _OPENAI_API_STYLES:
        raise ModelUnavailable(
            "OPENAI_API_STYLE must be 'responses' or 'chat_completions'",
            category="configuration",
            reason_code="openai_configuration_invalid",
            retryable=False,
            provider="openai",
        )
    return style


def _openai_api_key() -> str:
    try:
        key = secret_value("OPENAI_API_KEY")
    except SecretConfigurationError as exc:
        raise ModelUnavailable(
            "OpenAI credentials are unavailable",
            category="auth",
            reason_code="openai_credentials_unavailable",
            retryable=False,
            provider="openai",
        ) from exc
    if not key:
        raise ModelUnavailable(
            "OpenAI credentials are not configured",
            category="auth",
            reason_code="openai_credentials_missing",
            retryable=False,
            provider="openai",
        )
    return key


def _openai_base_url() -> str:
    """Return a normalized HTTPS API base without disclosing invalid input."""

    raw = os.getenv("OPENAI_BASE_URL", _DEFAULT_OPENAI_BASE_URL)
    if not raw or raw != raw.strip() or "\\" in raw:
        raise ModelUnavailable(
            "OPENAI_BASE_URL must be a valid HTTPS API base URL",
            category="configuration",
            reason_code="openai_configuration_invalid",
            retryable=False,
            provider="openai",
        )
    if any(ord(character) <= 32 or ord(character) == 127 for character in raw):
        raise ModelUnavailable(
            "OPENAI_BASE_URL must be a valid HTTPS API base URL",
            category="configuration",
            reason_code="openai_configuration_invalid",
            retryable=False,
            provider="openai",
        )
    try:
        parsed = urlsplit(raw)
        # Accessing port validates malformed and out-of-range port values.
        parsed.port
    except ValueError as exc:
        raise ModelUnavailable(
            "OPENAI_BASE_URL must be a valid HTTPS API base URL",
            category="configuration",
            reason_code="openai_configuration_invalid",
            retryable=False,
            provider="openai",
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelUnavailable(
            "OPENAI_BASE_URL must use HTTPS and contain no credentials, query, or fragment",
            category="configuration",
            reason_code="openai_configuration_invalid",
            retryable=False,
            provider="openai",
        )
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _openai_endpoint(style: str) -> str:
    suffix = "responses" if style == "responses" else "chat/completions"
    return f"{_openai_base_url()}/{suffix}"


def _openai_request_id(response: Any) -> str | None:
    headers = getattr(response, "headers", {})
    for name in ("x-request-id", "request-id", "openai-request-id"):
        value = str(headers.get(name, "")).strip()
        if value and _OPENAI_REQUEST_ID.fullmatch(value):
            return value
    return None


def _openai_error_markers(response: Any) -> set[str]:
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    error = payload.get("error")
    if not isinstance(error, dict):
        return set()
    markers: set[str] = set()
    for name in ("code", "type"):
        value = error.get(name)
        if isinstance(value, str) and len(value) <= 128:
            markers.add(value.strip().casefold())
    return markers


def _openai_http_failure(response: Any) -> ModelUnavailable:
    status_code = int(getattr(response, "status_code", 0) or 0)
    request_id = _openai_request_id(response)
    markers = _openai_error_markers(response)
    marker_text = " ".join(markers)

    common = {
        "provider": "openai",
        "status_code": status_code or None,
        "request_id": request_id,
    }
    if status_code == 402 or markers & _OPENAI_QUOTA_MARKERS or any(
        word in marker_text for word in ("billing", "quota", "credit")
    ):
        return ModelUnavailable(
            "OpenAI quota or billing is unavailable",
            category="quota_billing",
            reason_code="openai_quota_or_billing",
            retryable=False,
            **common,
        )
    if status_code in {401, 403} or markers & _OPENAI_AUTH_MARKERS:
        return ModelUnavailable(
            "OpenAI authentication or access failed",
            category="auth",
            reason_code="openai_auth_failed",
            retryable=False,
            **common,
        )
    if status_code == 429:
        return ModelUnavailable(
            "OpenAI rate limit exceeded",
            category="rate_limit",
            reason_code="openai_rate_limited",
            retryable=True,
            **common,
        )
    if status_code in {408, 409, 425} or status_code >= 500:
        return ModelUnavailable(
            "OpenAI is temporarily unavailable",
            category="transient",
            reason_code="openai_transient_failure",
            retryable=True,
            **common,
        )
    return ModelUnavailable(
        "OpenAI rejected the request",
        category="invalid_request",
        reason_code="openai_invalid_request",
        retryable=False,
        **common,
    )


def _openai_transport_failure(exc: httpx.RequestError) -> ModelUnavailable:
    reason_code = (
        "openai_timeout" if isinstance(exc, httpx.TimeoutException) else "openai_transport_failure"
    )
    return ModelUnavailable(
        "OpenAI is temporarily unavailable",
        category="transient",
        reason_code=reason_code,
        retryable=True,
        provider="openai",
    )


def _openai_response_body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        raise ModelUnavailable(
            "OpenAI returned an invalid response",
            category="invalid_response",
            reason_code="openai_invalid_response",
            retryable=True,
            provider="openai",
            status_code=int(getattr(response, "status_code", 0) or 0) or None,
            request_id=_openai_request_id(response),
        ) from None
    if not isinstance(body, dict):
        raise ModelUnavailable(
            "OpenAI returned an invalid response",
            category="invalid_response",
            reason_code="openai_invalid_response",
            retryable=True,
            provider="openai",
            status_code=int(getattr(response, "status_code", 0) or 0) or None,
            request_id=_openai_request_id(response),
        )
    return body


def _openai_response_text(
    body: dict[str, Any],
    style: str,
    response: Any | None = None,
) -> str:
    texts: list[str] = []
    if style == "chat_completions":
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") in {"text", "output_text"}
                    and isinstance(item.get("text"), str)
                )
    else:
        output_text = body.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text
        output = body.get("output")
        if isinstance(output, list):
            for entry in output:
                content = entry.get("content") if isinstance(entry, dict) else None
                if not isinstance(content, list):
                    continue
                texts.extend(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "output_text"
                    and isinstance(item.get("text"), str)
                )
    text = "\n".join(value for value in texts if value)
    if text.strip():
        return text
    raise ModelUnavailable(
        "OpenAI returned an invalid response",
        category="invalid_response",
        reason_code="openai_invalid_response",
        retryable=True,
        provider="openai",
        status_code=(
            int(getattr(response, "status_code", 0) or 0) or None
            if response is not None
            else None
        ),
        request_id=_openai_request_id(response) if response is not None else None,
    )


def _anthropic_request_id(response: Any) -> str | None:
    headers = getattr(response, "headers", {})
    for name in ("request-id", "x-request-id"):
        value = str(headers.get(name, "")).strip()
        if value and _OPENAI_REQUEST_ID.fullmatch(value):
            return value
    return None


def _anthropic_http_failure(response: Any) -> ModelUnavailable:
    status_code = int(getattr(response, "status_code", 0) or 0)
    common = {
        "provider": "anthropic",
        "status_code": status_code or None,
        "request_id": _anthropic_request_id(response),
    }
    if status_code in {401, 403}:
        return ModelUnavailable(
            "Anthropic authentication or access failed",
            category="auth",
            reason_code="anthropic_auth_failed",
            retryable=False,
            **common,
        )
    if status_code == 429:
        return ModelUnavailable(
            "Anthropic rate limit exceeded",
            category="rate_limit",
            reason_code="anthropic_rate_limited",
            retryable=True,
            **common,
        )
    if status_code == 408:
        return ModelUnavailable(
            "Anthropic request timed out",
            category="transient",
            reason_code="anthropic_timeout",
            retryable=True,
            **common,
        )
    if status_code >= 500:
        return ModelUnavailable(
            "Anthropic is temporarily unavailable",
            category="transient",
            reason_code="anthropic_transient_failure",
            retryable=True,
            **common,
        )
    return ModelUnavailable(
        "Anthropic rejected the request",
        category="invalid_request",
        reason_code="anthropic_invalid_request",
        retryable=False,
        **common,
    )


def _anthropic_transport_failure(exc: httpx.RequestError) -> ModelUnavailable:
    reason_code = (
        "anthropic_timeout"
        if isinstance(exc, httpx.TimeoutException)
        else "anthropic_transport_failure"
    )
    return ModelUnavailable(
        "Anthropic is temporarily unavailable",
        category="transient",
        reason_code=reason_code,
        retryable=True,
        provider="anthropic",
    )


def _anthropic_response_text(response: Any) -> str:
    common = {
        "provider": "anthropic",
        "status_code": int(getattr(response, "status_code", 0) or 0) or None,
        "request_id": _anthropic_request_id(response),
    }
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        raise ModelUnavailable(
            "Anthropic returned an invalid response",
            category="invalid_response",
            reason_code="anthropic_invalid_response",
            retryable=True,
            **common,
        ) from None
    if not isinstance(body, dict) or not isinstance(body.get("content"), list):
        raise ModelUnavailable(
            "Anthropic returned an invalid response",
            category="invalid_response",
            reason_code="anthropic_invalid_response",
            retryable=True,
            **common,
        )
    texts = [
        block.get("text", "")
        for block in body["content"]
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    content = "\n".join(text for text in texts if text).strip()
    if content:
        return content
    raise ModelUnavailable(
        "Anthropic returned an invalid response",
        category="invalid_response",
        reason_code="anthropic_invalid_response",
        retryable=True,
        **common,
    )


def _selected_review_provider() -> str:
    configured = (
        os.getenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "auto").strip().casefold()
        or "auto"
    )
    if configured not in _REVIEW_PROVIDERS:
        raise ModelUnavailable(
            "MOUCHEN_CLAUDE_REVIEW_PROVIDER is invalid",
            category="configuration",
            reason_code="review_provider_invalid",
            retryable=False,
        )
    if configured == "auto":
        command = os.getenv("CLAUDE_CODE_COMMAND", "claude")
        return "claude_code" if shutil.which(command) is not None else "anthropic"
    return configured


def _codex_cli_process_failure(stderr: str) -> ModelUnavailable:
    detail = str(stderr)[:65_536].casefold()
    common = {"provider": "codex_cli"}
    if any(marker in detail for marker in _CODEX_AUTH_FAILURE_MARKERS):
        return ModelUnavailable(
            "Codex CLI is not authenticated",
            category="auth",
            reason_code="codex_cli_not_logged_in",
            retryable=False,
            **common,
        )
    unsupported_model = any(
        marker in detail for marker in _CODEX_MODEL_FAILURE_MARKERS
    ) or re.search(
        r"\bmodel\b[^\r\n]{0,200}\b(?:not supported|not available|not found|does not exist)\b",
        detail,
    )
    if unsupported_model:
        return ModelUnavailable(
            "Codex CLI model is unsupported",
            category="configuration",
            reason_code="codex_cli_model_unsupported",
            retryable=False,
            **common,
        )
    if any(marker in detail for marker in _CODEX_NETWORK_FAILURE_MARKERS):
        return ModelUnavailable(
            "Codex CLI network request failed",
            category="transient",
            reason_code="codex_cli_network_failure",
            retryable=True,
            **common,
        )
    return ModelUnavailable(
        "Codex CLI execution failed",
        category="transient",
        reason_code="codex_cli_execution_failed",
        retryable=True,
        **common,
    )


def _claude_code_process_failure(stdout: str, stderr: str) -> ModelUnavailable:
    detail = f"{stderr}\n{stdout}"[:65_536].casefold()
    common = {"provider": "anthropic_via_claude_code"}
    if any(marker in detail for marker in _CLAUDE_AUTH_FAILURE_MARKERS):
        return ModelUnavailable(
            "Claude Code is not authenticated",
            category="auth",
            reason_code="claude_code_not_logged_in",
            retryable=False,
            **common,
        )
    unsupported_model = any(
        marker in detail for marker in _CLAUDE_MODEL_FAILURE_MARKERS
    ) or re.search(
        r"\bmodel\b[^\r\n]{0,200}\b(?:not supported|not available|not found|does not exist)\b",
        detail,
    )
    if unsupported_model:
        return ModelUnavailable(
            "Claude Code model is unsupported",
            category="configuration",
            reason_code="claude_code_model_unsupported",
            retryable=False,
            **common,
        )
    if any(marker in detail for marker in _CLAUDE_RATE_LIMIT_MARKERS):
        return ModelUnavailable(
            "Claude Code rate limit exceeded",
            category="rate_limit",
            reason_code="claude_code_rate_limited",
            retryable=True,
            **common,
        )
    if any(marker in detail for marker in _CLAUDE_NETWORK_FAILURE_MARKERS):
        return ModelUnavailable(
            "Claude Code network request failed",
            category="transient",
            reason_code="claude_code_network_failure",
            retryable=True,
            **common,
        )
    return ModelUnavailable(
        "Claude Code execution failed",
        category="execution",
        reason_code="claude_code_execution_failed",
        retryable=False,
        **common,
    )


async def probe_openai_credentials(
    timeout_seconds: float = 10.0,
) -> OpenAICredentialProbe:
    """Check OpenAI credentials with a read-only model-list request."""

    try:
        key = _openai_api_key()
        endpoint = f"{_openai_base_url()}/models"
        try:
            async with httpx.AsyncClient(
                timeout=max(0.1, min(30.0, float(timeout_seconds)))
            ) as client:
                response = await client.get(
                    endpoint,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPStatusError as exc:
            raise _openai_http_failure(exc.response) from None
        except httpx.RequestError as exc:
            raise _openai_transport_failure(exc) from exc
        if int(getattr(response, "status_code", 200)) >= 400:
            raise _openai_http_failure(response)
        body = _openai_response_body(response)
        if not isinstance(body.get("data"), list):
            raise ModelUnavailable(
                "OpenAI returned an invalid response",
                category="invalid_response",
                reason_code="openai_invalid_response",
                retryable=True,
                provider="openai",
                status_code=int(getattr(response, "status_code", 0) or 0) or None,
                request_id=_openai_request_id(response),
            )
    except ModelUnavailable as exc:
        return OpenAICredentialProbe(
            ready=False,
            reason_code=exc.reason_code,
            retryable=exc.retryable,
            status_code=exc.status_code,
            request_id=exc.request_id,
        )
    return OpenAICredentialProbe(
        ready=True,
        reason_code="openai_credentials_ready",
        retryable=False,
        status_code=int(getattr(response, "status_code", 200)),
        request_id=_openai_request_id(response),
    )


def _reasoning_effort(mode: str) -> str:
    """Map My AI Twin route modes to standard OpenAI reasoning effort values."""

    return {
        "routine": "medium",
        "complex": "high",
        "pro": "max",
    }.get(mode, "medium")


def _child_process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "MOUCHEN_API_TOKEN",
        "MOUCHEN_API_BEARER_TOKEN",
        "MOUCHEN_DB_PATH",
        "MOUCHEN_SAFETY_SALT",
        "OLLAMA_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        environment.pop(name, None)
        environment.pop(f"{name}_FILE", None)
    return environment


def _managed_process_options() -> dict[str, Any]:
    if _IS_WINDOWS:
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        }
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a managed CLI and its descendants without invoking a shell."""

    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    elif process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


async def _drain_communication(task: asyncio.Task[tuple[str, str]]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except (Exception, asyncio.CancelledError):
        pass


async def _communicate_managed_process(
    process: subprocess.Popen[str],
    input_text: str,
    timeout: float,
) -> tuple[str, str]:
    communication = asyncio.create_task(asyncio.to_thread(process.communicate, input_text))
    try:
        return await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _terminate_process_tree(process)
        await _drain_communication(communication)
        raise ModelUnavailable("managed model CLI timed out") from exc
    except asyncio.CancelledError:
        _terminate_process_tree(process)
        await _drain_communication(communication)
        raise
    except Exception:
        _terminate_process_tree(process)
        raise


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    mode: str
    second_opinion: bool = False
    raw_cloud_approved: bool = False


def preferred_openai_provider() -> str:
    configured = os.getenv("MOUCHEN_MODEL_PROVIDER", "auto").strip().casefold() or "auto"
    if configured not in _MODEL_PROVIDERS:
        raise ModelUnavailable(
            "MOUCHEN_MODEL_PROVIDER is invalid",
            category="configuration",
            reason_code="model_provider_invalid",
            retryable=False,
        )
    if configured != "auto":
        return configured
    if os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY_FILE"):
        return "openai"
    if os.getenv("MOUCHEN_CODEX_CLI_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "codex_cli"
    return "openai"


def _require_models_enabled() -> None:
    from .local_mode import rules_only

    if rules_only():
        raise ModelUnavailable(
            "This local profile uses deterministic rules; model calls are disabled",
            category="configuration", reason_code="local_rules_only",
            retryable=False, provider="rules",
        )


def choose_route(
    level: AdviceLevel,
    force_private_7b: bool = False,
    purpose: str | None = None,
) -> ModelRoute:
    if force_private_7b:
        return ModelRoute("ollama", os.getenv("OLLAMA_MODEL", "qwen3:8b"), "private")
    if level <= AdviceLevel.L1:
        return ModelRoute("template", "deterministic-v1", "local")
    if (purpose or "").strip().casefold() == "user_question":
        return ModelRoute(
            preferred_openai_provider(),
            os.getenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol"),
            "pro",
        )
    if level == AdviceLevel.L2:
        return ModelRoute(
            preferred_openai_provider(),
            os.getenv("OPENAI_ROUTINE_MODEL", "gpt-5.6-terra"),
            "routine",
        )
    if level == AdviceLevel.L3:
        return ModelRoute(
            preferred_openai_provider(),
            os.getenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol"),
            "complex",
        )
    return ModelRoute(
        preferred_openai_provider(),
        os.getenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol"),
        "pro",
        second_opinion=True,
    )


class ModelGateway:
    # Explicit capability marker used by the durable queue wrapper.  Do not
    # infer this from an exact concrete type: tests, decorators, and future
    # provider adapters may legitimately wrap or subclass the gateway.
    supports_pre_reserved_budget = True

    def __init__(
        self,
        timeout_seconds: float = 120.0,
        *,
        global_budget_reserver: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout = timeout_seconds
        self._global_budget_reserver = global_budget_reserver

    def _reserve_global_paid_call(
        self,
        provider: str,
        user_id: str | None,
        purpose: str,
    ) -> None:
        if self._global_budget_reserver is None:
            return
        if not str(user_id or "").strip():
            raise ModelUnavailable(
                "paid model calls require an authenticated user",
                category="configuration",
                reason_code="model_user_missing",
                retryable=False,
                provider=provider,
            )
        decision = self._global_budget_reserver(
            user_id=str(user_id),
            provider=provider,
            purpose=purpose,
        )
        if not decision.allowed:
            reason_code = str(
                getattr(decision, "reason_code", None)
                or "global_model_budget_exhausted"
            )
            raise ModelUnavailable(
                "model call budget is exhausted",
                category="rate_limit",
                reason_code=reason_code,
                retryable=True,
                provider=provider,
                status_code=429,
                retry_after_seconds=max(1, int(decision.retry_after)),
            )

    def reserve_paid_call(
        self,
        provider: str,
        user_id: str | None,
        purpose: str,
    ) -> bool:
        """Reserve one paid attempt before its outbound audit is persisted.

        ``generate`` and ``second_opinion`` retain their internal fallback so
        callers which do not need an audit cannot accidentally bypass the
        global fuse. Audited callers reserve explicitly, then pass
        ``global_budget_reserved=True`` to avoid charging twice.
        """

        normalized = str(provider or "").strip().casefold()
        if normalized in {"", "template", "ollama", "disabled"}:
            return False
        self._reserve_global_paid_call(normalized, user_id, purpose)
        return True

    @staticmethod
    def prepare_cloud_payload(
        prompt: str,
        context: dict[str, Any],
        *,
        allow_raw: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Finish all local privacy checks before reserving a paid attempt."""

        return prepare_cloud_request(prompt, context, allow_raw=allow_raw)

    async def generate(
        self,
        route: ModelRoute,
        prompt: str,
        context: dict[str, Any],
        user_id: str | None = None,
        *,
        global_budget_reserved: bool = False,
        payload_prepared: bool = False,
    ) -> str:
        _require_models_enabled()
        if route.provider == "template":
            if str(context.get("response_locale", "")).casefold() in {
                "en",
                "en-us",
            }:
                return "Model offline: evidence, action, first step, and verification time have been preserved."
            return "模型离线：已保留证据、动作、第一步和核验时间。"
        if route.provider == "ollama":
            return await self._ollama(route, prompt, context)
        if not payload_prepared:
            prompt, context = self.prepare_cloud_payload(
                prompt,
                context,
                allow_raw=route.raw_cloud_approved,
            )
        if route.provider in {"openai", "codex_cli"} and not global_budget_reserved:
            self.reserve_paid_call(
                route.provider,
                user_id,
                f"generate.{route.mode}",
            )
        if route.provider == "openai":
            return await self._openai(route, prompt, context, user_id)
        if route.provider == "codex_cli":
            return await self._codex_cli(route, prompt, context)
        raise ModelUnavailable(f"unsupported provider: {route.provider}")

    async def second_opinion(
        self,
        prompt: str,
        context: dict[str, Any],
        *,
        user_id: str | None = None,
        global_budget_reserved: bool = False,
        payload_prepared: bool = False,
    ) -> str:
        _require_models_enabled()
        raw_cloud_approved = bool(context.get("_raw_cloud_approved"))
        context = {
            key: value for key, value in context.items() if key != "_raw_cloud_approved"
        }
        if not payload_prepared:
            prompt, context = self.prepare_cloud_payload(
                prompt,
                context,
                allow_raw=raw_cloud_approved,
            )
        review_provider = _selected_review_provider()
        if review_provider != "disabled" and not global_budget_reserved:
            self.reserve_paid_call(
                review_provider,
                user_id,
                "second_opinion",
            )
        if review_provider == "claude_code":
            return await self._claude_code(prompt, context)
        if review_provider == "openai":
            route = ModelRoute(
                provider="openai",
                model=os.getenv(
                    "OPENAI_SECOND_OPINION_MODEL",
                    os.getenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol"),
                ),
                mode="pro",
            )
            critic_prompt = f"{_OPENAI_SECOND_OPINION_PREFIX}\n\n{prompt}"
            return await self._openai(route, critic_prompt, context, None)
        if review_provider == "disabled":
            raise ModelUnavailable(
                "Claude review is disabled",
                category="configuration",
                reason_code="review_disabled",
                retryable=False,
            )
        return await self._anthropic(prompt, context)

    def second_opinion_route(self) -> tuple[str, str]:
        review_provider = _selected_review_provider()
        if review_provider == "openai":
            return "openai_second_opinion", os.getenv(
                "OPENAI_SECOND_OPINION_MODEL",
                os.getenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol"),
            )
        if review_provider == "claude_code":
            return "anthropic_via_claude_code", os.getenv(
                "CLAUDE_CODE_REVIEW_MODEL",
                "claude-fable-5",
            )
        if review_provider == "disabled":
            return "disabled", "none"
        return "anthropic", os.getenv("ANTHROPIC_REVIEW_MODEL", "claude-opus-5")

    async def _anthropic(self, prompt: str, context: dict[str, Any]) -> str:
        try:
            key = secret_value("ANTHROPIC_API_KEY")
        except SecretConfigurationError as exc:
            raise ModelUnavailable(
                "Anthropic credentials are unavailable",
                category="auth",
                reason_code="anthropic_credentials_unavailable",
                retryable=False,
                provider="anthropic",
            ) from exc
        if not key:
            raise ModelUnavailable(
                "Anthropic credentials are not configured",
                category="auth",
                reason_code="anthropic_credentials_missing",
                retryable=False,
                provider="anthropic",
            )
        model = os.getenv("ANTHROPIC_REVIEW_MODEL", "claude-opus-5")
        payload = {
            "model": model,
            "max_tokens": _model_max_output_tokens(),
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nContext:\n"
                        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                }
            ],
        }
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPStatusError as exc:
            raise _anthropic_http_failure(exc.response) from None
        except httpx.RequestError as exc:
            raise _anthropic_transport_failure(exc) from None
        if int(getattr(response, "status_code", 200)) >= 400:
            raise _anthropic_http_failure(response)
        return _anthropic_response_text(response)

    async def _ollama(self, route: ModelRoute, prompt: str, context: dict[str, Any]) -> str:
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            token = secret_value("OLLAMA_AUTH_TOKEN")
        except SecretConfigurationError as exc:
            raise ModelUnavailable("OLLAMA_AUTH_TOKEN is not configured") from exc
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        payload = {
            "model": route.model,
            "stream": False,
            "options": {"num_predict": _model_max_output_tokens()},
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nContext:\n"
                        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                }
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{base_url}/api/chat", json=payload, headers=headers)
            if response.status_code >= 400:
                raise ModelUnavailable(f"Ollama returned HTTP {response.status_code}")
        return response.json().get("message", {}).get("content", "")

    async def _openai(
        self,
        route: ModelRoute,
        prompt: str,
        context: dict[str, Any],
        user_id: str | None,
    ) -> str:
        key = _openai_api_key()
        style = _openai_api_style()
        endpoint = _openai_endpoint(style)
        request_text = (
            f"{prompt}\n\nContext:\n"
            f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
        )
        reasoning_effort = _reasoning_effort(route.mode)
        if style == "responses":
            payload: dict[str, Any] = {
                "model": route.model,
                "max_output_tokens": _model_max_output_tokens(),
                "reasoning": {"effort": reasoning_effort},
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": request_text}],
                    }
                ],
            }
        else:
            payload = {
                "model": route.model,
                "max_completion_tokens": _model_max_output_tokens(),
                "messages": [{"role": "user", "content": request_text}],
                "reasoning_effort": reasoning_effort,
            }
        if user_id:
            try:
                salt = (
                    secret_value("MOUCHEN_SAFETY_SALT")
                    or secret_value("MOUCHEN_API_TOKEN")
                    or key
                )
            except SecretConfigurationError as exc:
                raise ModelUnavailable("safety identifier secret is unavailable") from exc
            payload["safety_identifier"] = hmac.new(
                salt.encode("utf-8"),
                user_id.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPStatusError as exc:
            raise _openai_http_failure(exc.response) from None
        except httpx.RequestError as exc:
            raise _openai_transport_failure(exc) from exc
        if int(getattr(response, "status_code", 200)) >= 400:
            raise _openai_http_failure(response)
        return _openai_response_text(
            _openai_response_body(response),
            style,
            response,
        )

    async def _codex_cli(
        self,
        route: ModelRoute,
        prompt: str,
        context: dict[str, Any],
    ) -> str:
        _reject_host_cli_for_commercial_tenants("codex_cli")
        command = os.getenv("CODEX_CLI_COMMAND", "codex")
        executable = shutil.which(command)
        if not executable:
            raise ModelUnavailable(
                "Codex CLI command is unavailable",
                category="configuration",
                reason_code="codex_cli_command_unavailable",
                retryable=False,
                provider="codex_cli",
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]+", route.model):
            raise ModelUnavailable(
                "Codex CLI model is invalid",
                category="configuration",
                reason_code="codex_cli_model_invalid",
                retryable=False,
                provider="codex_cli",
            )
        default_effort = {"routine": "medium", "complex": "high", "pro": "max"}.get(
            route.mode,
            "medium",
        )
        configured_effort = os.getenv(
            "CODEX_CLI_REASONING_EFFORT",
            default_effort,
        ).strip().casefold()
        if configured_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ModelUnavailable(
                "CODEX_CLI_REASONING_EFFORT is invalid",
                category="configuration",
                reason_code="codex_cli_effort_invalid",
                retryable=False,
                provider="codex_cli",
            )
        effort = (
            "max"
            if route.mode == "pro" and configured_effort != "ultra"
            else configured_effort
        )
        try:
            timeout = float(os.getenv("CODEX_CLI_TIMEOUT_SECONDS", "300"))
        except ValueError:
            timeout = -1
        if not 0 < timeout < float("inf"):
            raise ModelUnavailable(
                "CODEX_CLI_TIMEOUT_SECONDS is invalid",
                category="configuration",
                reason_code="codex_cli_timeout_invalid",
                retryable=False,
                provider="codex_cli",
            )
        workdir = os.getenv("CODEX_CLI_WORKDIR", tempfile.gettempdir())
        if is_english(context.get("response_locale")):
            task = (
                "You are My AI Twin's structured analyzer. Do not call tools, read "
                "files, or execute external actions. Analyze only the supplied "
                "minimal context and follow the requested output protocol exactly."
                f"\n\nTask:\n{prompt}\n\nMinimal context:\n"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
            )
        else:
            task = (
                "你是AI替身的结构化分析器。不要调用任何工具，不要读取文件，不要执行外部动作。"
                "只分析给定的最小上下文，并严格按任务要求输出。"
                f"\n\n任务：\n{prompt}\n\n最小上下文：\n"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
            )

        environment = _child_process_environment()
        ca_certificate = os.getenv("MOUCHEN_CODEX_CA_CERTIFICATE")
        if ca_certificate:
            environment["CODEX_CA_CERTIFICATE"] = ca_certificate
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "exec",
                    "--model",
                    route.model,
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                    "-C",
                    workdir,
                    "-c",
                    f'model_reasoning_effort="{effort}"',
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                **_managed_process_options(),
            )
            stdout, stderr = await _communicate_managed_process(process, task, timeout)
        except ModelUnavailable as exc:
            if str(exc) == "managed model CLI timed out":
                raise ModelUnavailable(
                    "Codex CLI timed out",
                    category="transient",
                    reason_code="codex_cli_timeout",
                    retryable=True,
                    provider="codex_cli",
                ) from None
            raise
        except Exception:
            raise ModelUnavailable(
                "Codex CLI execution failed",
                category="transient",
                reason_code="codex_cli_execution_failed",
                retryable=True,
                provider="codex_cli",
            ) from None
        if process.returncode != 0:
            raise _codex_cli_process_failure(stderr)
        content = stdout.strip()
        if not content:
            raise ModelUnavailable(
                "Codex CLI returned an invalid response",
                category="invalid_response",
                reason_code="codex_cli_invalid_response",
                retryable=True,
                provider="codex_cli",
            )
        return content

    async def _claude_code(self, prompt: str, context: dict[str, Any]) -> str:
        _reject_host_cli_for_commercial_tenants("claude_code")
        command = os.getenv("CLAUDE_CODE_COMMAND", "claude")
        executable = shutil.which(command)
        if not executable:
            raise ModelUnavailable(
                "Claude Code command is unavailable",
                category="configuration",
                reason_code="claude_code_command_unavailable",
                retryable=False,
                provider="anthropic_via_claude_code",
            )
        model = os.getenv("CLAUDE_CODE_REVIEW_MODEL", "claude-fable-5")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
            raise ModelUnavailable(
                "CLAUDE_CODE_REVIEW_MODEL is invalid",
                category="configuration",
                reason_code="claude_code_model_invalid",
                retryable=False,
                provider="anthropic_via_claude_code",
            )
        try:
            timeout = float(os.getenv("CLAUDE_CODE_TIMEOUT_SECONDS", "90"))
        except ValueError:
            timeout = -1
        if not 0 < timeout < float("inf"):
            raise ModelUnavailable(
                "CLAUDE_CODE_TIMEOUT_SECONDS is invalid",
                category="configuration",
                reason_code="claude_code_timeout_invalid",
                retryable=False,
                provider="anthropic_via_claude_code",
            )
        if is_english(context.get("response_locale")):
            review_prompt = (
                "You are My AI Twin's independent reviewer. Analyze only the supplied "
                "minimal context; do not call tools or read files. Check whether the "
                "evidence supports the conclusion, the prediction is falsifiable, "
                "and the action is specific. Return the brief requested verdict."
                f"\n\nTask:\n{prompt}\n\nMinimal context:\n"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
            )
        else:
            review_prompt = (
                "你是AI替身的独立复核器。只分析所给最小上下文，不调用工具，不读取文件。"
                "检查证据是否支持结论、预测是否可证伪、动作是否具体，并给出简短复核意见。"
                f"\n\n任务：\n{prompt}\n\n最小上下文：\n"
                f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
            )

        try:
            process = subprocess.Popen(
                [
                    executable,
                    "--print",
                    "--output-format",
                    "text",
                    "--no-session-persistence",
                    "--safe-mode",
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "",
                    "--model",
                    model,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_process_environment(),
                **_managed_process_options(),
            )
            stdout, stderr = await _communicate_managed_process(
                process,
                review_prompt,
                timeout,
            )
        except ModelUnavailable as exc:
            if str(exc) == "managed model CLI timed out":
                raise ModelUnavailable(
                    "Claude Code timed out",
                    category="transient",
                    reason_code="claude_code_timeout",
                    retryable=True,
                    provider="anthropic_via_claude_code",
                ) from None
            raise
        except (OSError, subprocess.SubprocessError):
            raise ModelUnavailable(
                "Claude Code execution failed",
                category="execution",
                reason_code="claude_code_execution_failed",
                retryable=False,
                provider="anthropic_via_claude_code",
            ) from None
        if process.returncode != 0:
            raise _claude_code_process_failure(stdout, stderr)
        content = stdout.strip()
        if not content:
            raise ModelUnavailable(
                "Claude Code returned an invalid response",
                category="invalid_response",
                reason_code="claude_code_invalid_response",
                retryable=False,
                provider="anthropic_via_claude_code",
            )
        return content
