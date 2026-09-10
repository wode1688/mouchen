from __future__ import annotations

import json
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any

from .settings import AppSettings, canonical_backend_origin


class BackendError(RuntimeError):
    pass


class BackendHTTPError(BackendError):
    """HTTP error with FastAPI's {"detail": ...} parsed for structured handling
    (e.g. a preference revision conflict carries the current server object)."""

    def __init__(self, status_code: int, message: str, detail: Any = None) -> None:
        super().__init__(f"Backend {status_code}: {message[:500]}")
        self.status_code = int(status_code)
        self.message = str(message)
        self.detail = detail


def _parse_http_error(status_code: int, raw: str) -> BackendHTTPError:
    message = raw[:500] or f"HTTP {status_code}"
    detail: Any = None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        detail = parsed.get("detail", parsed)
        if isinstance(detail, dict):
            message = str(detail.get("message") or "") or message
        elif isinstance(detail, str):
            message = detail[:500]
        elif isinstance(detail, list):  # FastAPI 422 validation errors
            message = json.dumps(detail, ensure_ascii=False)[:500]
    return BackendHTTPError(status_code, message, detail=detail)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never replay Mouchen requests, credentials, or bodies after a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: float):
    # Mouchen requests can carry bearer credentials and captured private
    # content.  An explicit empty ProxyHandler prevents urllib from inheriting
    # Windows or environment proxy settings for any backend request.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    ).open(request, timeout=timeout)


class BackendClient:
    def __init__(
        self,
        settings: AppSettings,
        timeout: float = 6.0,
        event_timeout: float = 330.0,
    ) -> None:
        self.settings = deepcopy(settings)
        # Revalidate at the network boundary as well as in the settings UI so
        # an in-memory mutation can never send credentials or captured text to
        # a remote cleartext endpoint.
        self.settings.validate()
        self.timeout = timeout
        self.event_timeout = event_timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def register(
        self,
        username: str,
        password: str,
        device_id: str,
        device_name: str | None = None,
        registration_code: str | None = None,
    ) -> dict[str, Any]:
        return self._authenticate(
            "/v1/auth/register",
            username,
            password,
            device_id,
            device_name,
            registration_code=registration_code,
            locale=self.settings.locale,
        )

    def login(
        self,
        username: str,
        password: str,
        device_id: str,
        device_name: str | None = None,
    ) -> dict[str, Any]:
        return self._authenticate("/v1/auth/login", username, password, device_id, device_name)

    def session(self) -> dict[str, Any]:
        value = self._request("GET", "/v1/session")
        return value if isinstance(value, dict) else {}

    def logout(self) -> dict[str, Any]:
        value = self._request("POST", "/v1/auth/logout", {})
        return value if isinstance(value, dict) else {}

    def account_preferences(self) -> dict[str, Any]:
        """Return account-wide preferences, including the authoritative locale."""
        value = self._request("GET", "/v1/account/preferences")
        return value if isinstance(value, dict) else {}

    def update_account_locale(self, locale: str) -> dict[str, Any]:
        """Persist the cross-device advice and interface language."""
        value = self._request("PUT", "/v1/account/preferences", {"locale": locale})
        return value if isinstance(value, dict) else {}

    def _authenticate(
        self,
        path: str,
        username: str,
        password: str,
        device_id: str,
        device_name: str | None,
        registration_code: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": username.strip(),
            "password": password,
            "device_id": device_id,
        }
        if device_name:
            body["device_name"] = device_name[:120]
        if registration_code:
            body["registration_code"] = registration_code.strip()[:512]
        if locale:
            body["locale"] = locale
        value = self._request("POST", path, body, authenticated=False)
        return value if isinstance(value, dict) else {}

    def analysis_status(self) -> dict[str, Any]:
        value = self._request("GET", "/v1/analysis/status")
        return value if isinstance(value, dict) else {}

    def send_event(
        self, event: dict[str, Any], proactive_cloud_approved: bool | None = None
    ) -> dict[str, Any]:
        cloud_approved = (
            self.settings.proactive_cloud_enabled
            if proactive_cloud_approved is None
            else proactive_cloud_approved
        )
        headers = {
            "X-Proactive-Cloud-Approved": str(cloud_approved).lower(),
        }
        return self._request(
            "POST",
            "/v1/events",
            event,
            headers,
            request_timeout=self.event_timeout,
        )

    def list_advice(self, limit: int = 500) -> list[dict[str, Any]]:
        value = self._request("GET", f"/v1/advice?limit={limit}")
        return value if isinstance(value, list) else []

    def feedback(self, advice_id: str, kind: str, note: str | None = None) -> dict[str, Any]:
        body = {"kind": kind, "note": note}
        return self._request("POST", f"/v1/advice/{advice_id}/feedback", body)

    def claim_advice_attention(
        self,
        device_id: str,
        *,
        platform: str = "windows",
        app_version: str | None = None,
    ) -> dict[str, Any]:
        body = {"device_id": device_id, "platform": platform}
        if app_version:
            body["app_version"] = app_version
        value = self._request("POST", "/v1/advice-attention/claim", body)
        return value if isinstance(value, dict) else {"status": "none"}

    def complete_advice_attention(
        self,
        advice_id: str,
        device_id: str,
        claim_token: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/advice-attention/{advice_id}/complete",
            {"device_id": device_id, "claim_token": claim_token},
        )

    def fail_advice_attention(
        self,
        advice_id: str,
        device_id: str,
        claim_token: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body = {"device_id": device_id, "claim_token": claim_token}
        if reason:
            body["reason"] = reason[:240]
        return self._request(
            "POST",
            f"/v1/advice-attention/{advice_id}/fail",
            body,
        )

    def list_goals(self) -> list[dict[str, Any]]:
        value = self._request("GET", "/v1/goals")
        return value if isinstance(value, list) else []

    def advice_preferences(self) -> dict[str, Any]:
        value = self._request("GET", "/v1/advice-preferences")
        return value if isinstance(value, dict) else {}

    def put_global_advice_preference(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/v1/advice-preferences/global", body)

    def put_goal_advice_preference(
        self, goal_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request("PUT", f"/v1/advice-preferences/goals/{goal_id}", body)

    def delete_goal_advice_preference(self, goal_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/advice-preferences/goals/{goal_id}")

    def create_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/goals", goal)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
        authenticated: bool = True,
    ) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Language": self.settings.locale,
        }
        if authenticated and self.settings.bearer_token:
            if self.settings.auth_mode == "session":
                request_origin = canonical_backend_origin(self.settings.backend_url)
                if request_origin != self.settings.session_origin:
                    raise BackendError(
                        "Authenticated session origin does not match the service address"
                    )
            headers["Authorization"] = f"Bearer {self.settings.bearer_token}"
        # Session mode never trusts a caller supplied identity header. Legacy
        # private-alpha installations remain usable during migration.
        if authenticated and self.settings.auth_mode == "legacy":
            headers["X-User-Id"] = self.settings.user_id
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            f"{self.settings.backend_url}{path}", data=data, headers=headers, method=method
        )
        try:
            timeout = self.timeout if request_timeout is None else request_timeout
            with _open_no_redirect(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                raise BackendError(
                    f"Backend redirect refused (HTTP {int(exc.code)})"
                ) from exc
            raw = exc.read().decode("utf-8", errors="replace")
            raise _parse_http_error(exc.code, raw) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(str(exc)) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError("Backend 返回了无法解析的数据") from exc
