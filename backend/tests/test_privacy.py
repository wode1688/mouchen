import asyncio
import json

from app import model_gateway
from app.model_gateway import ModelGateway, ModelRoute
from app.privacy import prepare_cloud_request, sanitize_cloud_context
from app.storage import Repository


def test_cloud_context_blocks_raw_sources_and_redacts_identifiers():
    context = {
        "event_text": (
            "联系人：王小明，邮箱 owner@example.com，手机 +86 138 0013 8000，"
            "详情 https://example.com/ticket/123456"
        ),
        "mail_body": "完整邮件不得出境 owner@example.com",
        "full_mail_body": "别名完整邮件也不得出境",
        "notification_body": "完整通知不得出境",
        "ime_text": "输入法原文不得出境",
        "accessibility_tree": {"text": "界面树不得出境"},
        "screenshot": "base64-image",
        "audio": b"recording-bytes",
        "api_token": "sk-" + "synthetic-test-value",
        "nested": {"api token": "opaque-secret-value"},
        "device_id": "device-private-id",
    }

    safe = sanitize_cloud_context(context)
    serialized = json.dumps(safe, ensure_ascii=False)

    assert safe["mail_body"] == "[local-only]"
    assert safe["full_mail_body"] == "[local-only]"
    assert safe["notification_body"] == "[local-only]"
    assert safe["ime_text"] == "[local-only]"
    assert safe["accessibility_tree"] == "[local-only]"
    assert safe["screenshot"] == "[local-only]"
    assert safe["audio"] == "[local-only]"
    assert safe["api_token"] == "[secret]"
    assert safe["nested"]["api_token"] == "[secret]"
    assert safe["device_id"] == "[device_id]"
    assert "王小明" not in serialized
    assert "owner@example.com" not in serialized
    assert "138 0013 8000" not in serialized
    assert "https://" not in serialized


def test_cloud_prompt_and_context_are_bounded_and_redacted():
    prompt, context = prepare_cloud_request(
        (
            "联系 owner@example.com，验证码 123456，再查看 C:\\Users\\Private\\secret.txt，"
            "Authorization: Bearer abcdefghijklmnop=="
        ),
        {"event_text": "发布失败", "recent_related_events": ["失败"] * 100},
    )

    assert "owner@example.com" not in prompt
    assert "123456" not in prompt
    assert "Private" not in prompt
    assert "abcdefghijklmnop" not in prompt
    assert len(context["recent_related_events"]) == 13
    assert context["recent_related_events"][-1] == "[truncated]"


def test_explicit_raw_cloud_preserves_personal_context_but_never_credentials():
    prompt, context = prepare_cloud_request(
        (
            "Contact owner@example.com about account 1234567890; "
            "password=hunter2; 验证码 654321"
        ),
        {
            "event_text": "Contact owner@example.com",
            "mail_body": "Original body with owner@example.com",
            "api_token": "sk-" + "synthetic-test-value",
            "nested": {"authorization": "Bearer abcdefghijklmnop"},
            "验证码": 654321,
            "card_number": 4111111111111111,
        },
        allow_raw=True,
    )

    assert "owner@example.com" in prompt
    assert "1234567890" in prompt
    assert "hunter2" not in prompt
    assert "654321" not in prompt
    assert context["event_text"] == "Contact owner@example.com"
    assert "Original body" in context["mail_body"]
    assert context["api_token"] == "[secret]"
    assert context["nested"]["authorization"] == "[secret]"
    assert context["验证码"] == "[secret]"
    assert context["card_number"] == "[secret]"


def test_explicit_raw_cloud_removes_mouchen_tokens_cards_and_private_keys():
    token = "mch_at_" + "0123456789abcdef" * 2 + "." + "A" * 43
    prompt, context = prepare_cloud_request(
        f"token {token}; card 4111 1111 1111 1111",
        {
            "event_text": (
                "-----BEGIN " + "PRIVATE KEY-----\nabc123\n-----END " + "PRIVATE KEY-----"
            )
        },
        allow_raw=True,
    )

    assert token not in prompt
    assert "4111 1111 1111 1111" not in prompt
    assert "abc123" not in context["event_text"]


def test_openai_gateway_sends_only_sanitized_slice(monkeypatch):
    payloads = []

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

        async def post(self, _url, json, headers):
            payloads.append(json)
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())
    result = asyncio.run(
        ModelGateway().generate(
            ModelRoute("openai", "gpt-5.6-sol", "pro"),
            "分析 owner@example.com 遇到的问题",
            {
                "event_text": "发布失败，联系人：王小明",
                "mail_body": "完整邮件 owner@example.com",
                "screenshot": "raw-image",
            },
            user_id="u1",
        )
    )

    outbound = payloads[0]["input"][0]["content"][0]["text"]
    assert result == "ok"
    assert "owner@example.com" not in outbound
    assert "王小明" not in outbound
    assert "完整邮件" not in outbound
    assert "raw-image" not in outbound
    assert "[local-only]" in outbound


def test_openai_gateway_honors_explicit_raw_route(monkeypatch):
    payloads = []

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

        async def post(self, _url, json, headers):
            payloads.append(json)
            return Response()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", lambda timeout: Client())
    asyncio.run(
        ModelGateway().generate(
            ModelRoute(
                "openai",
                "gpt-5.6-sol",
                "pro",
                raw_cloud_approved=True,
            ),
            "Review owner@example.com",
            {"event_text": "Contact owner@example.com"},
            user_id="u1",
        )
    )

    outbound = payloads[0]["input"][0]["content"][0]["text"]
    assert "owner@example.com" in outbound


def test_cloud_audit_applies_the_same_privacy_gate(tmp_path):
    repo = Repository(tmp_path / "audit.db")
    repo.audit_cloud_slice(
        "u1",
        "openai",
        "gpt-5.6-sol",
        "test",
        {
            "event_text": "联系 owner@example.com",
            "mail_body": "完整邮件原文",
            "authorization": "Bearer secret-value",
        },
    )
    row = repo._connection.execute(
        "SELECT redacted_context_json FROM cloud_slices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored = row["redacted_context_json"]

    assert "owner@example.com" not in stored
    assert "完整邮件原文" not in stored
    assert "secret-value" not in stored
    assert "[local-only]" in stored
    assert "[secret]" in stored
    repo.close()


def test_cloud_audit_records_the_redacted_prompt_and_context(tmp_path):
    repo = Repository(tmp_path / "prompt-audit.db")
    repo.audit_cloud_slice(
        "u1",
        "openai",
        "gpt-5.6-sol",
        "test-prompt",
        {"event_text": "Contact context@example.com"},
        prompt="Email prompt@example.com and use Bearer secret-value",
    )
    row = repo._connection.execute(
        "SELECT redacted_context_json FROM cloud_slices ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored = json.loads(row["redacted_context_json"])

    assert set(stored) == {"prompt", "context"}
    assert "prompt@example.com" not in stored["prompt"]
    assert "secret-value" not in stored["prompt"]
    assert "context@example.com" not in json.dumps(stored["context"])
    assert "[email]" in stored["prompt"]
    assert "[secret]" in stored["prompt"]
    repo.close()
