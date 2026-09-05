import asyncio
import subprocess
import threading

import pytest

from app import model_gateway
from app.domain.models import AdviceLevel
from app.model_gateway import ModelGateway, ModelRoute, ModelUnavailable, choose_route


def test_openai_request_uses_privacy_safe_stable_identifier(monkeypatch):
    payloads = []
    urls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "ok"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            urls.append(url)
            payloads.append(json)
            assert headers["Authorization"] == "Bearer test-key"
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_STYLE", raising=False)
    monkeypatch.setenv("MOUCHEN_SAFETY_SALT", "test-salt")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())
    route = ModelRoute("openai", "gpt-5.6-sol", "pro")
    gateway = ModelGateway()

    first = asyncio.run(gateway.generate(route, "review", {}, user_id="private@example.com"))
    second = asyncio.run(gateway.generate(route, "review", {}, user_id="private@example.com"))

    assert first == second == "ok"
    assert urls == [
        "https://api.openai.com/v1/responses",
        "https://api.openai.com/v1/responses",
    ]
    assert payloads[0]["reasoning"] == {"effort": "max"}
    assert payloads[0]["max_output_tokens"] == 2048
    assert payloads[0]["safety_identifier"] == payloads[1]["safety_identifier"]
    assert "private@example.com" not in payloads[0]["safety_identifier"]


def test_openai_responses_uses_configured_https_base(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": "configured response"}
                        ]
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1/")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    reply = asyncio.run(
        ModelGateway().generate(
            ModelRoute("openai", "compatible-model", "complex"),
            "review",
            {"event": "deadline"},
        )
    )

    assert reply == "configured response"
    url, payload, headers = calls[0]
    assert url == "https://gateway.example/v1/responses"
    assert payload["model"] == "compatible-model"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["input"][0]["content"][0]["text"].startswith("review\n\nContext:\n")
    assert "messages" not in payload
    assert headers["Authorization"] == "Bearer test-key"


def test_openai_key_can_be_read_from_file(tmp_path, monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "file-backed response"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    key_file = tmp_path / "openai-key"
    key_file.write_text("file-backed-key\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_STYLE", raising=False)
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    route = choose_route(AdviceLevel.L2)
    reply = asyncio.run(ModelGateway().generate(route, "review", {}))

    assert route.provider == "openai"
    assert reply == "file-backed response"
    assert calls[0][2]["Authorization"] == "Bearer file-backed-key"


def test_openai_chat_completions_mode_builds_and_parses_compatible_request(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "chat response"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/api/v1")
    monkeypatch.setenv("OPENAI_API_STYLE", "chat_completions")
    monkeypatch.setenv("MOUCHEN_SAFETY_SALT", "test-salt")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    reply = asyncio.run(
        ModelGateway().generate(
            ModelRoute("openai", "compatible-model", "pro"),
            "review",
            {"event": "deadline"},
            user_id="private@example.com",
        )
    )

    assert reply == "chat response"
    url, payload, headers = calls[0]
    assert url == "https://gateway.example/api/v1/chat/completions"
    assert payload["messages"][0]["content"].startswith("review\n\nContext:\n")
    assert payload["reasoning_effort"] == "max"
    assert payload["max_completion_tokens"] == 2048
    assert len(payload["safety_identifier"]) == 64
    assert "private@example.com" not in payload["safety_identifier"]
    assert "input" not in payload
    assert "reasoning" not in payload
    assert headers["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize(
    ("status_code", "error_code", "category", "reason_code", "retryable"),
    [
        (401, "invalid_api_key", "auth", "openai_auth_failed", False),
        (402, "billing_not_active", "quota_billing", "openai_quota_or_billing", False),
        (429, "insufficient_quota", "quota_billing", "openai_quota_or_billing", False),
        (429, "rate_limit_exceeded", "rate_limit", "openai_rate_limited", True),
        (503, "server_error", "transient", "openai_transient_failure", True),
        (400, "invalid_request_error", "invalid_request", "openai_invalid_request", False),
    ],
)
def test_openai_http_failures_have_safe_retry_metadata(
    monkeypatch,
    status_code,
    error_code,
    category,
    reason_code,
    retryable,
):
    class Response:
        headers = {"x-request-id": "req_safe-123"}

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {
                "error": {
                    "code": error_code,
                    "type": error_code,
                    "message": "private response text and sk-secret",
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, json, headers):
            assert json["model"] == "compatible-model"
            assert headers["Authorization"] == "Bearer test-key"
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review private user text",
                {"event": "private event text"},
            )
        )

    failure = captured.value
    assert failure.category == category
    assert failure.reason_code == reason_code
    assert failure.retryable is retryable
    assert failure.provider == "openai"
    assert failure.status_code == status_code
    assert failure.request_id == "req_safe-123"
    assert failure.safe_details()["reason_code"] == reason_code
    assert "private" not in str(failure).casefold()
    assert "sk-secret" not in str(failure)


def test_openai_invalid_json_is_safe_and_retryable(monkeypatch):
    class Response:
        status_code = 200
        headers = {"x-request-id": "req_invalid-json"}

        def json(self):
            raise ValueError("private malformed response body")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, json, headers):
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.category == "invalid_response"
    assert failure.reason_code == "openai_invalid_response"
    assert failure.retryable is True
    assert failure.request_id == "req_invalid-json"
    assert failure.__cause__ is None
    assert "private malformed" not in str(failure)


def test_openai_transport_timeout_is_retryable(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            request = model_gateway.httpx.Request("POST", url)
            raise model_gateway.httpx.ReadTimeout("private timeout", request=request)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.category == "transient"
    assert failure.reason_code == "openai_timeout"
    assert failure.retryable is True
    assert "private timeout" not in str(failure)


def test_openai_raised_http_status_error_is_safely_classified(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            request = model_gateway.httpx.Request("POST", url)
            response = model_gateway.httpx.Response(
                401,
                request=request,
                headers={"x-request-id": "req_raised-401"},
                json={
                    "error": {
                        "code": "invalid_api_key",
                        "message": "private response text",
                    }
                },
            )
            raise model_gateway.httpx.HTTPStatusError(
                "private status detail",
                request=request,
                response=response,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.reason_code == "openai_auth_failed"
    assert failure.retryable is False
    assert failure.request_id == "req_raised-401"
    assert failure.__cause__ is None
    assert "private" not in str(failure).casefold()


def test_openai_credential_probe_is_read_only_and_classifies_401(monkeypatch):
    calls = []

    class Response:
        status_code = 401
        headers = {"x-request-id": "req_probe-401"}

        def json(self):
            return {
                "error": {
                    "code": "invalid_api_key",
                    "message": "private credential detail",
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, headers):
            calls.append((url, headers))
            return Response()

        async def post(self, *_args, **_kwargs):
            raise AssertionError("credential probe must not create a model response")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    result = asyncio.run(model_gateway.probe_openai_credentials())

    assert result.ready is False
    assert result.reason_code == "openai_auth_failed"
    assert result.retryable is False
    assert result.status_code == 401
    assert result.request_id == "req_probe-401"
    assert calls == [
        (
            "https://gateway.example/v1/models",
            {"Authorization": "Bearer test-key"},
        )
    ]


def test_openai_credential_probe_accepts_model_list_without_exposing_it(monkeypatch):
    class Response:
        status_code = 200
        headers = {"x-request-id": "req_probe-ready"}

        def json(self):
            return {"object": "list", "data": [{"id": "private-model-name"}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, _url, headers):
            assert headers["Authorization"] == "Bearer test-key"
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    result = asyncio.run(model_gateway.probe_openai_credentials())

    assert result == model_gateway.OpenAICredentialProbe(
        ready=True,
        reason_code="openai_credentials_ready",
        retryable=False,
        status_code=200,
        request_id="req_probe-ready",
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.example/v1",
        "https://user:password@gateway.example/v1",
        "https://gateway.example/v1?tenant=private",
        "https://gateway.example/v1#fragment",
        " https://gateway.example/v1",
        "https://gateway.example\\v1",
    ],
)
def test_openai_base_url_rejects_unsafe_values_without_network(monkeypatch, base_url):
    class Client:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid base URL must fail before network access")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", Client)

    with pytest.raises(ModelUnavailable, match="OPENAI_BASE_URL"):
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review",
                {},
            )
        )


def test_openai_api_style_rejects_unknown_value_without_network(monkeypatch):
    class Client:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid API style must fail before network access")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("OPENAI_API_STYLE", "auto")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", Client)

    with pytest.raises(ModelUnavailable, match="OPENAI_API_STYLE"):
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("openai", "compatible-model", "routine"),
                "review",
                {},
            )
        )


def test_explicit_openai_second_opinion_is_a_separate_independent_critic_call(monkeypatch):
    payloads = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": '{"supported":true}'}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, json, headers):
            assert headers["Authorization"] == "Bearer test-key"
            payloads.append(json)
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_SECOND_OPINION_MODEL", "gpt-5.6-sol")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())
    gateway = ModelGateway()

    primary = asyncio.run(
        gateway.generate(
            ModelRoute("openai", "gpt-5.6-sol", "pro"),
            "Produce the proposed advice",
            {"evidence": "deadline moved"},
        )
    )
    review = asyncio.run(
        gateway.second_opinion(
            "Return a supported verdict",
            {"proposed_action": "freeze the release"},
        )
    )

    assert primary == review == '{"supported":true}'
    assert len(payloads) == 2
    primary_text = payloads[0]["input"][0]["content"][0]["text"]
    review_text = payloads[1]["input"][0]["content"][0]["text"]
    assert "Produce the proposed advice" in primary_text
    assert "independent second-opinion critic" in review_text
    assert "Do not continue or polish" in review_text
    assert payloads[1]["reasoning"] == {"effort": "max"}
    assert gateway.second_opinion_route() == (
        "openai_second_opinion",
        "gpt-5.6-sol",
    )


def test_unknown_second_opinion_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "silent-fallback")

    with pytest.raises(ModelUnavailable, match="invalid"):
        asyncio.run(ModelGateway().second_opinion("review", {}))


def test_auto_review_does_not_fallback_after_selected_claude_failure(monkeypatch):
    gateway = ModelGateway()
    calls = []
    failure = ModelUnavailable(
        "Claude Code is not authenticated",
        category="auth",
        reason_code="claude_code_not_logged_in",
        retryable=False,
        provider="anthropic_via_claude_code",
    )

    async def claude_code(_prompt, _context):
        calls.append("claude_code")
        raise failure

    async def anthropic(_prompt, _context):
        calls.append("anthropic")
        return "must not run"

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "auto")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(gateway, "_claude_code", claude_code)
    monkeypatch.setattr(gateway, "_anthropic", anthropic)

    assert gateway.second_opinion_route() == (
        "anthropic_via_claude_code",
        "claude-fable-5",
    )
    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(gateway.second_opinion("review", {}))

    assert captured.value is failure
    assert calls == ["claude_code"]


def test_auto_review_selects_anthropic_once_when_claude_is_unavailable(monkeypatch):
    gateway = ModelGateway()
    calls = []

    async def claude_code(_prompt, _context):
        calls.append("claude_code")
        return "must not run"

    async def anthropic(_prompt, _context):
        calls.append("anthropic")
        return "supported"

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "auto")
    monkeypatch.setenv("ANTHROPIC_REVIEW_MODEL", "claude-opus-5")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: None)
    monkeypatch.setattr(gateway, "_claude_code", claude_code)
    monkeypatch.setattr(gateway, "_anthropic", anthropic)

    assert gateway.second_opinion_route() == ("anthropic", "claude-opus-5")
    assert asyncio.run(gateway.second_opinion("review", {})) == "supported"
    assert calls == ["anthropic"]


@pytest.mark.parametrize(
    ("status_code", "category", "reason_code", "retryable"),
    [
        (400, "invalid_request", "anthropic_invalid_request", False),
        (401, "auth", "anthropic_auth_failed", False),
        (403, "auth", "anthropic_auth_failed", False),
        (408, "transient", "anthropic_timeout", True),
        (429, "rate_limit", "anthropic_rate_limited", True),
        (503, "transient", "anthropic_transient_failure", True),
    ],
)
def test_anthropic_http_failures_have_safe_retry_metadata(
    monkeypatch,
    status_code,
    category,
    reason_code,
    retryable,
):
    class Response:
        headers = {"request-id": f"req_anthropic-{status_code}"}

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {"error": {"message": "private-response sk-ant-secret"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, json, headers):
            assert json["model"] == "claude-opus-5"
            assert headers["x-api-key"] == "test-anthropic-key"
            return Response()

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_REVIEW_MODEL", "claude-opus-5")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review private context", {}))

    failure = captured.value
    assert failure.category == category
    assert failure.reason_code == reason_code
    assert failure.retryable is retryable
    assert failure.provider == "anthropic"
    assert failure.status_code == status_code
    assert failure.request_id == f"req_anthropic-{status_code}"
    assert "private-response" not in str(failure)
    assert "sk-ant-secret" not in str(failure)


@pytest.mark.parametrize(
    ("failure_kind", "reason_code"),
    [
        ("timeout", "anthropic_timeout"),
        ("network", "anthropic_transport_failure"),
    ],
)
def test_anthropic_transport_failures_are_safe_and_retryable(
    monkeypatch,
    failure_kind,
    reason_code,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            request = model_gateway.httpx.Request("POST", url)
            exception = (
                model_gateway.httpx.ReadTimeout
                if failure_kind == "timeout"
                else model_gateway.httpx.ConnectError
            )
            raise exception("private transport detail", request=request)

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == "transient"
    assert failure.reason_code == reason_code
    assert failure.retryable is True
    assert failure.provider == "anthropic"
    assert "private transport" not in str(failure)


def test_anthropic_raised_http_status_is_classified_without_body_leak(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            request = model_gateway.httpx.Request("POST", url)
            response = model_gateway.httpx.Response(
                403,
                request=request,
                headers={"request-id": "req_anthropic-raised"},
                json={"error": {"message": "private forbidden response"}},
            )
            raise model_gateway.httpx.HTTPStatusError(
                "private status detail",
                request=request,
                response=response,
            )

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.reason_code == "anthropic_auth_failed"
    assert failure.retryable is False
    assert failure.request_id == "req_anthropic-raised"
    assert failure.__cause__ is None
    assert "private" not in str(failure).casefold()


@pytest.mark.parametrize("payload", [None, {}, {"content": []}, {"content": "invalid"}])
def test_anthropic_invalid_json_responses_are_safe_and_retryable(monkeypatch, payload):
    class Response:
        status_code = 200
        headers = {"request-id": "req_anthropic-invalid"}

        def json(self):
            if payload is None:
                raise ValueError("private malformed JSON")
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, json, headers):
            return Response()

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == "invalid_response"
    assert failure.reason_code == "anthropic_invalid_response"
    assert failure.retryable is True
    assert failure.request_id == "req_anthropic-invalid"
    assert "private malformed" not in str(failure)


def test_anthropic_valid_text_response_is_parsed_once(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"request-id": "req_anthropic-success"}

        def json(self):
            return {
                "content": [
                    {"type": "text", "text": "supported"},
                    {"type": "text", "text": "because evidence matches"},
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json, headers):
            calls.append((url, json, headers))
            return Response()

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())

    result = asyncio.run(ModelGateway().second_opinion("review", {}))

    assert result == "supported\nbecause evidence matches"
    assert len(calls) == 1


def test_anthropic_missing_credentials_are_terminal(monkeypatch):
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY_FILE", raising=False)

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == "auth"
    assert failure.reason_code == "anthropic_credentials_missing"
    assert failure.retryable is False
    assert failure.provider == "anthropic"


def test_claude_code_review_disables_tools_and_session_persistence(monkeypatch):
    calls = []

    class Process:
        pid = 3210
        returncode = 0

        def __init__(self, args, kwargs):
            self.args = args
            self.kwargs = kwargs

        def communicate(self, input_text):
            calls.append((self.args, self.kwargs, input_text))
            return "证据支持结论", ""

    def popen(args, **kwargs):
        return Process(args, kwargs)

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.delenv("CLAUDE_CODE_REVIEW_MODEL", raising=False)
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "must-not-reach-child")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", popen)

    reply = asyncio.run(
        ModelGateway().second_opinion(
            "核验预测",
            {
                "domain": "work",
                "event_text": "联系 owner@example.com",
                "audio": "raw-recording",
                "response_locale": "en-US",
            },
        )
    )

    assert reply == "证据支持结论"
    args, kwargs, input_text = calls[0]
    assert "--safe-mode" in args
    assert "--no-session-persistence" in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-fable-5"
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert "核验预测" in input_text
    assert "You are My AI Twin's independent reviewer." in input_text
    assert "你是AI替身的独立复核器" not in input_text
    assert "owner@example.com" not in input_text
    assert "raw-recording" not in input_text
    assert "[local-only]" in input_text
    assert "MOUCHEN_API_TOKEN" not in kwargs["env"]


def test_claude_code_missing_command_is_terminal_configuration_failure(monkeypatch):
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: None)

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == "configuration"
    assert failure.reason_code == "claude_code_command_unavailable"
    assert failure.retryable is False
    assert failure.provider == "anthropic_via_claude_code"


@pytest.mark.parametrize(
    ("stderr", "category", "reason_code", "retryable"),
    [
        (
            "Not logged in. private-token",
            "auth",
            "claude_code_not_logged_in",
            False,
        ),
        (
            "The model 'claude-fable-5' is not available. private-token",
            "configuration",
            "claude_code_model_unsupported",
            False,
        ),
        (
            "Network connection timed out. private-token",
            "transient",
            "claude_code_network_failure",
            True,
        ),
        (
            "API Error: 429 rate limit. private-token",
            "rate_limit",
            "claude_code_rate_limited",
            True,
        ),
        (
            "You've hit your limit; resets at 3pm. private-token",
            "rate_limit",
            "claude_code_rate_limited",
            True,
        ),
        (
            "rate_limit_error: too many requests. private-token",
            "rate_limit",
            "claude_code_rate_limited",
            True,
        ),
        (
            "Fetch failed: ECONNRESET. private-token",
            "transient",
            "claude_code_network_failure",
            True,
        ),
        (
            "Unexpected execution failure. private-token",
            "execution",
            "claude_code_execution_failed",
            False,
        ),
    ],
)
def test_claude_code_nonzero_exit_is_safely_classified(
    monkeypatch,
    stderr,
    category,
    reason_code,
    retryable,
):
    class Process:
        returncode = 1

        def communicate(self, _input_text):
            return "private stdout", stderr

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == category
    assert failure.reason_code == reason_code
    assert failure.retryable is retryable
    assert failure.provider == "anthropic_via_claude_code"
    assert "private" not in str(failure).casefold()


@pytest.mark.parametrize(
    ("name", "value", "reason_code"),
    [
        ("CLAUDE_CODE_REVIEW_MODEL", "invalid/model", "claude_code_model_invalid"),
        ("CLAUDE_CODE_TIMEOUT_SECONDS", "invalid", "claude_code_timeout_invalid"),
    ],
)
def test_claude_code_invalid_configuration_is_terminal(
    monkeypatch,
    name,
    value,
    reason_code,
):
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid configuration reached subprocess")
        ),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway().second_opinion("review", {}))

    failure = captured.value
    assert failure.category == "configuration"
    assert failure.reason_code == reason_code
    assert failure.retryable is False
    assert failure.provider == "anthropic_via_claude_code"


def test_claude_code_default_timeout_precedes_android_client_deadline(monkeypatch):
    captured = {}

    class Process:
        returncode = 0

    def popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    async def communicate(process, input_text, timeout):
        captured["process"] = process
        captured["input"] = input_text
        captured["timeout"] = timeout
        return "复核完成", ""

    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.delenv("CLAUDE_CODE_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", popen)
    monkeypatch.setattr(model_gateway, "_communicate_managed_process", communicate)

    reply = asyncio.run(ModelGateway().second_opinion("核验预测", {}))

    assert reply == "复核完成"
    assert captured["args"][captured["args"].index("--model") + 1] == "claude-fable-5"
    assert captured["timeout"] == 90
    assert captured["timeout"] < 120


def test_claude_code_timeout_terminates_managed_process_tree(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    terminated = []

    class Process:
        pid = 4242
        returncode = None

        def communicate(self, _input_text):
            started.set()
            assert release.wait(2)
            self.returncode = 1
            return "", ""

    process = Process()
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate(candidate):
        terminated.append(candidate.pid)
        release.set()

    monkeypatch.setattr(model_gateway, "_terminate_process_tree", terminate)

    with pytest.raises(ModelUnavailable, match="timed out") as captured:
        asyncio.run(ModelGateway().second_opinion("核验预测", {}))

    failure = captured.value
    assert failure.category == "transient"
    assert failure.reason_code == "claude_code_timeout"
    assert failure.retryable is True
    assert failure.provider == "anthropic_via_claude_code"
    assert started.is_set()
    assert terminated == [4242]


def test_claude_code_cancellation_terminates_managed_process_tree(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    terminated = []

    class Process:
        pid = 4343
        returncode = None

        def communicate(self, _input_text):
            started.set()
            assert release.wait(2)
            self.returncode = 1
            return "", ""

    process = Process()
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "claude.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate(candidate):
        terminated.append(candidate.pid)
        release.set()

    monkeypatch.setattr(model_gateway, "_terminate_process_tree", terminate)

    async def cancel_review():
        review = asyncio.create_task(ModelGateway().second_opinion("核验预测", {}))
        assert await asyncio.to_thread(started.wait, 1)
        review.cancel()
        with pytest.raises(asyncio.CancelledError):
            await review

    asyncio.run(cancel_review())

    assert terminated == [4343]


def test_windows_tree_termination_uses_taskkill_descendants_and_force(monkeypatch):
    calls = []

    class Process:
        pid = 4444
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            raise AssertionError("taskkill should have terminated the root process")

    process = Process()

    def run(args, **kwargs):
        calls.append((args, kwargs))
        process.returncode = 1
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(model_gateway, "_IS_WINDOWS", True)
    monkeypatch.setattr(model_gateway.subprocess, "run", run)

    model_gateway._terminate_process_tree(process)

    args, kwargs = calls[0]
    assert args == ["taskkill.exe", "/PID", "4444", "/T", "/F"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_claude_code_audit_route_names_the_cloud_destination(monkeypatch):
    monkeypatch.setenv("MOUCHEN_CLAUDE_REVIEW_PROVIDER", "claude_code")
    monkeypatch.setenv("CLAUDE_CODE_REVIEW_MODEL", "claude-fable-5")

    assert ModelGateway().second_opinion_route() == (
        "anthropic_via_claude_code",
        "claude-fable-5",
    )


def test_codex_cli_fallback_is_explicit_ephemeral_and_read_only(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args, kwargs):
            self.args = args
            self.kwargs = kwargs

        def communicate(self, input_text):
            recorded = dict(self.kwargs)
            recorded["input"] = input_text
            calls.append((self.args, recorded))
            return '{"intervene":false}', ""

    def popen(args, **kwargs):
        return Process(args, kwargs)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "auto")
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "true")
    monkeypatch.setenv("MOUCHEN_CODEX_CA_CERTIFICATE", "C:/certs/private-ca.pem")
    monkeypatch.setenv("MOUCHEN_API_TOKEN", "must-not-reach-child")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", popen)

    route = choose_route(AdviceLevel.L3)
    reply = asyncio.run(
        ModelGateway().generate(
            route,
            "发现问题",
            {"event": "发布失败", "response_locale": "en-US"},
        )
    )

    assert route.provider == "codex_cli"
    assert route.model == "gpt-5.6-sol"
    assert reply == '{"intervene":false}'
    args, kwargs = calls[0]
    assert "--ephemeral" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in args
    assert args[-1] == "-"
    assert "发布失败" in kwargs["input"]
    assert "You are My AI Twin's structured analyzer." in kwargs["input"]
    assert "你是AI替身的结构化分析器" not in kwargs["input"]
    assert kwargs["env"]["CODEX_CA_CERTIFICATE"] == "C:/certs/private-ca.pem"
    assert "MOUCHEN_API_TOKEN" not in kwargs["env"]


def test_codex_cli_missing_command_is_terminal_configuration_failure(monkeypatch):
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: None)

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.category == "configuration"
    assert failure.reason_code == "codex_cli_command_unavailable"
    assert failure.retryable is False
    assert failure.provider == "codex_cli"


def test_commercial_multi_user_rejects_host_codex_cli_before_execution(monkeypatch):
    monkeypatch.setenv("MOUCHEN_COMMERCIAL_MULTI_USER", "true")
    monkeypatch.setattr(
        model_gateway.shutil,
        "which",
        lambda _command: pytest.fail("host command lookup must not run"),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "tenant prompt",
                {},
            )
        )

    failure = captured.value
    assert failure.reason_code == "codex_cli_disabled_for_multi_user"
    assert failure.retryable is False


def test_commercial_multi_user_rejects_host_claude_cli_before_execution(monkeypatch):
    monkeypatch.setenv("MOUCHEN_COMMERCIAL_MULTI_USER", "true")
    monkeypatch.setattr(
        model_gateway.shutil,
        "which",
        lambda _command: pytest.fail("host command lookup must not run"),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(ModelGateway()._claude_code("tenant prompt", {}))

    failure = captured.value
    assert failure.reason_code == "claude_code_disabled_for_multi_user"
    assert failure.retryable is False


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("64", 128), ("4096", 4096), ("999999", 8192), ("invalid", 2048)],
)
def test_model_output_token_cap_is_bounded(monkeypatch, configured, expected):
    monkeypatch.setenv("MOUCHEN_MODEL_MAX_OUTPUT_TOKENS", configured)
    assert model_gateway._model_max_output_tokens() == expected


def test_codex_cli_invalid_model_is_terminal_configuration_failure(monkeypatch):
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid model reached subprocess")
        ),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "invalid/model", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.reason_code == "codex_cli_model_invalid"
    assert failure.retryable is False


def test_codex_cli_invalid_effort_is_terminal_configuration_failure(monkeypatch):
    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "turbo")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid effort reached subprocess")
        ),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.reason_code == "codex_cli_effort_invalid"
    assert failure.retryable is False


def test_codex_cli_explicit_ultra_effort_overrides_pro_default(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args):
            self.args = args

        def communicate(self, _input_text):
            calls.append(self.args)
            return "ok", ""

    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "ultra")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda args, **_kwargs: Process(args),
    )

    reply = asyncio.run(
        ModelGateway().generate(
            ModelRoute("codex_cli", "gpt-5.6-sol", "pro"),
            "review",
            {},
        )
    )

    assert reply == "ok"
    assert calls[0][calls[0].index("-c") + 1] == 'model_reasoning_effort="ultra"'


@pytest.mark.parametrize(
    ("stderr", "category", "reason_code", "retryable"),
    [
        (
            "Not logged in. private-token",
            "auth",
            "codex_cli_not_logged_in",
            False,
        ),
        (
            "The model 'gpt-5.6-sol' is not supported for this account. private-token",
            "configuration",
            "codex_cli_model_unsupported",
            False,
        ),
        (
            "Network connection timed out. private-token",
            "transient",
            "codex_cli_network_failure",
            True,
        ),
        (
            "Unexpected execution failure. private-token",
            "transient",
            "codex_cli_execution_failed",
            True,
        ),
    ],
)
def test_codex_cli_nonzero_exit_is_classified_without_stderr_leak(
    monkeypatch,
    stderr,
    category,
    reason_code,
    retryable,
):
    class Process:
        returncode = 1

        def communicate(self, _input_text):
            return "", stderr

    monkeypatch.delenv("CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.category == category
    assert failure.reason_code == reason_code
    assert failure.retryable is retryable
    assert failure.provider == "codex_cli"
    assert "private-token" not in str(failure)
    assert stderr not in str(failure)


def test_codex_cli_timeout_is_safe_and_retryable(monkeypatch):
    class Process:
        returncode = None

    async def communicate(_process, _input_text, _timeout):
        raise ModelUnavailable("managed model CLI timed out")

    monkeypatch.delenv("CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(
        model_gateway.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(model_gateway, "_communicate_managed_process", communicate)

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.category == "transient"
    assert failure.reason_code == "codex_cli_timeout"
    assert failure.retryable is True
    assert failure.provider == "codex_cli"


def test_codex_cli_execution_exception_is_safe_and_retryable(monkeypatch):
    def popen(*_args, **_kwargs):
        raise OSError("private executable path and token")

    monkeypatch.delenv("CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", popen)

    with pytest.raises(ModelUnavailable) as captured:
        asyncio.run(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "routine"),
                "review",
                {},
            )
        )

    failure = captured.value
    assert failure.reason_code == "codex_cli_execution_failed"
    assert failure.retryable is True
    assert failure.__cause__ is None
    assert "private" not in str(failure).casefold()


def test_api_key_takes_priority_over_codex_cli(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "auto")
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "true")

    assert choose_route(AdviceLevel.L3).provider == "openai"


def test_explicit_codex_cli_provider_takes_priority_over_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stale-key")
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "codex_cli")
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "false")

    assert choose_route(AdviceLevel.L3).provider == "codex_cli"


def test_explicit_openai_provider_takes_priority_over_codex_cli(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "true")

    assert choose_route(AdviceLevel.L3).provider == "openai"


def test_invalid_explicit_model_provider_is_terminal_configuration_error(monkeypatch):
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "unknown")

    with pytest.raises(ModelUnavailable) as captured:
        choose_route(AdviceLevel.L3)

    failure = captured.value
    assert failure.category == "configuration"
    assert failure.reason_code == "model_provider_invalid"
    assert failure.retryable is False


def test_user_question_l2_uses_sol_pro_with_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "auto")
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")

    route = choose_route(AdviceLevel.L2, purpose="user_question")

    assert route == ModelRoute("openai", "gpt-5.6-sol", "pro")


def test_unapproved_user_question_can_stay_on_level_one_template(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    route = choose_route(AdviceLevel.L1, purpose="user_question")

    assert route == ModelRoute("template", "deterministic-v1", "local")


def test_user_question_l2_uses_sol_max_default_with_codex_cli(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "auto")
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_COMPLEX_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "low")
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args, kwargs):
            self.args = args
            self.kwargs = kwargs

        def communicate(self, _input_text):
            calls.append((self.args, self.kwargs))
            return "ok", ""

    def popen(args, **kwargs):
        return Process(args, kwargs)

    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", popen)
    route = choose_route(AdviceLevel.L2, purpose="user_question")
    reply = asyncio.run(ModelGateway().generate(route, "发布为什么失败", {}))

    assert route == ModelRoute("codex_cli", "gpt-5.6-sol", "pro")
    assert reply == "ok"
    args, _ = calls[0]
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="max"'


def test_codex_cli_cancellation_terminates_managed_process_tree(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    terminated = []

    class Process:
        pid = 4545
        returncode = None

        def communicate(self, _input_text):
            started.set()
            assert release.wait(2)
            self.returncode = 1
            return "", ""

    process = Process()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOUCHEN_CODEX_CLI_ENABLED", "true")
    monkeypatch.setattr(model_gateway.shutil, "which", lambda _command: "codex.cmd")
    monkeypatch.setattr(model_gateway.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def terminate(candidate):
        terminated.append(candidate.pid)
        release.set()

    monkeypatch.setattr(model_gateway, "_terminate_process_tree", terminate)

    async def cancel_generation():
        task = asyncio.create_task(
            ModelGateway().generate(
                ModelRoute("codex_cli", "gpt-5.6-sol", "pro"),
                "review",
                {"event_text": "deadline risk"},
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_generation())

    assert terminated == [4545]
