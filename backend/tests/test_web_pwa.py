from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _document() -> tuple[str, _DocumentParser]:
    source = _read("index.html")
    parser = _DocumentParser()
    parser.feed(source)
    return source, parser


def test_account_pwa_assets_are_utf8_linked_and_mobile_ready():
    source, document = _document()
    required = {
        "index.html",
        "app.css",
        "app.js",
        "manifest.webmanifest",
        "manifest-en.webmanifest",
        "sw.js",
        "icon.svg",
    }
    assert required <= {path.name for path in STATIC_DIR.iterdir()}
    assert "AI替身" in source
    assert "�" not in source

    links = [attrs.get("href") for tag, attrs in document.tags if tag == "link"]
    scripts = [attrs.get("src") for tag, attrs in document.tags if tag == "script"]
    assert "/static/manifest.webmanifest" in links
    assert "/static/app.css" in links
    assert "/static/app.js" in scripts

    metas = {
        attrs.get("name"): attrs.get("content")
        for tag, attrs in document.tags
        if tag == "meta" and attrs.get("name")
    }
    assert "viewport-fit=cover" in (metas["viewport"] or "")
    assert metas["apple-mobile-web-app-capable"] == "yes"
    assert metas["apple-mobile-web-app-title"] == "AI替身"

    manifest = json.loads(_read("manifest.webmanifest"))
    assert manifest["lang"] == "zh-CN"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/static/index.html"
    assert manifest["scope"] == "/static/"
    assert manifest["icons"][0]["src"] == "/static/icon.svg"
    assert "maskable" in manifest["icons"][0]["purpose"]

    english_manifest = json.loads(_read("manifest-en.webmanifest"))
    assert english_manifest["lang"] == "en-US"
    assert english_manifest["name"] == "My AI Twin · Personal AI Chief of Staff"
    assert english_manifest["short_name"] == "My AI Twin"
    assert english_manifest["start_url"] == manifest["start_url"]


def test_account_forms_cover_login_invite_registration_and_logout():
    source, document = _document()
    required_ids = {
        "authScreen",
        "appScreen",
        "loginForm",
        "registerForm",
        "loginUsername",
        "loginPassword",
        "registerUsername",
        "registerPassword",
        "registerPasswordConfirm",
        "registrationCode",
        "logoutButton",
        "settingsLogoutButton",
        "changePasswordForm",
        "currentPassword",
        "newPassword",
        "newPasswordConfirm",
        "exportAccountButton",
        "exportAccountPassword",
        "deleteAccountForm",
        "deleteAccountPassword",
        "deleteAccountConfirm",
        "authLanguageSelect",
        "accountLanguageSelect",
    }
    assert required_ids <= document.ids
    assert "邀请码注册" in source
    assert "访问令牌只保存在当前浏览器会话中" in source

    inputs = {
        attrs.get("id"): attrs
        for tag, attrs in document.tags
        if tag == "input" and attrs.get("id")
    }
    assert inputs["loginUsername"]["autocomplete"] == "username"
    assert inputs["loginPassword"]["autocomplete"] == "current-password"
    assert inputs["registerPassword"]["autocomplete"] == "new-password"
    assert inputs["registrationCode"]["autocomplete"] == "one-time-code"

    script = _read("app.js")
    for path in ("/v1/auth/login", "/v1/auth/register", "/v1/session", "/v1/auth/logout"):
        assert path in script
    for path in ("/v1/account/password", "/v1/account/export", "/v1/account"):
        assert path in script
    assert 'headers.set("Authorization", `Bearer ${token}`)' in script
    assert "await validateSession()" in script
    assert "URL.createObjectURL(blob)" in script
    assert "current_password: passwordInput.value" in script
    assert 'expireSession(tr(' in script
    assert '"账号和服务器中的个人数据已删除。"' in script
    assert '"The account and its personal data have been deleted from the server."' in script


def test_browser_identity_comes_only_from_bearer_session():
    source = _read("index.html") + _read("app.js")
    assert "X-User-Id" not in source
    assert "demo-user" not in source
    assert "userId" not in source

    script = _read("app.js")
    assert 'sessionStorage.setItem(TOKEN_KEY, token)' in script
    assert "sessionStorage.getItem(TOKEN_KEY)" in script
    assert "sessionStorage.removeItem(TOKEN_KEY)" in script
    assert not re.search(r"localStorage\.(?:getItem|setItem|removeItem)\(TOKEN_KEY", script)

    durable_storage_arguments = re.findall(
        r"localStorage\.(?:getItem|setItem|removeItem)\(([^,\)]+)", script
    )
    assert durable_storage_arguments
    assert set(map(str.strip, durable_storage_arguments)) == {"DEVICE_KEY"}


def test_unauthorized_api_response_clears_token_and_returns_to_login():
    script = _read("app.js")
    assert "response.status === 401 && authenticated" in script
    assert 'sessionStorage.removeItem(TOKEN_KEY)' in script
    assert '"登录已失效，请重新登录。"' in script
    assert '"Your session has expired. Please sign in again."' in script
    assert "byId(\"appScreen\").hidden = true" in script
    assert "byId(\"authScreen\").hidden = false" in script


def test_session_epoch_aborts_stale_requests_and_rejects_redirects():
    script = _read("app.js")
    assert "epoch: 0" in script
    assert "requestControllers: new Set()" in script
    assert "beginSessionTransition()" in script
    assert "controller.abort()" in script
    assert "requestEpoch !== state.epoch" in script
    assert "token !== accessToken()" in script
    assert 'redirect: "error"' in script
    assert "response.redirected" in script
    assert "requireSameOriginPath(path)" in script
    assert 'path.startsWith("//")' in script


def test_pwa_responses_have_browser_security_headers():
    from fastapi.testclient import TestClient

    import app.main as main

    response = TestClient(main.app).get("/")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "microphone=()" in response.headers["permissions-policy"]


def test_cloud_and_raw_context_require_separate_explicit_session_consent():
    source, document = _document()
    assert {"cloudAnalysisConsent", "rawContextConsent", "rawConsentAcknowledge"} <= document.ids
    assert "两个开关默认关闭" in source
    assert "完整原文可能包含身份、账号、通信对象和商业信息" in source

    inputs = {
        attrs.get("id"): attrs
        for tag, attrs in document.tags
        if tag == "input" and attrs.get("id")
    }
    assert "checked" not in inputs["cloudAnalysisConsent"]
    assert "checked" not in inputs["rawContextConsent"]
    assert "disabled" in inputs["rawContextConsent"]

    script = _read("app.js")
    assert "consent: {cloud: false, raw: false}" in script
    assert '"X-Proactive-Cloud-Approved": state.consent.cloud ? "true" : "false"' in script
    assert (
        '"X-Raw-Cloud-Approved": state.consent.cloud && state.consent.raw ? "true" : "false"'
        in script
    )
    assert "state.consent.cloud = true" in script
    assert "state.consent.raw = true" in script
    assert "rawConsentAcknowledge" in script
    assert "sessionStorage.setItem" not in "\n".join(
        line for line in script.splitlines() if "consent" in line.casefold()
    )


def test_account_locale_switches_interface_and_future_counsel_across_devices():
    source, document = _document()
    assert {"authLanguageSelect", "accountLanguageSelect"} <= document.ids
    assert 'value="zh-CN"' in source
    assert 'value="en-US"' in source
    assert "此设置属于账号，并会同步到其他设备" in source

    script = _read("app.js")
    assert 'const DEFAULT_LOCALE = "zh-CN"' in script
    assert 'headers.set("Accept-Language", state.locale)' in script
    assert "locale: state.locale" in script
    assert 'apiRequest("/v1/account/preferences"' in script
    assert 'method: "PUT"' in script
    assert "applyLocale(session.locale || state.locale" in script
    assert 'state.session.locale = preferences.locale' in script
    assert "localeGeneration: 0" in script
    assert "localeMutationPending: false" in script
    assert "refreshLocaleGeneration === state.localeGeneration" in script
    assert "!state.localeMutationPending" in script
    assert "applyLocale(preferences.locale);" in script
    assert '"/static/manifest-en.webmanifest"' in script
    assert '"Important things"' in script
    assert '"Your personal AI chief of staff"' in script
    assert "Existing counsel and original evidence are not mechanically translated." in script


def test_service_worker_caches_only_public_static_shell():
    worker = _read("sw.js")
    assert 'url.pathname.startsWith("/v1/")' in worker
    assert 'request.headers.has("Authorization")' in worker
    assert 'request.method !== "GET"' in worker
    assert "STATIC_PATHS.has(url.pathname)" in worker
    assert "cache.addAll(STATIC_SHELL)" in worker
    assert "cache.put" not in worker
    assert "accessToken" not in worker
    assert "sessionStorage" not in worker
    assert "localStorage" not in worker
    assert "/v1/session" not in worker
    assert "/v1/advice" not in worker
    assert "/v1/goals" not in worker

    shell_match = re.search(
        r"const STATIC_SHELL = Object\.freeze\(\[(.*?)\]\);",
        worker,
        flags=re.DOTALL,
    )
    assert shell_match
    shell = set(re.findall(r'"([^"]+)"', shell_match.group(1)))
    assert shell == {
        "/static/index.html",
        "/static/app.css",
        "/static/app.js",
        "/static/icon.svg",
        "/static/manifest-en.webmanifest",
        "/static/manifest.webmanifest",
    }
    assert all(not path.startswith("/v1") for path in shell)


@pytest.mark.parametrize("script_name", ["app.js", "sw.js"])
def test_browser_javascript_has_valid_syntax(script_name: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed in this test environment")
    completed = subprocess.run(
        [node, "--check", str(STATIC_DIR / script_name)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
