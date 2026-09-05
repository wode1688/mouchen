from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

from .i18n import detect_system_locale, normalize_locale
from .security import Protector, WindowsDataProtector


DEFAULT_EXTENSIONS = [
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".docx",
    ".xlsx",
    ".pdf",
]


COLLECTION_CONSENT_VERSION = 1
_ACCOUNT_CONSENT_BOOLEAN_FIELDS = (
    "collection_enabled",
    "active_window_enabled",
    "visible_text_enabled",
    "browser_history_enabled",
    "clipboard_enabled",
    "file_watch_enabled",
    "file_content_enabled",
    "proactive_cloud_enabled",
    "allow_remote_full_context",
)


def canonical_backend_origin(backend_url: str) -> str:
    """Return the normalized network origin used to bind an account token."""
    parsed = urlparse(str(backend_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Backend URL must use http:// or https://")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Backend URL port is invalid") from exc
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    effective_port = port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{effective_port}"


@dataclass(slots=True)
class AppSettings:
    backend_url: str = field(
        default_factory=lambda: os.environ.get("MOUCHEN_BACKEND_URL", "http://127.0.0.1:8787")
    )
    user_id: str = "demo-user"
    bearer_token: str = ""
    auth_mode: str = "session"
    session_user_id: str = ""
    session_username: str = ""
    # Set when a session token is issued.  An authenticated token must never
    # be sent after the service address has been changed in settings.
    session_origin: str = ""
    # Before sign-in this follows Windows. Once an account session supplies a
    # locale it becomes the local copy of the account-wide language choice.
    locale: str = field(default_factory=detect_system_locale)
    collection_enabled: bool = True
    active_window_enabled: bool = True
    visible_text_enabled: bool = True
    browser_history_enabled: bool = True
    clipboard_enabled: bool = True
    file_watch_enabled: bool = False
    file_content_enabled: bool = False
    proactive_cloud_enabled: bool = True
    notifications_enabled: bool = True
    start_with_windows: bool = False
    allow_remote_full_context: bool = False
    poll_seconds: int = 3
    sync_seconds: int = 8
    proactive_review_minutes: int = 10
    watched_folders: list[str] = field(default_factory=list)
    watched_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    visible_text_excluded_apps: list[str] = field(
        default_factory=lambda: [
            "credentialuibroker.exe",
            "keepass.exe",
            "keepassxc.exe",
            "logonui.exe",
            "winlogon.exe",
            "1password.exe",
            "bitwarden.exe",
        ]
    )
    # Consent is deliberately keyed by the authenticated server tenant rather
    # than kept as one machine-wide flag.  This prevents a second Windows user
    # from inheriting the previous account's observation permissions.
    account_consents: dict[str, dict[str, object]] = field(default_factory=dict)

    def validate(self) -> None:
        self.backend_url = self.backend_url.strip().rstrip("/")
        parsed = urlparse(self.backend_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Backend URL 必须使用 http:// 或 https://")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("Backend URL 不能包含账号信息、查询参数或片段")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Backend URL 端口无效") from exc
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == "http" and parsed.hostname.casefold() not in loopback_hosts:
            raise ValueError("远程服务器必须使用 https://，避免账号密码和采集内容被窃听")
        if self.auth_mode not in {"session", "legacy"}:
            raise ValueError("登录模式无效")
        if self.auth_mode == "legacy" and not self.user_id.strip():
            raise ValueError("旧版 User ID 不能为空")
        self.user_id = self.user_id.strip()
        self.session_user_id = self.session_user_id.strip()
        self.session_username = self.session_username.strip()
        self.session_origin = self.session_origin.strip()
        self.locale = normalize_locale(self.locale)
        if self.has_session_credentials():
            current_origin = canonical_backend_origin(self.backend_url)
            if not self.session_origin:
                raise ValueError("旧登录缺少服务器绑定，请重新登录")
            elif self.session_origin != current_origin:
                raise ValueError(
                    "The signed-in account is bound to another server. "
                    "Sign out before changing the service address."
                )
        elif self.session_origin:
            # An origin without a token is not an authorization grant.
            self.session_origin = ""
        self.poll_seconds = min(60, max(1, int(self.poll_seconds)))
        self.sync_seconds = min(300, max(2, int(self.sync_seconds)))
        self.proactive_review_minutes = min(120, max(5, int(self.proactive_review_minutes)))
        self.watched_folders = list(dict.fromkeys(str(Path(item)) for item in self.watched_folders if item))
        self.watched_extensions = [
            value if value.startswith(".") else f".{value}"
            for value in dict.fromkeys(item.strip().lower() for item in self.watched_extensions if item.strip())
        ]
        self.visible_text_excluded_apps = list(
            dict.fromkeys(
                item.strip().casefold()
                for item in self.visible_text_excluded_apps
                if item.strip()
            )
        )
        if not isinstance(self.account_consents, dict):
            self.account_consents = {}
        else:
            self.account_consents = {
                str(key): dict(value)
                for key, value in self.account_consents.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    def has_session_credentials(self) -> bool:
        return bool(
            self.auth_mode == "session"
            and self.bearer_token.strip()
            and self.session_user_id.strip()
        )

    def apply_session(self, access_token: str, session: dict[str, object]) -> None:
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        user_id = str(
            session.get("user_id")
            or user.get("id")
            or user.get("user_id")
            or ""
        ).strip()
        username = str(
            session.get("username")
            or user.get("username")
            or user.get("display_name")
            or ""
        ).strip()
        token = str(access_token or "").strip()
        if not token or not user_id:
            raise ValueError("服务器未返回完整的登录会话")
        self.auth_mode = "session"
        self.bearer_token = token
        self.session_user_id = user_id
        self.session_username = username
        self.session_origin = canonical_backend_origin(self.backend_url)
        session_locale = session.get("locale")
        if session_locale:
            self.locale = normalize_locale(session_locale, fallback=self.locale)

    def clear_session(self, *, preserve_identity: bool = False) -> None:
        self.auth_mode = "session"
        self.bearer_token = ""
        self.session_origin = ""
        # Signed-out screens follow Windows; the next authenticated session
        # will replace this with the account-wide locale.
        self.locale = detect_system_locale()
        if not preserve_identity:
            self.session_user_id = ""
            self.session_username = ""

    def is_local_backend(self) -> bool:
        host = (urlparse(self.backend_url).hostname or "").casefold()
        return host in {"127.0.0.1", "localhost", "::1"}

    def current_consent_key(self) -> str:
        """Return a non-identifying key for this authenticated account."""
        if self.auth_mode != "session" or not self.session_user_id.strip():
            return ""
        # Preserve path case: two deployments can legally differ only by a
        # case-sensitive URL path and must never share a consent profile.
        identity = f"{self.backend_url.rstrip('/')}\n{self.session_user_id.strip()}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def current_consent(self) -> dict[str, object]:
        key = self.current_consent_key()
        value = self.account_consents.get(key, {}) if key else {}
        return dict(value) if isinstance(value, dict) else {}

    def has_current_collection_consent(self) -> bool:
        # The private-alpha owner used an explicit all-access configuration
        # before account sessions existed.  Do not silently reset that owner.
        if self.auth_mode == "legacy":
            return True
        profile = self.current_consent()
        try:
            return int(profile.get("version", 0)) >= COLLECTION_CONSENT_VERSION
        except (TypeError, ValueError):
            return False

    def collection_permitted(self) -> bool:
        return self.has_current_collection_consent() and self.collection_enabled

    def cloud_analysis_permitted(self) -> bool:
        return self.has_current_collection_consent() and self.proactive_cloud_enabled

    def disable_unconsented_collection(self) -> None:
        """Apply safe in-memory defaults without deleting another account's profile."""
        for name in _ACCOUNT_CONSENT_BOOLEAN_FIELDS:
            setattr(self, name, False)
        self.watched_folders = []

    def save_current_collection_consent(self) -> None:
        key = self.current_consent_key()
        if not key:
            raise ValueError("必须先登录账号才能保存采集授权")
        profile: dict[str, object] = {
            "version": COLLECTION_CONSENT_VERSION,
            **{name: bool(getattr(self, name)) for name in _ACCOUNT_CONSENT_BOOLEAN_FIELDS},
            "watched_folders": list(self.watched_folders),
            "watched_extensions": list(self.watched_extensions),
        }
        self.account_consents = {**self.account_consents, key: profile}

    def restore_current_collection_consent(self) -> bool:
        if self.auth_mode == "legacy":
            return True
        if not self.has_current_collection_consent():
            self.disable_unconsented_collection()
            return False
        profile = self.current_consent()
        for name in _ACCOUNT_CONSENT_BOOLEAN_FIELDS:
            setattr(self, name, bool(profile.get(name, False)))
        folders = profile.get("watched_folders", [])
        extensions = profile.get("watched_extensions", DEFAULT_EXTENSIONS)
        self.watched_folders = list(folders) if isinstance(folders, list) else []
        self.watched_extensions = (
            list(extensions) if isinstance(extensions, list) else list(DEFAULT_EXTENSIONS)
        )
        return True


def default_data_dir() -> Path:
    override = os.environ.get("MOUCHEN_DESKTOP_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable")
    return Path(local) / "Mouchen" / "Desktop"


class SettingsRepository:
    def __init__(self, path: Path | None = None, protector: Protector | None = None) -> None:
        data_dir = default_data_dir() if path is None else path.parent
        self.path = path or data_dir / "settings.json"
        self.protector = protector or WindowsDataProtector()

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(AppSettings)} - {"bearer_token"}
        values = {key: value for key, value in raw.items() if key in allowed}
        protected = str(raw.get("bearer_token_protected", ""))
        pre_origin_session = (
            raw.get("auth_mode") == "session"
            and bool(protected)
            and not str(raw.get("session_origin", "")).strip()
        )
        if protected and not pre_origin_session:
            values["bearer_token"] = self.protector.unprotect(base64.b64decode(protected)).decode("utf-8")
        # Existing private-alpha files authenticated with X-User-Id. Preserve
        # that behavior while new installations default to account sessions.
        if "auth_mode" not in raw and protected:
            values["auth_mode"] = "legacy"
        settings = AppSettings(**values)
        settings.validate()
        return settings

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        values = asdict(settings)
        token = values.pop("bearer_token", "")
        values["bearer_token_protected"] = (
            base64.b64encode(self.protector.protect(token.encode("utf-8"))).decode("ascii") if token else ""
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
