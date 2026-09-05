from __future__ import annotations

import hashlib
import platform
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from .backend import BackendClient, BackendError, BackendHTTPError
from .collectors import (
    ActiveWindowCollector,
    BrowserHistoryCollector,
    ClipboardCollector,
    FileActivityCollector,
    Signal,
    VisibleTextCollector,
)
from .privacy import prepare_outbound
from .proactive import ActivityReviewBuffer
from .settings import AppSettings, SettingsRepository
from .store import EventStore


Callback = Callable[[dict[str, Any]], None]
SessionIdentity = tuple[str, str, str, str, str]
SessionContext = tuple[int, SessionIdentity]
UISessionContext = tuple[int, str]
EVENT_SYNC_BATCH_SIZE = 100
EVENT_STORAGE_QUOTA_RETRY_SECONDS = 60.0
ADVICE_SYNC_LIMIT = 500
ADVICE_POLL_SECONDS = 30.0
MAX_PROACTIVE_REVIEW_AGE_SECONDS = 30 * 60
_LOCALLY_NEW_ADVICE_KEY = "_mouchen_locally_new"
DESKTOP_PLATFORM = "windows"
_ATTENTION_STATE_PREFIX = "advice_notification:"

_CLOUD_CANDIDATE = re.compile(
    r"失败|错误|异常|超时|逾期|截止|拒绝|投诉|取消|延误|风险|威胁|"
    r"failed|error|exception|timeout|overdue|deadline|declined|complaint|risk|urgent",
    re.IGNORECASE,
)


class DesktopAgent:
    def __init__(
        self,
        settings_repository: SettingsRepository,
        store: EventStore,
        on_status: Callback | None = None,
        on_event: Callback | None = None,
        on_advice: Callback | None = None,
        on_attention: Callback | None = None,
    ) -> None:
        self.settings_repository = settings_repository
        self.store = store
        self.on_status = on_status or (lambda _: None)
        self.on_event = on_event or (lambda _: None)
        self.on_advice = on_advice or (lambda _: None)
        self.on_attention = on_attention or (lambda _: None)
        self._settings_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._settings = settings_repository.load()
        self._session_epoch = 0
        self._locale_update_generation = 0
        # UI workers may start in a different order from the user's clicks.
        # Keep the click sequence separate from the arrival-based generation
        # so an older worker can never supersede a newer selection.
        self._locale_update_request_id = 0
        # Account-locale writes must reach the server in selection order.  A
        # generation fence alone only protects local state: without a
        # dedicated I/O lock, a slow old PUT can still finish after a newer
        # PUT and leave the authoritative server preference stale.
        self._locale_update_io_lock = threading.Lock()
        self._server_confirmed_locale = self._settings.locale
        self._server_confirmed_locale_identity = self._settings_identity(self._settings)
        self.device_id = store.get_or_create_device_id(DESKTOP_PLATFORM)
        self._stop = threading.Event()
        self._sync_wake = threading.Event()
        self._paused = False
        self._threads: list[threading.Thread] = []
        self._active = ActiveWindowCollector()
        self._visible_text = VisibleTextCollector()
        self._browser_history = BrowserHistoryCollector(
            state_loader=lambda: self.store.load_state("browser_history_cursors"),
            state_saver=lambda value: self.store.save_state("browser_history_cursors", value),
        )
        self._clipboard = ClipboardCollector()
        self._files = FileActivityCollector()
        self._activity_review = ActivityReviewBuffer()
        self._last_status: dict[str, Any] = {}
        self._last_activity_review_at = time.monotonic()
        self._event_storage_quota_identity: SessionIdentity | None = None
        self._event_storage_quota_retry_at = 0.0
        self._event_storage_quota_error = ""

    def _reset_observation_state(self) -> None:
        """Drop all in-memory cursors/deduplication state between accounts."""
        self._active.flush()
        self._active = ActiveWindowCollector()
        self._visible_text = VisibleTextCollector()
        self._browser_history = BrowserHistoryCollector(
            state_loader=lambda: self.store.load_state("browser_history_cursors"),
            state_saver=lambda value: self.store.save_state("browser_history_cursors", value),
        )
        self._clipboard = ClipboardCollector()
        self._files = FileActivityCollector()
        self._activity_review = ActivityReviewBuffer()
        self._last_activity_review_at = time.monotonic()

    @staticmethod
    def _is_authentication_failure(error: BaseException) -> bool:
        return isinstance(error, BackendHTTPError) and error.status_code in {401, 403}

    @staticmethod
    def _is_event_storage_quota_failure(error: BaseException) -> bool:
        # The backend also uses 507 for goal, charter, device and feedback
        # limits. Only the exact event-store response is safe to treat as a
        # batch-wide condition.
        return (
            isinstance(error, BackendHTTPError)
            and error.status_code == 507
            and error.message.strip().casefold() == "event storage quota exceeded"
        )

    def _defer_event_sync_for_storage_quota(
        self,
        settings: AppSettings,
        error: BackendHTTPError,
    ) -> bool:
        with self._settings_lock:
            identity = self._settings_identity(settings)
            if identity != self._settings_identity(self._settings):
                return False
            self._event_storage_quota_identity = identity
            self._event_storage_quota_retry_at = (
                time.monotonic() + EVENT_STORAGE_QUOTA_RETRY_SECONDS
            )
            self._event_storage_quota_error = str(error)
            return True

    def _event_storage_quota_backoff(
        self,
        settings: AppSettings,
    ) -> tuple[str, int] | None:
        identity = self._settings_identity(settings)
        with self._settings_lock:
            if self._event_storage_quota_identity != identity:
                return None
            remaining = self._event_storage_quota_retry_at - time.monotonic()
            if remaining <= 0:
                self._event_storage_quota_identity = None
                self._event_storage_quota_retry_at = 0.0
                self._event_storage_quota_error = ""
                return None
            retry_seconds = max(1, int(remaining + 0.999))
            return self._event_storage_quota_error, retry_seconds

    def _invalidate_runtime_session(self, error: BaseException) -> None:
        settings = self.settings
        if settings.auth_mode != "session":
            return
        settings.clear_session(preserve_identity=True)
        self.update_settings(settings)
        self._paused = True
        self._publish_status(
            running=True,
            paused=True,
            auth_required=True,
            auth_error=str(error)[:300],
        )

    @staticmethod
    def _settings_identity(settings: AppSettings) -> SessionIdentity:
        account_id = (
            settings.session_user_id
            if settings.auth_mode == "session"
            else settings.user_id
        )
        return (
            settings.backend_url.rstrip("/"),
            settings.session_origin,
            settings.auth_mode,
            account_id.strip(),
            settings.bearer_token,
        )

    def _session_context(self, settings: AppSettings | None = None) -> SessionContext:
        with self._settings_lock:
            current = deepcopy(self._settings) if settings is None else settings
            return self._session_epoch, self._settings_identity(current)

    @staticmethod
    def _ui_session_context(context: SessionContext) -> UISessionContext:
        identity_digest = hashlib.sha256(
            "\0".join(context[1]).encode("utf-8")
        ).hexdigest()
        return context[0], identity_digest

    def ui_session_context(self) -> UISessionContext:
        """Return an opaque account snapshot without exposing credentials."""
        return self._ui_session_context(self._session_context())

    def ui_session_context_is_current(
        self, context: UISessionContext | None
    ) -> bool:
        if context is None:
            return False
        return context == self._ui_session_context(self._session_context())

    def _context_is_current(
        self,
        context: SessionContext | None,
        stop_event: threading.Event | None = None,
    ) -> bool:
        if context is None:
            return True
        if stop_event is not None and stop_event.is_set():
            return False
        with self._settings_lock:
            return (
                context[0] == self._session_epoch
                and context[1] == self._settings_identity(self._settings)
            )

    @property
    def settings(self) -> AppSettings:
        with self._settings_lock:
            return deepcopy(self._settings)

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError("Previous account workers are still stopping; try again shortly")
            self._stop = threading.Event()
            with self._settings_lock:
                self._session_epoch += 1
                epoch = self._session_epoch
            self.store.purge()
            stop_event = self._stop
            self._threads = [
                threading.Thread(
                    target=self._collect_loop,
                    args=(stop_event, epoch),
                    name="mouchen-collector",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._sync_loop,
                    args=(stop_event, epoch),
                    name="mouchen-sync",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._advice_loop,
                    args=(stop_event, epoch),
                    name="mouchen-advice",
                    daemon=True,
                ),
            ]
            for thread in self._threads:
                thread.start()
        self._publish_status(
            running=True,
            paused=self._paused,
            consent_required=not self.settings.has_current_collection_consent(),
        )

    def stop(self, timeout: float = 8.0) -> None:
        flushed = self._active.flush()
        if self.settings.collection_permitted() and not self._paused:
            for signal in flushed:
                self._enqueue(signal)
        with self._lifecycle_lock:
            stop_event = self._stop
            stop_event.set()
            with self._settings_lock:
                # Invalidate every context before waiting.  A request that
                # returns late cannot mark events or advice for a new account.
                self._session_epoch += 1
            self._sync_wake.set()
            deadline = time.monotonic() + max(0.0, float(timeout))
            current = threading.current_thread()
            for thread in self._threads:
                if thread is current:
                    continue
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            alive = [thread.name for thread in self._threads if thread.is_alive()]
        self._publish_status(running=False, paused=self._paused)
        if alive:
            raise RuntimeError(
                "Background account work is still stopping; account switch was cancelled"
            )

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._publish_status(running=True, paused=self._paused)

    def update_settings(self, settings: AppSettings) -> None:
        settings.validate()
        with self._settings_lock:
            if (
                self._settings.has_session_credentials()
                and settings.backend_url.rstrip("/")
                != self._settings.backend_url.rstrip("/")
            ):
                raise ValueError("Sign out before changing the service address")
            identity_changed = (
                self._settings_identity(settings) != self._settings_identity(self._settings)
            )
            # Persist and replace one snapshot while holding the writer lock.
            # A locale refresh can therefore never race an account switch and
            # restore the old account's credentials or collection choices.
            self.settings_repository.save(settings)
            if identity_changed:
                self._session_epoch += 1
                self._stop.set()
            self._settings = deepcopy(settings)
            if identity_changed:
                # A confirmation is account-scoped.  Never use the previous
                # account's rollback value after a sign-in or account switch.
                self._server_confirmed_locale = self._settings.locale
                self._server_confirmed_locale_identity = self._settings_identity(
                    self._settings
                )
                self._event_storage_quota_identity = None
                self._event_storage_quota_retry_at = 0.0
                self._event_storage_quota_error = ""
        self._sync_wake.set()
        status_changes: dict[str, Any] = {"settings_saved": True}
        if identity_changed:
            status_changes.update(
                event_sync_deferred_for_quota=False,
                event_sync_retry_seconds=0,
                sync_error="",
            )
        self._publish_status(**status_changes)

    def apply_collection_consent(
        self,
        *,
        visible_interface: bool,
        browser_activity: bool,
        clipboard_text: bool,
        file_content: bool,
        cloud_analysis: bool,
    ) -> None:
        """Persist one authenticated account's explicit source choices."""
        settings = self.settings
        if settings.auth_mode != "session" or not settings.has_session_credentials():
            raise ValueError("必须先登录账号才能保存采集授权")
        first_consent = not settings.has_current_collection_consent()
        if first_consent:
            # Consent is prospective.  Never upload a queue produced by an
            # older build before this account made an explicit choice.
            self.store.clear_account_data()
            self._reset_observation_state()
        settings.active_window_enabled = bool(visible_interface)
        settings.visible_text_enabled = bool(visible_interface)
        settings.browser_history_enabled = bool(browser_activity)
        settings.clipboard_enabled = bool(clipboard_text)
        settings.file_watch_enabled = bool(file_content)
        settings.file_content_enabled = bool(file_content)
        settings.collection_enabled = any(
            (
                visible_interface,
                browser_activity,
                clipboard_text,
                file_content,
            )
        )
        settings.proactive_cloud_enabled = bool(cloud_analysis)
        # Full unredacted remote context remains a separate advanced setting;
        # consenting to cloud analysis alone never enables it implicitly.
        settings.allow_remote_full_context = False
        settings.save_current_collection_consent()
        self.update_settings(settings)
        self._paused = False
        self._publish_status(
            paused=False,
            consent_required=False,
            consent_saved=True,
            observation_disabled=not (
                settings.collection_permitted() or settings.cloud_analysis_permitted()
            ),
            sync_deferred_for_consent=False,
        )

    def submit_problem(self, text: str, domain: str | None = None) -> str:
        normalized = text.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        facts: dict[str, Any] = {
            "visible_text": normalized[:12_000],
            "context": "manual_problem",
            "analysis_requested": True,
            "content_kind": "user_input",
            "speaker": "user",
            "message_direction": "self",
            "visible_only": False,
            "evidence_strength": "direct",
            "session_key": f"manual-{uuid.uuid4().hex[:24]}",
            "content_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "resolution_state": "unresolved",
        }
        if domain:
            facts["domain"] = domain
        local_id = self._enqueue(
            Signal(
                source="windows.manual",
                type="ui.visible_text",
                facts=facts,
                sensitivity="sensitive",
                confidence=1.0,
            )
        )
        self._sync_wake.set()
        return local_id

    def health(self) -> dict[str, Any]:
        return BackendClient(self.settings).health()

    def validate_session(self) -> dict[str, Any]:
        settings = self.settings
        context = self._session_context(settings)
        if settings.auth_mode == "legacy":
            return {"user_id": settings.user_id, "legacy": True}
        if not settings.has_session_credentials():
            raise BackendError("请先登录AI替身")
        session = BackendClient(settings).session()
        returned_user = session.get("user") if isinstance(session.get("user"), dict) else {}
        returned_user_id = str(
            session.get("user_id")
            or returned_user.get("id")
            or returned_user.get("user_id")
            or ""
        )
        if not returned_user_id or returned_user_id != settings.session_user_id:
            raise BackendError("登录状态已失效，请重新登录")
        returned_locale = session.get("locale")
        if returned_locale and self._context_is_current(context):
            normalized_returned_locale = str(returned_locale)
            if normalized_returned_locale != settings.locale:
                updated = deepcopy(settings)
                updated.apply_session(updated.bearer_token, session)
                self.update_settings(updated)
            self._record_server_confirmed_locale_if_current(
                normalized_returned_locale,
                context,
            )
        return session

    def authenticate(
        self,
        username: str,
        password: str,
        register: bool = False,
        registration_code: str | None = None,
        expected_user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = username.strip()
        if not normalized or not password:
            raise ValueError("请输入用户名和密码")
        # A re-authentication or account switch must close the observation
        # gate before any network round-trip begins.
        self._paused = True
        with self._settings_lock:
            # Invalidate account-scoped requests immediately, including when
            # collection workers are not running.
            self._session_epoch += 1
        if any(thread.is_alive() for thread in self._threads):
            self.stop()
        current = self.settings
        previous_user_id = current.session_user_id
        if not previous_user_id and current.auth_mode == "legacy" and current.bearer_token:
            previous_user_id = current.user_id
        client = BackendClient(current)
        response = (
            client.register(
                normalized,
                password,
                self.device_id,
                platform.node() or "Windows 电脑",
                registration_code=registration_code,
            )
            if register
            else client.login(normalized, password, self.device_id, platform.node() or "Windows 电脑")
        )
        session = response.get("session") if isinstance(response.get("session"), dict) else {}
        updated = deepcopy(current)
        updated.apply_session(str(response.get("access_token", "")), session)
        # The identity is accepted only from the authenticated server session,
        # never from a local text field.
        confirmed = BackendClient(updated).session()
        confirmed_user = (
            confirmed.get("user") if isinstance(confirmed.get("user"), dict) else {}
        )
        confirmed_user_id = str(
            confirmed.get("user_id")
            or confirmed_user.get("id")
            or confirmed_user.get("user_id")
            or ""
        )
        if confirmed_user_id != updated.session_user_id:
            raise BackendError("服务器返回的账号会话不一致")
        required_user_id = (expected_user_id or "").strip()
        if required_user_id and confirmed_user_id != required_user_id:
            # A legacy-owner claim must never turn into an account switch.  In
            # particular, logging into an unrelated commercial account must
            # not clear the private-alpha owner's unsent queue or inherited
            # consent. Revoke the just-issued device session best-effort and
            # leave the established legacy settings untouched.
            try:
                BackendClient(updated).logout()
            except BackendError:
                pass
            raise BackendError("该账号不属于这台电脑的原有数据，请使用主人账号或主人领取码")
        updated.apply_session(updated.bearer_token, confirmed)
        migrated_legacy_owner = (
            current.auth_mode == "legacy"
            and current.user_id.strip() == updated.session_user_id
        )
        if migrated_legacy_owner:
            # The private-alpha owner already chose these sources.  Preserve
            # that configuration when the same server tenant gains a username.
            updated.save_current_collection_consent()
        else:
            updated.restore_current_collection_consent()
        self.update_settings(updated)
        if previous_user_id and previous_user_id != updated.session_user_id:
            # Invalidate the old epoch before clearing.  A late response can
            # therefore neither repopulate the cleared cache nor notify the
            # newly authenticated account.
            self.store.clear_account_data()
            self._reset_observation_state()
        self._paused = not updated.has_current_collection_consent()
        return confirmed

    def logout(self) -> None:
        # Stop observation before the network logout round-trip so no event can
        # be captured for an account that is in the process of signing out.
        self._paused = True
        with self._settings_lock:
            # A locale response that arrives during server logout is stale.
            self._session_epoch += 1
        if any(thread.is_alive() for thread in self._threads):
            self.stop()
        settings = self.settings
        try:
            if settings.bearer_token:
                BackendClient(settings).logout()
        finally:
            settings.clear_session()
            self.update_settings(settings)
            self.store.clear_account_data()
            self._reset_observation_state()

    def list_goals(self) -> list[dict[str, Any]]:
        return BackendClient(self.settings).list_goals()

    def advice_preferences(self) -> dict[str, Any]:
        return BackendClient(self.settings).advice_preferences()

    def update_account_locale(
        self,
        locale: str,
        *,
        request_id: int | None = None,
        expected_session_context: UISessionContext | None = None,
    ) -> dict[str, Any]:
        """Persist and apply the account-wide language without restarting collection."""
        with self._settings_lock:
            settings = deepcopy(self._settings)
            context = (self._session_epoch, self._settings_identity(settings))
            if (
                expected_session_context is not None
                and expected_session_context != self._ui_session_context(context)
            ):
                return {"_discarded": True}
            if request_id is None:
                self._locale_update_request_id += 1
            else:
                normalized_request_id = int(request_id)
                if normalized_request_id <= self._locale_update_request_id:
                    return {"_discarded": True}
                self._locale_update_request_id = normalized_request_id
            self._locale_update_generation += 1
            generation = self._locale_update_generation

        # Serialize the network writes, not just their responses.  A newer
        # request can advance the generation while an older PUT is in flight;
        # once the old PUT releases this lock, the newer PUT is guaranteed to
        # run last and therefore becomes the server's final value.
        with self._locale_update_io_lock:
            if not self._locale_context_is_current(context, generation):
                return {"_discarded": True}
            try:
                result = BackendClient(settings).update_account_locale(locale)
            except Exception:
                if not self._locale_context_is_current(context, generation):
                    return {"_discarded": True}
                confirmed_locale = self._server_confirmed_locale_for_context(context)
                if confirmed_locale:
                    restored = self._apply_account_locale_if_current(
                        confirmed_locale,
                        context,
                        generation=generation,
                    )
                    if restored is not None:
                        self._publish_status_if_context_current(
                            context,
                            None,
                            account_locale=restored.locale,
                            account_locale_context=self._ui_session_context(context),
                        )
                raise
            returned_locale = str(result.get("locale") or locale)
            # Even when a newer UI selection makes this response stale for
            # display purposes, the successful PUT is now the server's known
            # value.  Retain it so a later failed PUT can roll back correctly.
            self._record_server_confirmed_locale_if_current(returned_locale, context)
            updated = self._apply_account_locale_if_current(
                returned_locale,
                context,
                generation=generation,
            )
            if updated is None:
                return {"_discarded": True}
            # Replace any account_locale retained by _last_status.  Without
            # this, a later unrelated status update replays the old language
            # and makes the UI appear to switch back when a page is opened.
            self._publish_status_if_context_current(
                context,
                None,
                account_locale=updated.locale,
                account_locale_context=self._ui_session_context(context),
            )
        response = dict(result)
        response["locale"] = updated.locale
        response["_discarded"] = False
        self._sync_wake.set()
        return response

    def _record_server_confirmed_locale_if_current(
        self,
        locale: str,
        context: SessionContext,
        stop_event: threading.Event | None = None,
    ) -> bool:
        with self._settings_lock:
            if not self._context_is_current(context, stop_event):
                return False
            self._server_confirmed_locale = locale
            self._server_confirmed_locale_identity = context[1]
            return True

    def _server_confirmed_locale_for_context(
        self,
        context: SessionContext,
        stop_event: threading.Event | None = None,
    ) -> str | None:
        with self._settings_lock:
            if not self._context_is_current(context, stop_event):
                return None
            if self._server_confirmed_locale_identity != context[1]:
                return None
            return self._server_confirmed_locale

    def _locale_context_is_current(
        self,
        context: SessionContext,
        generation: int,
        stop_event: threading.Event | None = None,
    ) -> bool:
        with self._settings_lock:
            return (
                self._context_is_current(context, stop_event)
                and generation == self._locale_update_generation
            )

    def _apply_account_locale_if_current(
        self,
        locale: str,
        context: SessionContext,
        *,
        generation: int,
        stop_event: threading.Event | None = None,
    ) -> AppSettings | None:
        """Atomically change only locale on the current settings snapshot."""
        with self._settings_lock:
            if not self._locale_context_is_current(context, generation, stop_event):
                return None
            updated = deepcopy(self._settings)
            updated.locale = locale
            updated.validate()
            self.settings_repository.save(updated)
            self._settings = deepcopy(updated)
            return deepcopy(updated)

    def _refresh_account_locale_once(
        self,
        client: BackendClient,
        settings: AppSettings,
        *,
        context: SessionContext,
        stop_event: threading.Event | None = None,
    ) -> AppSettings | None:
        """Refresh locale while silently dropping an old account's response."""
        with self._settings_lock:
            generation = self._locale_update_generation

        # The GET and its record/apply phase share the same I/O sequence as
        # manual PUTs.  Otherwise a GET can read the old server value while a
        # PUT is in flight, return after that PUT, and overwrite the new local
        # and confirmed value despite sharing the same generation.
        with self._locale_update_io_lock:
            try:
                account_preferences = client.account_preferences()
            except Exception:
                if not self._locale_context_is_current(context, generation, stop_event):
                    return None
                raise
            if not self._locale_context_is_current(context, generation, stop_event):
                return None
            account_locale = str(account_preferences.get("locale") or "")
            if account_locale:
                self._record_server_confirmed_locale_if_current(
                    account_locale,
                    context,
                    stop_event,
                )
            if not account_locale or account_locale == settings.locale:
                return self.settings
            updated = self._apply_account_locale_if_current(
                account_locale,
                context,
                generation=generation,
                stop_event=stop_event,
            )
        if updated is not None:
            self._publish_status(
                account_locale=updated.locale,
                account_locale_context=self._ui_session_context(context),
            )
        return updated

    def _run_advice_preference_request(
        self,
        operation: Callable[[BackendClient], dict[str, Any]],
        *,
        expected_session_context: UISessionContext | None,
    ) -> dict[str, Any]:
        """Run one preference mutation without crossing account boundaries."""
        with self._settings_lock:
            settings = deepcopy(self._settings)
            context = (self._session_epoch, self._settings_identity(settings))
            if (
                expected_session_context is not None
                and expected_session_context != self._ui_session_context(context)
            ):
                return {"_discarded": True}
            client = BackendClient(settings)
        if not self._context_is_current(context):
            return {"_discarded": True}
        try:
            result = operation(client)
        except Exception:
            if not self._context_is_current(context):
                return {"_discarded": True}
            raise
        if not self._context_is_current(context):
            return {"_discarded": True}
        return result

    def save_advice_preference(
        self,
        goal_id: str | None,
        body: dict[str, Any],
        *,
        expected_session_context: UISessionContext | None = None,
    ) -> dict[str, Any]:
        payload = deepcopy(body)

        def save(client: BackendClient) -> dict[str, Any]:
            if goal_id:
                return client.put_goal_advice_preference(goal_id, payload)
            return client.put_global_advice_preference(payload)

        return self._run_advice_preference_request(
            save,
            expected_session_context=expected_session_context,
        )

    def delete_goal_advice_preference(
        self,
        goal_id: str,
        *,
        expected_session_context: UISessionContext | None = None,
    ) -> dict[str, Any]:
        return self._run_advice_preference_request(
            lambda client: client.delete_goal_advice_preference(goal_id),
            expected_session_context=expected_session_context,
        )

    def create_goal(self, goal: dict[str, Any]) -> dict[str, Any]:
        result = BackendClient(self.settings).create_goal(goal)
        self._sync_wake.set()
        return result

    def feedback(self, advice_id: str, kind: str, note: str | None = None) -> dict[str, Any]:
        settings = self.settings
        context = self._session_context(settings)
        result = BackendClient(settings).feedback(advice_id, kind, note)
        with self._settings_lock:
            if not self._context_is_current(context):
                raise BackendError("Account changed before feedback completed")
            updated = result.get("advice") if isinstance(result, dict) else None
            if isinstance(updated, dict) and str(updated.get("id", "")) == advice_id:
                self.store.save_advice(updated)
            else:
                if kind == "adopted":
                    self.store.update_advice_status(advice_id, "adopted")
                elif kind in {
                    "dismissed",
                    "irrelevant",
                    "fact_error",
                    "prediction_error",
                    "timing_error",
                    "stop_topic",
                }:
                    self.store.update_advice_status(advice_id, "dismissed")
        return result

    def complete_attention(self, advice_id: str, claim_token: str) -> dict[str, Any]:
        return BackendClient(self.settings).complete_advice_attention(
            advice_id,
            self.device_id,
            claim_token,
        )

    def fail_attention(
        self,
        advice_id: str,
        claim_token: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return BackendClient(self.settings).fail_advice_attention(
            advice_id,
            self.device_id,
            claim_token,
            reason,
        )

    def _collect_loop(self, stop_event: threading.Event, epoch: int) -> None:
        while not stop_event.is_set():
            settings = self.settings
            context = (epoch, self._settings_identity(settings))
            if settings.collection_permitted() and not self._paused:
                try:
                    signals: list[Signal] = []
                    if settings.active_window_enabled:
                        signals.extend(self._active.poll())
                    if settings.visible_text_enabled:
                        signals.extend(
                            self._visible_text.poll(settings.visible_text_excluded_apps)
                        )
                    if settings.browser_history_enabled:
                        signals.extend(self._browser_history.poll())
                    if settings.clipboard_enabled:
                        signals.extend(self._clipboard.poll())
                    if settings.file_watch_enabled:
                        signals.extend(
                            self._files.poll(
                                settings.watched_folders,
                                settings.watched_extensions,
                                settings.file_content_enabled,
                            )
                        )
                    if not self._context_is_current(context, stop_event):
                        return
                    for signal in signals:
                        self._enqueue(signal)
                    current = self._active.current
                    self._publish_status(
                        current_app=current.process if current else "",
                        current_title=current.title if current else "",
                    )
                    self._maybe_enqueue_activity_review(settings)
                except Exception as exc:
                    if self._context_is_current(context, stop_event):
                        self._publish_status(collector_error=str(exc))
            stop_event.wait(settings.poll_seconds)

    def _sync_loop(self, stop_event: threading.Event, epoch: int) -> None:
        while not stop_event.is_set():
            settings = self.settings
            context = (epoch, self._settings_identity(settings))
            try:
                client = BackendClient(settings)
                if settings.auth_mode == "session" and not settings.has_session_credentials():
                    self._paused = True
                    self._publish_status(
                        online=False,
                        paused=True,
                        auth_required=True,
                        sync_error="登录已失效，请重新登录",
                    )
                    self._sync_wake.wait(settings.sync_seconds)
                    self._sync_wake.clear()
                    continue
                health = client.health()
                if not self._context_is_current(context, stop_event):
                    return
                if settings.auth_mode == "session":
                    try:
                        refreshed = self._refresh_account_locale_once(
                            client,
                            settings,
                            context=context,
                            stop_event=stop_event,
                        )
                    except BackendError:
                        # Locale refresh must never make collection or event
                        # synchronization appear offline.
                        if not self._context_is_current(context, stop_event):
                            return
                        refreshed = settings
                    if refreshed is None:
                        return
                    if refreshed.locale != settings.locale:
                        settings = refreshed
                        client = BackendClient(refreshed)
                self._publish_status(
                    online=True,
                    backend_version=health.get("version", ""),
                    model_provider=health.get("model_provider", ""),
                    sync_error="",
                )
                self._sync_events_once(
                    client,
                    settings,
                    context=context,
                    stop_event=stop_event,
                )
                if not self._context_is_current(context, stop_event):
                    return
                try:
                    analysis_status = client.analysis_status()
                except BackendError as exc:
                    self._publish_status(analysis_status_error=str(exc))
                else:
                    if not self._context_is_current(context, stop_event):
                        return
                    self._publish_status(
                        analysis_status=analysis_status,
                        analysis_status_error="",
                    )
            except BackendError as exc:
                if not self._context_is_current(context, stop_event):
                    return
                if self._is_authentication_failure(exc):
                    self._invalidate_runtime_session(exc)
                self._publish_status(online=False, sync_error=str(exc))
            except Exception as exc:
                if not self._context_is_current(context, stop_event):
                    return
                self._publish_status(online=False, sync_error=str(exc))
            self._sync_wake.wait(settings.sync_seconds)
            self._sync_wake.clear()

    def _sync_events_once(
        self,
        client: BackendClient,
        settings: AppSettings,
        *,
        context: SessionContext | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        has_consent = settings.has_current_collection_consent()
        if not has_consent or not (
            settings.collection_permitted() or settings.cloud_analysis_permitted()
        ):
            self._publish_status_if_context_current(
                context,
                stop_event,
                consent_required=not has_consent,
                observation_disabled=has_consent,
                sync_deferred_for_consent=not has_consent,
                event_sync_deferred_for_quota=False,
                event_sync_retry_seconds=0,
                sync_error="",
            )
            return
        if not self._publish_status_if_context_current(
            context,
            stop_event,
            consent_required=False,
            observation_disabled=False,
            sync_deferred_for_consent=False,
        ):
            return
        quota_backoff = self._event_storage_quota_backoff(settings)
        if quota_backoff is not None:
            quota_error, retry_seconds = quota_backoff
            self._publish_status_if_context_current(
                context,
                stop_event,
                online=True,
                sync_error=quota_error,
                event_sync_deferred_for_quota=True,
                event_sync_retry_seconds=retry_seconds,
            )
            return
        # Clear an expired/account-cleared quota warning before a potentially
        # slow recovery batch starts, rather than after up to 100 requests.
        if not self._publish_status_if_context_current(
            context,
            stop_event,
            event_sync_deferred_for_quota=False,
            event_sync_retry_seconds=0,
            sync_error="",
        ):
            return
        last_error = ""
        quota_deferred = False
        for event in self.store.pending(limit=EVENT_SYNC_BATCH_SIZE):
            if not self._context_is_current(context, stop_event):
                break
            event_id = str(event["local_id"])
            try:
                outbound = prepare_outbound(event, settings)
                response = client.send_event(
                    outbound,
                    proactive_cloud_approved=self._should_use_cloud(event, settings),
                )
            except BackendHTTPError as exc:
                if not self._context_is_current(context, stop_event):
                    return
                if self._is_authentication_failure(exc):
                    self._invalidate_runtime_session(exc)
                    last_error = str(exc)
                    break
                if self._is_event_storage_quota_failure(exc):
                    quota_deferred = self._defer_event_sync_for_storage_quota(
                        settings, exc
                    )
                    last_error = str(exc)
                    break
                with self._settings_lock:
                    if not self._context_is_current(context, stop_event):
                        return
                    self.store.mark_failed(event_id, str(exc))
                last_error = str(exc)
                continue
            except Exception as exc:
                if not self._context_is_current(context, stop_event):
                    return
                with self._settings_lock:
                    if not self._context_is_current(context, stop_event):
                        return
                    self.store.mark_failed(event_id, str(exc))
                last_error = str(exc)
                continue
            with self._settings_lock:
                if not self._context_is_current(context, stop_event):
                    return
                self.store.mark_synced(event_id)
                try:
                    self._save_response_advice(response)
                except Exception as exc:
                    last_error = f"advice save failed: {exc}"

        self._publish_status_if_context_current(
            context,
            stop_event,
            online=True,
            sync_error=last_error,
            event_sync_deferred_for_quota=quota_deferred,
            event_sync_retry_seconds=(
                int(EVENT_STORAGE_QUOTA_RETRY_SECONDS) if quota_deferred else 0
            ),
        )

    def _advice_loop(self, stop_event: threading.Event, epoch: int) -> None:
        while not stop_event.is_set():
            try:
                settings = self.settings
                context = (epoch, self._settings_identity(settings))
                if settings.auth_mode == "session" and not settings.has_session_credentials():
                    stop_event.wait(ADVICE_POLL_SECONDS)
                    continue
                client = BackendClient(settings)
                self._poll_advice_once(client, context=context, stop_event=stop_event)
                reports_flushed = self._retry_attention_reports_once(
                    client,
                    context=context,
                    stop_event=stop_event,
                )
                if not self._context_is_current(context, stop_event):
                    return
                if settings.notifications_enabled and reports_flushed:
                    self._claim_attention_once(
                        client,
                        context=context,
                        stop_event=stop_event,
                    )
                if not self._context_is_current(context, stop_event):
                    return
                self._publish_status(online=True, advice_error="")
            except BackendError as exc:
                if not self._context_is_current(context, stop_event):
                    return
                if self._is_authentication_failure(exc):
                    self._invalidate_runtime_session(exc)
                self._publish_status(advice_error=str(exc))
            except Exception as exc:
                if not self._context_is_current(context, stop_event):
                    return
                self._publish_status(advice_error=str(exc))
            stop_event.wait(ADVICE_POLL_SECONDS)

    def _poll_advice_once(
        self,
        client: BackendClient,
        *,
        context: SessionContext | None = None,
        stop_event: threading.Event | None = None,
    ) -> int:
        changed: list[dict[str, Any]] = []
        response = client.list_advice(limit=ADVICE_SYNC_LIMIT)
        with self._settings_lock:
            if not self._context_is_current(context, stop_event):
                return 0
            for advice in response:
                advice_id = str(advice.get("id", ""))
                if not advice_id:
                    continue
                locally_new = not self.store.has_advice(advice_id)
                if self.store.save_advice(advice):
                    notification = dict(advice)
                    notification[_LOCALLY_NEW_ADVICE_KEY] = locally_new
                    changed.append(notification)

            for advice in sorted(changed, key=lambda item: str(item.get("id", ""))):
                self.on_advice(advice)
        return len(changed)

    def _claim_attention_once(
        self,
        client: BackendClient,
        *,
        context: SessionContext | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        response = client.claim_advice_attention(
            self.device_id,
            platform=DESKTOP_PLATFORM,
        )
        with self._settings_lock:
            if not self._context_is_current(context, stop_event):
                return None
            if str(response.get("status", "")) != "claimed":
                return None
            advice = response.get("advice")
            claim_token = str(response.get("claim_token", "")).strip()
            advice_id = str(advice.get("id", "")).strip() if isinstance(advice, dict) else ""
            if not advice_id or not claim_token:
                raise BackendError("Backend returned an invalid advice-attention claim")

            locally_new = not self.store.has_advice(advice_id)
            if self.store.save_advice(advice):
                notification = dict(advice)
                notification[_LOCALLY_NEW_ADVICE_KEY] = locally_new
                self.on_advice(notification)
            self.on_attention(dict(response))
        return response

    def _retry_attention_reports_once(
        self,
        client: BackendClient,
        *,
        context: SessionContext | None = None,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """Retry encrypted delivery receipts before asking the server for new work."""
        all_flushed = True
        for state_key, saved in self.store.list_states(_ATTENTION_STATE_PREFIX):
            if not self._context_is_current(context, stop_event):
                return False
            action = str(saved.get("server_report_action", ""))
            claim_token = str(saved.get("server_claim_token", ""))
            advice_id = str(saved.get("advice_id", "")) or state_key.removeprefix(
                _ATTENTION_STATE_PREFIX
            )
            if action not in {"complete", "fail"} or not claim_token or not advice_id:
                continue
            try:
                response = (
                    client.complete_advice_attention(
                        advice_id,
                        self.device_id,
                        claim_token,
                    )
                    if action == "complete"
                    else client.fail_advice_attention(
                        advice_id,
                        self.device_id,
                        claim_token,
                        str(saved.get("server_report_reason", "")) or None,
                    )
                )
            except BackendError as exc:
                if not self._context_is_current(context, stop_event):
                    return False
                detail = str(exc)
                if not (detail.startswith("Backend 404:") or detail.startswith("Backend 409:")):
                    all_flushed = False
                    with self._settings_lock:
                        if not self._context_is_current(context, stop_event):
                            return False
                        saved["server_report_status"] = "error"
                        saved["server_report_error"] = detail[:300]
                        self.store.save_state(state_key, saved)
                    continue
                response = {"status": "stale"}

            with self._settings_lock:
                if not self._context_is_current(context, stop_event):
                    return False
                saved["server_report_status"] = str(response.get("status", "reported"))
                for field in (
                    "server_report_action",
                    "server_claim_token",
                    "server_report_reason",
                    "server_report_error",
                ):
                    saved.pop(field, None)
                self.store.save_state(state_key, saved)
        return all_flushed

    def _enqueue(self, signal: Signal) -> str:
        event = signal.to_local_event()
        self._activity_review.add(event)
        event_id = self.store.enqueue(event)
        self.on_event(event)
        self._sync_wake.set()
        return event_id

    def _maybe_enqueue_activity_review(
        self,
        settings: AppSettings,
        now: float | None = None,
    ) -> str | None:
        if not settings.collection_permitted() or not settings.cloud_analysis_permitted() or self._paused:
            return None
        moment = time.monotonic() if now is None else float(now)
        review_seconds = settings.proactive_review_minutes * 60
        if moment - self._last_activity_review_at < review_seconds:
            return None
        signal = self._activity_review.build_signal(minimum_events=2)
        if signal is None:
            return None
        self._last_activity_review_at = moment
        return self._enqueue(signal)

    def _save_response_advice(self, response: dict[str, Any]) -> None:
        evaluation = response.get("evaluation") if isinstance(response, dict) else None
        if not isinstance(evaluation, dict) or evaluation.get("decision") != "publish":
            return
        advice = evaluation.get("advice")
        if isinstance(advice, dict) and advice.get("id"):
            advice_id = str(advice["id"])
            locally_new = not self.store.has_advice(advice_id)
            if self.store.save_advice(advice):
                notification = dict(advice)
                notification[_LOCALLY_NEW_ADVICE_KEY] = locally_new
                self.on_advice(notification)

    @staticmethod
    def _should_use_cloud(
        event: dict[str, Any],
        settings: AppSettings,
        now: datetime | None = None,
    ) -> bool:
        if not settings.cloud_analysis_permitted():
            return False
        facts = event.get("facts", {})
        if facts.get("historical_backfill"):
            return False
        if facts.get("context") == "proactive_activity_review":
            occurred_at = event.get("occurred_at")
            try:
                occurred = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
                if occurred.tzinfo is None:
                    occurred = occurred.replace(tzinfo=timezone.utc)
                moment = now or datetime.now(timezone.utc)
                if (moment - occurred.astimezone(timezone.utc)).total_seconds() > MAX_PROACTIVE_REVIEW_AGE_SECONDS:
                    return False
            except (TypeError, ValueError):
                return False
        if facts.get("analysis_requested"):
            return True
        searchable = " ".join(str(value) for value in facts.values() if isinstance(value, (str, int, float)))
        return bool(_CLOUD_CANDIDATE.search(searchable))

    def _publish_status_if_context_current(
        self,
        context: SessionContext | None,
        stop_event: threading.Event | None,
        **changes: Any,
    ) -> bool:
        # Serialize the context check with identity changes so an old worker
        # cannot restore a cleared quota warning after an account switch.
        with self._settings_lock:
            if not self._context_is_current(context, stop_event):
                return False
            self._publish_status(**changes)
            return True

    def _publish_status(self, **changes: Any) -> None:
        next_status = {**self._last_status, **changes, **self.store.stats()}
        if next_status != self._last_status:
            self._last_status = next_status
            self.on_status(dict(next_status))
