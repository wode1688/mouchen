from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from mouchen_desktop.backend import (
    BackendClient,
    BackendError,
    _open_no_redirect,
    _RejectRedirects,
)
from mouchen_desktop.settings import AppSettings


def test_network_boundary_rejects_remote_plain_http_even_if_settings_were_mutated():
    settings = AppSettings()
    settings.backend_url = "http://203.0.113.10:8788"

    with pytest.raises(ValueError, match="https"):
        BackendClient(settings)


def test_session_token_is_bound_to_login_origin():
    settings = AppSettings(backend_url="https://mouchen.example.com/api")
    settings.apply_session(
        "session-token",
        {"user_id": "server-user", "username": "alice"},
    )

    assert settings.session_origin == "https://mouchen.example.com:443"
    settings.backend_url = "https://attacker.example.net/api"

    with pytest.raises(ValueError, match="bound to another server"):
        BackendClient(settings)


def test_all_backend_redirects_fail_closed(monkeypatch):
    assert _RejectRedirects().redirect_request(None, None, 302, "Found", {}, "https://other") is None

    def redirect_response(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://other.example.net/collect"},
            None,
        )

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", redirect_response)

    with pytest.raises(BackendError, match="redirect refused"):
        BackendClient(AppSettings()).health()


def test_backend_opener_explicitly_bypasses_os_and_environment_proxies(monkeypatch):
    observed = {}
    response = object()

    class Opener:
        def open(self, request, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return response

    def fake_build_opener(*handlers):
        observed["handlers"] = handlers
        return Opener()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    request = urllib.request.Request("https://mouchen.example.com/health")

    assert _open_no_redirect(request, timeout=9.0) is response

    handlers = observed["handlers"]
    proxy_handler = next(
        handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)
    )
    assert proxy_handler.proxies == {}
    assert any(isinstance(handler, _RejectRedirects) for handler in handlers)
    assert observed["request"] is request
    assert observed["timeout"] == 9.0


def test_analysis_status_uses_authenticated_v1_endpoint(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"waiting":2}'

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["user_id"] = request.headers["X-user-id"]
        observed["authorization"] = request.headers["Authorization"]
        return Response()

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    settings = AppSettings(user_id="owner", bearer_token="secret", auth_mode="legacy")

    assert BackendClient(settings).analysis_status()["waiting"] == 2
    assert observed["url"].endswith("/v1/analysis/status")
    assert observed["user_id"] == "owner"
    assert observed["authorization"] == "Bearer secret"


def test_session_mode_uses_token_without_spoofable_user_header(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"user_id":"server-user"}'

    def fake_urlopen(request, timeout):
        observed["headers"] = dict(request.headers)
        return Response()

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    settings = AppSettings(
        auth_mode="session",
        session_user_id="server-user",
        bearer_token="session-token",
        session_origin="http://127.0.0.1:8787",
    )

    assert BackendClient(settings).session()["user_id"] == "server-user"
    assert observed["headers"]["Authorization"] == "Bearer session-token"
    assert "X-user-id" not in observed["headers"]


def test_login_posts_device_contract_without_existing_credentials(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"access_token":"new","session":{"user_id":"u-1"}}'

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["headers"] = dict(request.headers)
        observed["body"] = request.data.decode("utf-8")
        return Response()

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    result = BackendClient(AppSettings(bearer_token="old")).login(
        "alice", "long-password", "windows-1", "Alice PC"
    )

    assert result["access_token"] == "new"
    assert observed["url"].endswith("/v1/auth/login")
    assert "Authorization" not in observed["headers"]
    assert "X-user-id" not in observed["headers"]
    assert '"device_id": "windows-1"' in observed["body"]


def test_registration_sends_one_time_invitation_without_existing_credentials(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"access_token":"new","session":{"user_id":"u-1"}}'

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["headers"] = dict(request.headers)
        observed["body"] = request.data.decode("utf-8")
        return Response()

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    BackendClient(AppSettings(locale="en-US")).register(
        "alice",
        "long-password",
        "windows-1",
        "Alice PC",
        registration_code="single-use-code",
    )

    assert observed["url"].endswith("/v1/auth/register")
    assert "Authorization" not in observed["headers"]
    assert '"registration_code": "single-use-code"' in observed["body"]
    assert '"locale": "en-US"' in observed["body"]


def test_event_request_uses_model_sized_timeout(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    client = BackendClient(AppSettings(), timeout=6.0, event_timeout=330.0)

    client.send_event(
        {
            "local_id": "event-1",
            "source": "windows.manual",
            "type": "ui.visible_text",
            "facts": {"visible_text": "analyze this"},
        },
        proactive_cloud_approved=True,
    )

    assert observed["timeout"] == 330.0


def test_attention_endpoints_send_stable_device_claim_contract(monkeypatch):
    observed = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        observed.append(
            (
                request.full_url,
                request.get_method(),
                request.data.decode("utf-8"),
                timeout,
            )
        )
        if request.full_url.endswith("/claim"):
            return Response(b'{"status":"none"}')
        return Response(b'{"status":"delivered"}')

    monkeypatch.setattr("mouchen_desktop.backend._open_no_redirect", fake_urlopen)
    client = BackendClient(AppSettings())

    assert client.claim_advice_attention("windows-device", platform="windows") == {
        "status": "none"
    }
    client.complete_advice_attention("advice-1", "windows-device", "claim-token")
    client.fail_advice_attention(
        "advice-2",
        "windows-device",
        "claim-token-2",
        "tray unavailable",
    )

    assert observed[0][0].endswith("/v1/advice-attention/claim")
    assert '"device_id": "windows-device"' in observed[0][2]
    assert '"platform": "windows"' in observed[0][2]
    assert observed[1][0].endswith("/v1/advice-attention/advice-1/complete")
    assert '"claim_token": "claim-token"' in observed[1][2]
    assert observed[2][0].endswith("/v1/advice-attention/advice-2/fail")
    assert '"reason": "tray unavailable"' in observed[2][2]
