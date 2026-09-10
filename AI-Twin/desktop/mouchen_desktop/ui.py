from __future__ import annotations

import bisect
import os
import queue
import re
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .advice_display import project_advice
from .agent import DesktopAgent
from .backend import BackendError, BackendHTTPError
from .i18n import (
    get_locale,
    install_tk_localization,
    retranslate,
    set_locale,
    set_raw_widget_text,
    set_ui_variable,
    tr,
)
from .preferences import PreferencesController
from .security import WindowsDataProtector
from .settings import AppSettings, SettingsRepository, default_data_dir
from .startup import StartupManager
from .store import EventStore
from .tray import TrayIcon


install_tk_localization()

COLORS = {
    "window": "#eef2f0",
    "surface": "#ffffff",
    "header": "#17211d",
    "sidebar": "#22302a",
    "text": "#18201d",
    "muted": "#607069",
    "border": "#d5ddd9",
    "accent": "#247052",
    "accent_dark": "#18533c",
    "ok": "#247052",
    "warn": "#b56a1b",
    "danger": "#b23a3a",
}

_NOTIFICATION_STATE_KEY = "last_notified_advice"
_NOTIFICATION_DELIVERY_PREFIX = "advice_notification:"
_NOTIFICATION_WATERMARK_KEY = "advice_notification_watermark"
_NOTIFICATION_RATE_LIMIT_SECONDS = 5.0
_NOTIFICATION_RETRY_BASE_SECONDS = 15.0
_NOTIFICATION_RETRY_MAX_SECONDS = 5 * 60.0
_ADVICE_RECONCILE_LIMIT = 500
_LOCALLY_NEW_ADVICE_KEY = "_mouchen_locally_new"
_TERMINAL_ADVICE_STATUSES = {
    "adopted",
    "dismissed",
    "feedback",
    "expired",
    "superseded",
    "verified",
    "withdrawn",
}
_FEEDBACK_ACTION_LABELS = {
    "adopted": "采纳",
    "irrelevant": "无关",
    "fact_error": "事实有误",
    "later": "稍后处理",
    "acknowledged": "知道了",
    "guidance": "指导",
}


def _present_startup_dialog(window: tk.Toplevel, root: tk.Tk) -> None:
    """Show an account gate even while the main application window is hidden."""
    # On Windows, a transient dialog inherits the visibility of its owner.  The
    # main Mouchen window stays withdrawn until authentication and consent have
    # completed, so binding a startup dialog to it would make the gate
    # invisible while wait_window() blocks forever.
    if root.winfo_viewable():
        window.transient(root)
    window.update_idletasks()
    window.deiconify()
    window.lift()


_ANALYSIS_FAILURE_LABELS = {
    "openai_auth_failed": "OpenAI 凭据失效",
    "codex_cli_not_logged_in": "Codex 未登录",
    "codex_cli_model_unsupported": "Codex 模型不可用",
    "model_provider_invalid": "模型路由配置错误",
    "analysis_max_attempts_exhausted": "已达到最大重试次数",
    "worker_lease_expired": "分析进程曾中断",
    "analysis_processing_unavailable": "分析服务暂不可用",
}


def _notification_delivery_key(advice_id: str) -> str:
    return f"{_NOTIFICATION_DELIVERY_PREFIX}{advice_id}"


def _notification_timestamp(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError):
        return ""


def _analysis_state_text(status: dict[str, Any]) -> str:
    if status.get("consent_required"):
        return "等待采集授权 · 当前未采集、未上传"
    if status.get("observation_disabled"):
        return "信息来源已全部关闭 · 当前未采集、未上传"
    if status.get("paused"):
        return "观察已暂停"
    if status.get("online") is False:
        return "后端离线 · 主动分析未运行"
    if status.get("event_sync_deferred_for_quota"):
        return "事件同步暂缓 · 云端存储空间已满，将自动重试"

    analysis = status.get("analysis_status")
    if not isinstance(analysis, dict):
        return (
            "主动守候中 · 分析状态暂不可用"
            if status.get("analysis_status_error")
            else "主动守候中"
        )
    counts = analysis.get("counts") if isinstance(analysis.get("counts"), dict) else {}
    waiting_reasons = (
        analysis.get("waiting_reasons")
        if isinstance(analysis.get("waiting_reasons"), dict)
        else {}
    )
    try:
        waiting = int(analysis.get("waiting", 0))
        running = int(counts.get("running", 0))
        retrying = int(counts.get("retry", 0))
    except (TypeError, ValueError):
        waiting = running = retrying = 0
    last_status = str(analysis.get("last_status") or "")
    last_reason = str(analysis.get("last_reason") or "")
    failure_reason = str(analysis.get("last_failure_reason") or "")

    if running:
        return f"正在分析 · 队列 {waiting} 项"
    try:
        budget_waiting = int(waiting_reasons.get("analysis_budget_deferred", 0))
    except (TypeError, ValueError):
        budget_waiting = 0
    if waiting and budget_waiting == waiting:
        return f"分析排队中 · {waiting} 项 · 等待预算窗口"
    if waiting and budget_waiting:
        return f"分析排队中 · {waiting} 项 · 其中 {budget_waiting} 项等待预算"
    if waiting and retrying and failure_reason:
        label = _ANALYSIS_FAILURE_LABELS.get(failure_reason, "模型服务暂不可用")
        return f"分析重试中 · {waiting} 项 · {label}"
    if waiting:
        return f"分析排队中 · {waiting} 项"
    if last_status == "published":
        return "主动守候中 · 最近已生成建言"
    if last_status == "no_intervention":
        if last_reason in _ANALYSIS_FAILURE_LABELS or last_reason == "analysis_max_attempts_exhausted":
            reason = failure_reason or last_reason
            label = _ANALYSIS_FAILURE_LABELS.get(reason, "分析服务不可用")
            return f"最近分析失败 · {label}"
        return "主动守候中 · 最近一次未达到建言标准"
    if not last_status:
        return "主动守候中 · 尚无分析任务"
    return "主动守候中"


class LegacyOwnerAccountGate:
    """Require an explicit legacy-owner decision before observation starts."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.choice = "exit"
        self.window = tk.Toplevel(root)
        self.window.title("领取AI替身主人账号")
        self.window.geometry("510x380")
        self.window.resizable(False, False)
        self.window.configure(bg=COLORS["surface"])
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        _present_startup_dialog(self.window, root)
        self.window.grab_set()

        frame = ttk.Frame(self.window, padding=30)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="先领取主人账号", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "这台电脑仍在使用旧版单主人密钥。请为原有目标、记录和建言领取用户名与密码，"
                "以后即可在电脑和手机上登录同一账号。"
            ),
            style="Muted.TLabel",
            wraplength=440,
        ).pack(anchor="w", pady=(8, 16))
        ttk.Label(
            frame,
            text="领取成功会保留本机数据和已经授权的信息来源，不会把原有资料迁到其他账号。",
            wraplength=440,
        ).pack(anchor="w", pady=(0, 12))
        ttk.Label(
            frame,
            text="在你作出选择前，AI替身不会观察、同步或推送。",
            foreground=COLORS["warn"],
            wraplength=440,
        ).pack(anchor="w", pady=(0, 18))
        ttk.Button(
            frame,
            text="领取主人账号",
            style="Accent.TButton",
            command=lambda: self._select("claim"),
        ).pack(fill="x")
        ttk.Button(
            frame,
            text="本次暂时继续旧模式",
            command=lambda: self._select("continue"),
        ).pack(fill="x", pady=(10, 0))
        ttk.Label(
            frame,
            text="临时继续只对本次启动有效；下次启动仍会询问，旧模式关闭后将无法继续使用。",
            style="Muted.TLabel",
            wraplength=440,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Button(frame, text="退出AI替身", command=self.window.destroy).pack(anchor="e", pady=(14, 0))
        self.window.after(50, lambda: self.window.focus_force())

    def _select(self, choice: str) -> None:
        self.choice = choice
        self.window.destroy()

    def show(self) -> str:
        self.root.wait_window(self.window)
        return self.choice


class AccountDialog:
    """Small account gate shown before any observation or synchronization starts."""

    def __init__(
        self,
        root: tk.Tk,
        agent: DesktopAgent,
        initial_error: str = "",
        *,
        claim_legacy_owner: bool = False,
    ) -> None:
        self.root = root
        self.agent = agent
        self.claim_legacy_owner = claim_legacy_owner
        self.expected_legacy_user_id = (
            agent.settings.user_id.strip() if claim_legacy_owner else ""
        )
        self.accepted = False
        self.window = tk.Toplevel(root)
        self.window.title("领取AI替身主人账号" if claim_legacy_owner else "登录AI替身")
        self.window.geometry("470x500" if claim_legacy_owner else "440x440")
        self.window.resizable(False, False)
        self.window.configure(bg=COLORS["surface"])
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        _present_startup_dialog(self.window, root)
        self.window.grab_set()

        frame = ttk.Frame(self.window, padding=28)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="领取主人账号" if claim_legacy_owner else "登录AI替身",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "使用主人领取码为原有数据设置用户名和密码；如果已在其他设备领取，直接登录主人账号。"
                if claim_legacy_owner
                else "同一账号可在电脑和手机上共享目标、建言与反馈。"
            ),
            style="Muted.TLabel",
            wraplength=400 if claim_legacy_owner else 370,
        ).pack(anchor="w", pady=(4, 20))

        self.backend = tk.StringVar(value=agent.settings.backend_url)
        self.username = tk.StringVar(
            value=(agent.settings.session_username or "owner")
            if claim_legacy_owner
            else agent.settings.session_username
        )
        self.password = tk.StringVar()
        self.confirm = tk.StringVar()
        self.registration_code = tk.StringVar()
        self.status = tk.StringVar(value=initial_error)

        self.backend_entry = self._entry(frame, "服务地址", self.backend)
        if agent.settings.has_session_credentials() or claim_legacy_owner:
            self.backend_entry.configure(state="disabled")
        self._entry(frame, "用户名", self.username)
        self.password_entry = self._entry(frame, "密码", self.password, show="●")
        self.password_confirmation_entry = self._entry(
            frame,
            "注册时再次输入密码",
            self.confirm,
            show="●",
        )
        self._entry(
            frame,
            "主人领取码（领取时必填）" if claim_legacy_owner else "邀请码（创建账号时填写）",
            self.registration_code,
            show="●",
        )

        ttk.Label(
            frame,
            textvariable=self.status,
            foreground=COLORS["danger"],
            wraplength=370,
        ).pack(fill="x", pady=(4, 10))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(4, 0))
        self.login_button = ttk.Button(
            buttons,
            text="已有主人账号，登录" if claim_legacy_owner else "登录",
            command=lambda: self._submit(False),
        )
        self.login_button.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.register_button = ttk.Button(
            buttons,
            text="领取并继续" if claim_legacy_owner else "创建账号",
            style="Accent.TButton",
            command=lambda: self._submit(True),
        )
        self.register_button.pack(side="left", expand=True, fill="x", padx=(5, 0))
        ttk.Label(
            frame,
            text=(
                "普通邀请码不能领取原有数据；账号必须由服务器确认属于当前主人。"
                if claim_legacy_owner
                else "AI替身不会让您填写内部用户编号；账号归属由服务器登录会话确认。"
            ),
            style="Muted.TLabel",
            wraplength=370,
        ).pack(anchor="w", pady=(16, 0))
        self.window.bind("<Return>", lambda _: self._submit(False))
        self.window.after(50, lambda: self.window.focus_force())

    @staticmethod
    def _entry(
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        show: str = "",
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).pack(anchor="w", pady=(0, 3))
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.pack(fill="x", pady=(0, 10))
        return entry

    def show(self) -> bool:
        self.root.wait_window(self.window)
        return self.accepted

    def _submit(self, register: bool) -> None:
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            set_ui_variable(self.status, "请输入用户名和密码")
            return
        if register and password != self.confirm.get():
            set_ui_variable(self.status, "两次输入的密码不一致")
            return
        if register and len(password) < 10:
            set_ui_variable(self.status, "密码至少需要 10 个字符")
            return
        if register and self.claim_legacy_owner and not self.registration_code.get().strip():
            set_ui_variable(self.status, "请输入主人领取码")
            return
        try:
            settings = self.agent.settings
            settings.backend_url = self.backend.get().strip()
            settings.validate()
            self.agent.update_settings(settings)
            self._set_busy(True, "正在创建账号…" if register else "正在登录…")
            self.window.update_idletasks()
            self.agent.authenticate(
                username,
                password,
                register=register,
                registration_code=self.registration_code.get().strip() or None,
                expected_user_id=self.expected_legacy_user_id or None,
            )
        except Exception as exc:
            if (
                self.claim_legacy_owner
                and register
                and isinstance(exc, BackendHTTPError)
                and exc.status_code in {403, 409}
            ):
                message = "领取失败：请确认主人领取码和用户名；普通邀请码不能领取原有数据"
            else:
                message = self._friendly_error(exc, register)
            self._set_busy(False, message)
            return
        self.password.set("")
        self.confirm.set("")
        self.registration_code.set("")
        self.accepted = True
        self.window.destroy()

    def _set_busy(self, busy: bool, text: str) -> None:
        state = "disabled" if busy else "normal"
        self.login_button.configure(state=state)
        self.register_button.configure(state=state)
        set_ui_variable(self.status, text)

    @staticmethod
    def _friendly_error(exc: Exception, register: bool) -> str:
        if isinstance(exc, BackendHTTPError):
            if exc.status_code == 401:
                return "用户名或密码不正确"
            if exc.status_code == 403 and register:
                return "暂未开放自助注册，请联系管理员开通账号"
            if exc.status_code == 409 and register:
                return "该用户名不可用，请更换后重试"
            if exc.status_code >= 500:
                return "AI替身服务暂时不可用，请稍后重试"
        detail = str(exc)
        if "urlopen" in detail.casefold() or "connection" in detail.casefold():
            return "暂时无法连接AI替身服务，请检查服务地址和网络"
        return detail[:220]


class CollectionConsentDialog:
    """First-login consent gate for one authenticated Windows account."""

    def __init__(self, root: tk.Tk, agent: DesktopAgent) -> None:
        self.root = root
        self.agent = agent
        self.accepted = False
        self.window = tk.Toplevel(root)
        self.window.title("选择AI替身可以了解的信息")
        self.window.geometry("560x590")
        self.window.resizable(False, False)
        self.window.configure(bg=COLORS["surface"])
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        _present_startup_dialog(self.window, root)
        self.window.grab_set()

        frame = ttk.Frame(self.window, padding=28)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="先决定AI替身可以了解什么", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "新账号默认不采集、不上传。下面每项都可以单独开启，选择只对当前账号生效，"
                "以后可以在“信息来源”中随时关闭。"
            ),
            style="Muted.TLabel",
            wraplength=490,
        ).pack(anchor="w", pady=(6, 18))

        self.visible_interface = tk.BooleanVar(value=False)
        self.browser_activity = tk.BooleanVar(value=False)
        self.clipboard_text = tk.BooleanVar(value=False)
        self.file_content = tk.BooleanVar(value=False)
        self.cloud_analysis = tk.BooleanVar(value=False)
        choices = (
            (
                "可见界面",
                "读取当前前台应用名称和无障碍接口中可见的文字；不读取密码框。",
                self.visible_interface,
            ),
            (
                "浏览器活动",
                "读取 Chrome / Edge 已访问页面的标题和网站域名，不保存完整网址。",
                self.browser_activity,
            ),
            (
                "剪贴板文字",
                "观察之后新复制的文字；密码、密钥和支付号码会在本机过滤。",
                self.clipboard_text,
            ),
            (
                "授权文件内容",
                "仅观察你之后在AI替身中主动选择的文件夹；当前尚未选择任何文件夹。",
                self.file_content,
            ),
            (
                "主动云分析",
                "允许已勾选来源触发 AI 分析。开启本项不会自动开启任何信息来源。",
                self.cloud_analysis,
            ),
        )
        local_processing = os.environ.get("MOUCHEN_MODEL_PROVIDER") == "rules"
        for title, detail, variable in choices:
            if local_processing and variable is self.cloud_analysis:
                continue
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=6)
            ttk.Checkbutton(row, text=title, variable=variable).pack(anchor="w")
            ttk.Label(
                row,
                text=detail,
                style="Muted.TLabel",
                wraplength=455,
            ).pack(anchor="w", padx=(24, 0), pady=(2, 0))

        ttk.Label(
            frame,
            text=(
                "当前使用电脑本地处理。记录保存在这台电脑，不调用外部 AI 模型。"
                "可以全部不开启，先查看手机同步过来的目标和建议。"
            ) if local_processing else (
                "勾选信息来源表示该来源的数据会加密同步到你的AI替身账号。云分析关闭时，"
                "不会把这些事件交给外部 AI 模型；“允许远程全文”仍需在高级设置中另行开启。"
            ),
            foreground=COLORS["warn"],
            wraplength=490,
        ).pack(anchor="w", pady=(14, 12))
        self.status = tk.StringVar()
        ttk.Label(
            frame,
            textvariable=self.status,
            foreground=COLORS["danger"],
            wraplength=490,
        ).pack(fill="x")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(
            actions,
            text="全部不开启，仅查看",
            command=self._disable_all,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="确认并开始",
            style="Accent.TButton",
            command=self._save,
        ).pack(side="right")
        self.window.after(50, lambda: self.window.focus_force())

    def show(self) -> bool:
        self.root.wait_window(self.window)
        return self.accepted

    def _disable_all(self) -> None:
        for variable in (
            self.visible_interface,
            self.browser_activity,
            self.clipboard_text,
            self.file_content,
            self.cloud_analysis,
        ):
            variable.set(False)
        self._save()

    def _save(self) -> None:
        try:
            self.agent.apply_collection_consent(
                visible_interface=self.visible_interface.get(),
                browser_activity=self.browser_activity.get(),
                clipboard_text=self.clipboard_text.get(),
                file_content=self.file_content.get(),
                cloud_analysis=self.cloud_analysis.get(),
            )
        except Exception as exc:
            self.status.set(str(exc)[:220])
            return
        self.accepted = True
        self.window.destroy()


class MouchenWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AI替身 · 电脑本地处理" if os.environ.get("MOUCHEN_MODEL_PROVIDER") == "rules"
                        else "AI替身 · Windows 私测版")
        self.root.geometry("1120x760")
        self.root.minsize(940, 650)
        self.root.configure(bg=COLORS["window"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.withdraw()
        self._configure_style()

        data_dir = default_data_dir()
        protector = WindowsDataProtector()
        self.settings_repository = SettingsRepository(data_dir / "settings.json", protector)
        self.store = EventStore(data_dir / "mouchen-desktop.db", protector)
        self.startup_manager = StartupManager()
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.agent = DesktopAgent(
            self.settings_repository,
            self.store,
            on_status=lambda value: self.ui_queue.put(("status", value)),
            on_event=lambda value: self.ui_queue.put(("event", value)),
            on_advice=lambda value: self._queue_session_event("advice", value),
            on_attention=lambda value: self._queue_session_event("attention", value),
        )
        set_locale(self.agent.settings.locale)
        retranslate(self.root)
        if not self._ensure_account():
            self.store.close()
            self.root.destroy()
            return
        # A validated or newly created session may carry the account-wide
        # locale. Apply it before the consent gate and main shell are shown.
        set_locale(self.agent.settings.locale)
        retranslate(self.root)
        if not self._ensure_collection_consent():
            self.store.close()
            self.root.destroy()
            return
        self.tray = TrayIcon()
        self.tray.start()
        self.goals: list[dict[str, Any]] = []
        self.preferences = PreferencesController()
        self._pref_syncing = False
        self._pref_busy = False
        self.selected_advice_id: str | None = None
        self._feedback_inflight = False
        self._feedback_status_advice_id: str | None = None
        self._advice_popups: list[tk.Toplevel] = []
        self._visible_advice_popup_ids: set[str] = set()
        self._advice_popups_by_id: dict[str, tk.Toplevel] = {}
        self._notification_queue: list[str] = []
        self._queued_advice_ids: set[str] = set()
        self._attention_claims: dict[str, dict[str, Any]] = {}
        self._notification_watermark: tuple[str, str] = ("", "")
        self._notification_startup_watermark: tuple[str, str] = ("", "")
        self._notification_next_dispatch_at = 0.0
        self._notification_initialized = False
        self._last_status: dict[str, Any] = {}
        self._account_locale_generation = 0
        self._account_locale_update_pending = False

        self._build_shell()
        retranslate(self.root)
        self._load_settings_controls(self.agent.settings)
        self._initialize_notification_queue()
        self.agent.start()
        self._load_goals()
        self._load_advice_preferences()
        self._refresh_local_views()
        self.root.after(200, self._drain_queue)
        self.root.after(350, self._drain_tray)
        self.root.after(800, self._process_notification_queue)
        self.root.after(2_000, self._periodic_refresh)
        self.root.deiconify()

    def _ensure_account(self) -> bool:
        settings = self.agent.settings
        if settings.auth_mode == "legacy":
            choice = LegacyOwnerAccountGate(self.root).show()
            if choice == "continue":
                return True
            if choice != "claim":
                return False
            return AccountDialog(
                self.root,
                self.agent,
                claim_legacy_owner=True,
            ).show()
        error = ""
        if settings.has_session_credentials():
            try:
                self.agent.validate_session()
                return True
            except Exception as exc:
                invalid = (
                    isinstance(exc, BackendHTTPError) and exc.status_code in {401, 403}
                ) or (
                    isinstance(exc, BackendError)
                    and any(marker in str(exc) for marker in ("登录状态已失效", "账号会话不一致"))
                )
                if not invalid:
                    # A temporary network or server outage must not erase a
                    # valid encrypted session or the user's offline queue.
                    return True
                error = f"登录已失效，请重新登录：{str(exc)[:120]}"
                # Keep the last server-derived identity while the login window
                # is shown. Re-authenticating the same account then preserves
                # unsent offline events; signing into another account still
                # clears the cache after the new identity is confirmed.
                settings.clear_session(preserve_identity=True)
                self.agent.update_settings(settings)
        return AccountDialog(self.root, self.agent, error).show()

    def _ensure_collection_consent(self) -> bool:
        settings = self.agent.settings
        if settings.auth_mode == "legacy":
            return True
        if settings.has_current_collection_consent():
            # Re-apply this account's source profile in case another account
            # was previously used on the same Windows installation.
            settings.restore_current_collection_consent()
            self.agent.update_settings(settings)
            return True
        return CollectionConsentDialog(self.root, self.agent).show()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background=COLORS["surface"])
        style.configure("App.TFrame", background=COLORS["window"])
        style.configure("Header.TFrame", background=COLORS["header"])
        style.configure("Header.TLabel", background=COLORS["header"], foreground="#ffffff")
        style.configure("Title.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Section.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Muted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        style.configure("NotificationError.TLabel", background=COLORS["surface"], foreground=COLORS["danger"])
        style.configure("Metric.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Segoe UI", 22, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", foreground=[("active", COLORS["accent_dark"])])
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook", background=COLORS["window"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), font=("Microsoft YaHei UI", 10))

    def _build_shell(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", height=68)
        header.pack(fill="x")
        header.pack_propagate(False)
        brand = ttk.Label(header, text="AI替身", style="Header.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        brand.pack(side="left", padx=(24, 10), pady=18)
        ttk.Label(header, text="事业助手 · Windows Private Alpha", style="Header.TLabel", font=("Microsoft YaHei UI", 10)).pack(side="left", pady=22)
        self.pause_button = ttk.Button(header, text="暂停观察", command=self._toggle_pause)
        self.pause_button.pack(side="right", padx=(8, 22), pady=18)
        self.connection_label = ttk.Label(header, text="后端检测中", style="Header.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        self.connection_label.pack(side="right", padx=8, pady=22)

        body = ttk.Frame(self.root, style="App.TFrame")
        body.pack(fill="both", expand=True, padx=16, pady=14)
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        self.overview_tab = ttk.Frame(self.tabs, padding=18)
        self.advice_tab = ttk.Frame(self.tabs, padding=18)
        self.goals_tab = ttk.Frame(self.tabs, padding=18)
        self.preferences_tab = ttk.Frame(self.tabs, padding=18)
        self.sources_tab = ttk.Frame(self.tabs, padding=18)
        self.activity_tab = ttk.Frame(self.tabs, padding=18)
        self.settings_tab = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.overview_tab, text="总览")
        self.tabs.add(self.advice_tab, text="建言")
        self.tabs.add(self.goals_tab, text="目标")
        self.tabs.add(self.preferences_tab, text="AI替身偏好")
        self.tabs.add(self.sources_tab, text="信息源")
        self.tabs.add(self.activity_tab, text="活动")
        self.tabs.add(self.settings_tab, text="设置")
        self._build_overview()
        self._build_advice()
        self._build_goals()
        self._build_preferences()
        self._build_sources()
        self._build_activity()
        self._build_settings()

    def _build_overview(self) -> None:
        self.overview_tab.columnconfigure(0, weight=3)
        self.overview_tab.columnconfigure(1, weight=2)
        self.overview_tab.rowconfigure(2, weight=1)

        metrics = ttk.Frame(self.overview_tab)
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        for index in range(3):
            metrics.columnconfigure(index, weight=1)
        self.metric_events = self._metric(metrics, 0, "近 24 小时信号")
        self.metric_pending = self._metric(metrics, 1, "待同步")
        self.metric_advice = self._metric(metrics, 2, "累计建言")

        problem = ttk.LabelFrame(self.overview_tab, text="当前问题", padding=14)
        problem.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        problem.columnconfigure(0, weight=1)
        self.problem_text = tk.Text(
            problem,
            height=4,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
        )
        self.problem_text.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(problem, text="关联目标").grid(row=1, column=0, sticky="w")
        self.problem_domain = tk.StringVar()
        self.problem_domain_box = ttk.Combobox(problem, textvariable=self.problem_domain, state="readonly", width=28)
        self.problem_domain_box.grid(row=1, column=1, sticky="w", padx=8)
        ttk.Button(problem, text="提交分析", style="Accent.TButton", command=self._submit_problem).grid(row=1, column=2, sticky="e")

        left = ttk.LabelFrame(self.overview_tab, text="最近建言", padding=10)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.overview_advice = ttk.Treeview(left, columns=("level", "domain", "action"), show="headings", height=10)
        self.overview_advice.heading("level", text="级别")
        self.overview_advice.heading("domain", text="领域")
        self.overview_advice.heading("action", text="建议")
        self.overview_advice.column("level", width=58, anchor="center")
        self.overview_advice.column("domain", width=100)
        self.overview_advice.column("action", width=470)
        self.overview_advice.grid(row=0, column=0, sticky="nsew")
        self.overview_advice.bind("<Double-1>", lambda _: self._open_selected_overview_advice())

        right = ttk.LabelFrame(self.overview_tab, text="正在观察", padding=14)
        right.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)
        self.current_app = ttk.Label(right, text="尚无活动窗口", style="Section.TLabel", wraplength=340)
        self.current_app.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.current_title = ttk.Label(right, text="", style="Muted.TLabel", wraplength=340)
        self.current_title.grid(row=1, column=0, sticky="nw")
        ttk.Separator(right).grid(row=2, column=0, sticky="ew", pady=16)
        self.agent_state = ttk.Label(
            right,
            text="守候启动中",
            style="Muted.TLabel",
            wraplength=340,
        )
        self.agent_state.grid(row=3, column=0, sticky="w")

    def _metric(self, parent: ttk.Frame, column: int, title: str) -> ttk.Label:
        frame = ttk.LabelFrame(parent, text=title, padding=(16, 10))
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        value = ttk.Label(frame, text="0", style="Metric.TLabel")
        value.pack(anchor="w")
        return value

    def _build_advice(self) -> None:
        self.advice_tab.rowconfigure(1, weight=3)
        self.advice_tab.rowconfigure(2, weight=2)
        self.advice_tab.columnconfigure(0, weight=1)
        self.notification_status = ttk.Label(
            self.advice_tab,
            text="通知状态：暂无投递记录",
            style="Muted.TLabel",
        )
        self.notification_status.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        columns = ("time", "level", "domain", "action", "status")
        self.advice_tree = ttk.Treeview(self.advice_tab, columns=columns, show="headings")
        headings = {"time": "时间", "level": "级别", "domain": "领域", "action": "建议", "status": "状态"}
        widths = {"time": 130, "level": 60, "domain": 110, "action": 580, "status": 80}
        for key in columns:
            self.advice_tree.heading(key, text=headings[key])
            self.advice_tree.column(key, width=widths[key], anchor="center" if key in {"level", "status"} else "w")
        self.advice_tree.grid(row=1, column=0, sticky="nsew")
        self.advice_tree.bind("<<TreeviewSelect>>", self._show_advice_detail)
        detail_frame = ttk.LabelFrame(self.advice_tab, text="建言详情", padding=12)
        detail_frame.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.advice_detail = tk.Text(
            detail_frame, height=7, wrap="word", font=("Microsoft YaHei UI", 10), relief="flat", state="disabled"
        )
        self.advice_detail.grid(row=0, column=0, columnspan=4, sticky="nsew")

        reply_frame = ttk.Frame(detail_frame)
        reply_frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        reply_frame.columnconfigure(0, weight=1)
        ttk.Label(
            reply_frame,
            text="回复与方向",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.feedback_note = tk.Text(
            reply_frame,
            height=3,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            relief="solid",
            borderwidth=1,
            state="disabled",
        )
        self.feedback_note.grid(row=1, column=0, sticky="ew")
        self.guidance_button = ttk.Button(
            reply_frame,
            text="发送指导",
            command=lambda: self._feedback("guidance"),
            state="disabled",
        )
        self.guidance_button.grid(row=1, column=1, sticky="ns", padx=(8, 0))

        action_frame = ttk.Frame(detail_frame)
        action_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.feedback_buttons: dict[str, ttk.Button] = {}
        for kind, label in (
            ("adopted", "采纳"),
            ("irrelevant", "无关"),
            ("fact_error", "事实有误"),
            ("later", "稍后处理"),
            ("acknowledged", "知道了"),
        ):
            button = ttk.Button(
                action_frame,
                text=label,
                command=lambda value=kind: self._feedback(value),
                state="disabled",
            )
            button.pack(side="left", padx=(0, 8))
            self.feedback_buttons[kind] = button
        self.feedback_status = ttk.Label(detail_frame, text="请选择一条建言", style="Muted.TLabel")
        self.feedback_status.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(7, 0))

    def _build_goals(self) -> None:
        self.goals_tab.columnconfigure(0, weight=2)
        self.goals_tab.columnconfigure(1, weight=3)
        self.goals_tab.rowconfigure(0, weight=1)
        list_frame = ttk.LabelFrame(self.goals_tab, text="目标坐标", padding=10)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.goal_tree = ttk.Treeview(list_frame, columns=("domain", "title"), show="headings")
        self.goal_tree.heading("domain", text="领域")
        self.goal_tree.heading("title", text="目标")
        self.goal_tree.column("domain", width=100)
        self.goal_tree.column("title", width=280)
        self.goal_tree.grid(row=0, column=0, sticky="nsew")
        self.goal_tree.bind("<<TreeviewSelect>>", self._show_goal)
        ttk.Button(list_frame, text="刷新", command=self._load_goals).grid(row=1, column=0, sticky="w", pady=(10, 0))

        form = ttk.LabelFrame(self.goals_tab, text="新增目标", padding=14)
        form.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        form.columnconfigure(1, weight=1)
        self.goal_domain = tk.StringVar(value="work")
        self.goal_title = tk.StringVar()
        self.goal_keywords = tk.StringVar()
        self.goal_packages = tk.StringVar()
        self.goal_hours = tk.StringVar(value="10")
        self._form_row(form, 0, "领域", ttk.Entry(form, textvariable=self.goal_domain))
        self._form_row(form, 1, "标题", ttk.Entry(form, textvariable=self.goal_title))
        ttk.Label(form, text="主公原话").grid(row=2, column=0, sticky="nw", pady=8)
        self.goal_quote = tk.Text(form, height=6, wrap="word", font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        self.goal_quote.grid(row=2, column=1, sticky="ew", pady=8)
        self._form_row(form, 3, "关键词", ttk.Entry(form, textvariable=self.goal_keywords))
        self._form_row(form, 4, "应用进程", ttk.Entry(form, textvariable=self.goal_packages))
        self._form_row(form, 5, "每周小时", ttk.Entry(form, textvariable=self.goal_hours, width=12), sticky="w")
        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=1, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="清空", command=self._clear_goal_form).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="保存目标", style="Accent.TButton", command=self._save_goal).pack(side="left")

    def _build_preferences(self) -> None:
        tab = self.preferences_tab
        tab.columnconfigure(0, weight=1)
        ttk.Label(tab, text="AI替身偏好", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            tab,
            text="长期设置：告诉AI替身观察什么、多久开口。针对单条建言的反馈请继续在“建言”页使用文字指导。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        scope_row = ttk.Frame(tab)
        scope_row.grid(row=2, column=0, sticky="w", pady=(0, 8))
        self.pref_scope = tk.StringVar(value="global")
        ttk.Radiobutton(
            scope_row,
            text="全局",
            value="global",
            variable=self.pref_scope,
            command=self._on_pref_scope_change,
        ).pack(side="left")
        ttk.Radiobutton(
            scope_row,
            text="指定目标",
            value="goal",
            variable=self.pref_scope,
            command=self._on_pref_scope_change,
        ).pack(side="left", padx=(12, 8))
        self.pref_goal = tk.StringVar(value="")
        self.pref_goal_box = ttk.Combobox(
            scope_row, textvariable=self.pref_goal, state="disabled", width=36
        )
        self.pref_goal_box.pack(side="left")
        self.pref_goal_box.bind("<<ComboboxSelected>>", self._on_pref_goal_change)
        self.pref_goal_empty = ttk.Label(
            scope_row, text="", style="Muted.TLabel"
        )
        self.pref_goal_empty.pack(side="left", padx=(8, 0))

        ttk.Label(tab, text="建议方向（AI替身重点观察和建议的角度）", style="Section.TLabel").grid(
            row=3, column=0, sticky="w"
        )
        self.pref_direction = tk.Text(tab, height=5, wrap="word")
        self.pref_direction.grid(row=4, column=0, sticky="ew", pady=(2, 6))
        self.pref_direction.bind("<KeyRelease>", self._on_pref_direction_change)

        self.pref_direction_mode_row = ttk.Frame(tab)
        self.pref_direction_mode_row.grid(row=5, column=0, sticky="w", pady=(0, 8))
        self.pref_direction_mode = tk.StringVar(value="inherit")
        for value, label in (
            ("inherit", "只用全局方向"),
            ("append", "全局方向后追加（冲突时目标优先）"),
            ("replace", "只用目标方向"),
        ):
            ttk.Radiobutton(
                self.pref_direction_mode_row,
                text=label,
                value=value,
                variable=self.pref_direction_mode,
                command=self._on_pref_field_change,
            ).pack(side="left", padx=(0, 10))

        frequency_row = ttk.Frame(tab)
        frequency_row.grid(row=6, column=0, sticky="w", pady=(0, 8))
        ttk.Label(frequency_row, text="建议频率", style="Section.TLabel").pack(side="left")
        self.pref_inherit_frequency = tk.BooleanVar(value=True)
        self.pref_inherit_frequency_box = ttk.Checkbutton(
            frequency_row,
            text="继承全局频率",
            variable=self.pref_inherit_frequency,
            command=self._on_pref_field_change,
        )
        self.pref_inherit_frequency_box.pack(side="left", padx=(12, 8))
        self.pref_frequency = tk.StringVar(value="")
        self.pref_frequency_box = ttk.Combobox(
            frequency_row, textvariable=self.pref_frequency, state="readonly", width=18
        )
        self.pref_frequency_box.pack(side="left")
        self.pref_frequency_box.bind("<<ComboboxSelected>>", self._on_pref_field_change)

        events_head = ttk.Frame(tab)
        events_head.grid(row=7, column=0, sticky="w")
        ttk.Label(events_head, text="建议事件类型（AI替身据此主动出言；不影响采集）", style="Section.TLabel").pack(
            side="left"
        )
        self.pref_inherit_events = tk.BooleanVar(value=True)
        self.pref_inherit_events_box = ttk.Checkbutton(
            events_head,
            text="继承全局事件类型",
            variable=self.pref_inherit_events,
            command=self._on_pref_field_change,
        )
        self.pref_inherit_events_box.pack(side="left", padx=(12, 0))
        self.pref_events_frame = ttk.Frame(tab)
        self.pref_events_frame.grid(row=8, column=0, sticky="w", pady=(4, 8))
        self.pref_event_vars: dict[str, tk.BooleanVar] = {}
        self.pref_event_boxes: dict[str, ttk.Checkbutton] = {}

        buttons = ttk.Frame(tab)
        buttons.grid(row=9, column=0, sticky="w", pady=(4, 4))
        self.pref_save_button = ttk.Button(
            buttons, text="保存", style="Accent.TButton", command=self._pref_save
        )
        self.pref_save_button.pack(side="left")
        self.pref_restore_button = ttk.Button(
            buttons, text="恢复全局设置", command=self._pref_restore_global
        )
        self.pref_restore_button.pack(side="left", padx=(10, 0))
        self.pref_status = ttk.Label(tab, text="正在加载偏好…", style="Muted.TLabel")
        self.pref_status.grid(row=10, column=0, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------------
    # preferences: controller <-> widgets
    # ------------------------------------------------------------------

    def _load_advice_preferences(self, *, refresh: bool = False) -> None:
        self._run_task(
            "advice_preferences_load",
            self.agent.advice_preferences,
            context={
                "refresh": refresh,
                "locale": self.agent.settings.locale,
                "session_context": self.agent.ui_session_context(),
            },
        )

    def _rebuild_pref_event_boxes(
        self, options: list[tuple[str, str]] | None = None
    ) -> None:
        for child in self.pref_events_frame.winfo_children():
            child.destroy()
        self.pref_event_vars = {}
        self.pref_event_boxes = {}
        options = self.preferences.catalog_options() if options is None else options
        for index, (identifier, label) in enumerate(options):
            variable = tk.BooleanVar(value=False)
            box = ttk.Checkbutton(
                self.pref_events_frame,
                text=label,
                variable=variable,
                command=lambda item=identifier: self._on_pref_event_toggle(item),
            )
            box.grid(row=index // 5, column=index % 5, sticky="w", padx=(0, 14), pady=2)
            self.pref_event_vars[identifier] = variable
            self.pref_event_boxes[identifier] = box

    def _sync_pref_event_boxes(self, options: list[tuple[str, str]]) -> None:
        identifiers = {identifier for identifier, _ in options}
        if set(self.pref_event_vars) != identifiers:
            self._rebuild_pref_event_boxes(options)
            return
        # IDs remain stable across languages, so matching IDs alone cannot
        # prove that the visible labels use the current locale.
        for identifier, label in options:
            box = self.pref_event_boxes.get(identifier)
            if box is not None:
                box.configure(text=label)

    def _sync_preferences_widgets(self) -> None:
        controller = self.preferences
        self._pref_syncing = True
        try:
            self._sync_pref_event_boxes(controller.catalog_options())
            goal_options = controller.goal_options()
            self.pref_goal_box.configure(
                values=[label for _, label in goal_options]
            )
            self.pref_goal_empty.configure(
                text="（还没有目标，可先设置全局偏好）"
                if controller.scope == "goal" and not goal_options
                else ""
            )
            self.pref_scope.set("goal" if controller.scope == "goal" else "global")
            if controller.scope == "goal" and controller.selected_goal_id:
                for identifier, label in goal_options:
                    if identifier == controller.selected_goal_id:
                        self.pref_goal.set(label)
                        break
            elif controller.scope == "goal":
                self.pref_goal.set("")
            form = controller.form
            self.pref_direction.delete("1.0", "end")
            if form.direction:
                self.pref_direction.insert("1.0", form.direction)
            self.pref_direction_mode.set(form.direction_mode)
            self.pref_inherit_frequency.set(form.inherit_frequency)
            frequency_labels = {
                mode: tr(label) for mode, label in controller.frequency_options()
            }
            self.pref_frequency_box.configure(
                values=list(frequency_labels.values())
            )
            self.pref_frequency.set(frequency_labels.get(form.frequency_mode, ""))
            self.pref_inherit_events.set(form.inherit_events)
            selected = set(form.event_types)
            for identifier, variable in self.pref_event_vars.items():
                variable.set(identifier in selected)
            self._apply_pref_widget_states()
            self.pref_status.configure(
                text=controller.status_text or ("未保存的修改" if controller.is_dirty() else ""),
                style=(
                    "NotificationError.TLabel"
                    if controller.status_level == "error"
                    else "Muted.TLabel"
                ),
            )
        finally:
            self._pref_syncing = False

    def _apply_pref_widget_states(self) -> None:
        controller = self.preferences
        busy = self._pref_busy
        goal_mode = controller.scope == "goal"
        has_goal = bool(controller.selected_goal_id)
        form_ready = controller.loaded and (not goal_mode or has_goal)
        base_state = "normal" if (form_ready and not busy) else "disabled"
        self.pref_direction.configure(state=base_state)
        self.pref_goal_box.configure(
            state="readonly" if (goal_mode and not busy and controller.goal_options()) else "disabled"
        )
        for child in self.pref_direction_mode_row.winfo_children():
            child.configure(state="normal" if (goal_mode and form_ready and not busy) else "disabled")
        self.pref_inherit_frequency_box.configure(
            state="normal" if (goal_mode and form_ready and not busy) else "disabled"
        )
        frequency_enabled = form_ready and not busy and (
            not goal_mode or not controller.form.inherit_frequency
        )
        self.pref_frequency_box.configure(
            state="readonly" if frequency_enabled else "disabled"
        )
        self.pref_inherit_events_box.configure(
            state="normal" if (goal_mode and form_ready and not busy) else "disabled"
        )
        events_enabled = form_ready and not busy and (
            not goal_mode or not controller.form.inherit_events
        )
        for box in self.pref_event_boxes.values():
            box.configure(state="normal" if events_enabled else "disabled")
        self.pref_save_button.configure(
            state="normal" if (form_ready and not busy) else "disabled"
        )
        self.pref_restore_button.configure(
            state="normal"
            if (goal_mode and has_goal and controller.form.has_override and not busy)
            else "disabled"
        )

    def _confirm_discard_pref_changes(self) -> bool:
        return messagebox.askyesno(
            "未保存的修改",
            "当前范围有未保存的偏好修改，切换后将丢失。仍要切换吗？",
            parent=self.root,
        )

    def _on_pref_scope_change(self) -> None:
        if self._pref_syncing:
            return
        controller = self.preferences
        target = self.pref_scope.get()
        goal_id = controller.selected_goal_id
        if target == "goal" and goal_id is None:
            options = controller.goal_options()
            goal_id = options[0][0] if options else None
        outcome = controller.select_scope(target, goal_id)
        if outcome == "confirm_required":
            if self._confirm_discard_pref_changes():
                controller.select_scope(target, goal_id, confirmed=True)
            # else: fall through and re-sync widgets back to the current scope
        self._sync_preferences_widgets()

    def _on_pref_goal_change(self, _event: Any = None) -> None:
        if self._pref_syncing:
            return
        controller = self.preferences
        label = self.pref_goal.get()
        goal_id = next(
            (
                identifier
                for identifier, option_label in controller.goal_options()
                if option_label == label
            ),
            None,
        )
        if goal_id is None:
            return
        outcome = controller.select_scope("goal", goal_id)
        if outcome == "confirm_required":
            if self._confirm_discard_pref_changes():
                controller.select_scope("goal", goal_id, confirmed=True)
        self._sync_preferences_widgets()

    def _on_pref_direction_change(self, _event: Any = None) -> None:
        if self._pref_syncing:
            return
        self.preferences.form.direction = self.pref_direction.get("1.0", "end-1c")

    def _on_pref_field_change(self, _event: Any = None) -> None:
        if self._pref_syncing:
            return
        controller = self.preferences
        controller.form.direction_mode = self.pref_direction_mode.get()
        controller.form.inherit_frequency = bool(self.pref_inherit_frequency.get())
        label_to_mode = {
            tr(label): mode for mode, label in controller.frequency_options()
        }
        selected = label_to_mode.get(self.pref_frequency.get())
        if selected is not None:
            controller.form.frequency_mode = selected
        controller.form.inherit_events = bool(self.pref_inherit_events.get())
        self._apply_pref_widget_states()

    def _on_pref_event_toggle(self, identifier: str) -> None:
        if self._pref_syncing:
            return
        variable = self.pref_event_vars.get(identifier)
        if variable is None:
            return
        self.preferences.toggle_event_type(identifier, bool(variable.get()))

    def _set_preferences_busy(self, busy: bool, message: str = "") -> None:
        self._pref_busy = busy
        if message:
            self.pref_status.configure(text=message, style="Muted.TLabel")
        self._apply_pref_widget_states()

    def _pref_save(self) -> None:
        if self._pref_busy:
            return
        self._on_pref_direction_change()
        self._on_pref_field_change()
        request = self.preferences.build_save_request()
        session_context = self.agent.ui_session_context()
        self.preferences.saving = True
        self._set_preferences_busy(True, "正在保存偏好…")
        self._run_task(
            "advice_preferences_save",
            lambda: self.agent.save_advice_preference(
                request["goal_id"],
                request["body"],
                expected_session_context=session_context,
            ),
            context={
                "scope_key": request["scope_key"],
                "revision": request["revision"],
                "session_context": session_context,
            },
        )

    def _pref_restore_global(self) -> None:
        if self._pref_busy:
            return
        request = self.preferences.build_delete_request()
        if request is None:
            return
        if not messagebox.askyesno(
            "恢复全局设置",
            "删除该目标的覆盖设置并立即恢复全局偏好？",
            parent=self.root,
        ):
            return
        session_context = self.agent.ui_session_context()
        self._set_preferences_busy(True, "正在恢复全局设置…")
        self._run_task(
            "advice_preferences_delete",
            lambda: self.agent.delete_goal_advice_preference(
                request["goal_id"],
                expected_session_context=session_context,
            ),
            context={
                "scope_key": request["scope_key"],
                "session_context": session_context,
            },
        )

    def _handle_preferences_task(self, name: str, result: dict[str, Any]) -> None:
        controller = self.preferences
        value = result.get("value")
        context = result.get("context") if isinstance(result.get("context"), dict) else {}
        if name == "advice_preferences_load":
            if (
                str(context.get("locale") or "") != self.agent.settings.locale
                or not self.agent.ui_session_context_is_current(
                    context.get("session_context")
                )
            ):
                return
            controller.load_snapshot(
                value if isinstance(value, dict) else {},
                keep_unsaved=bool(context.get("refresh")),
            )
            controller.status_text = ""
            self._sync_preferences_widgets()
            return
        if not self.agent.ui_session_context_is_current(
            context.get("session_context")
        ):
            return
        scope_key = str(context.get("scope_key", ""))
        if name == "advice_preferences_save":
            applied = controller.apply_save_success(
                scope_key, value if isinstance(value, dict) else {}
            )
        else:
            applied = controller.apply_delete_success(
                scope_key, value if isinstance(value, dict) else {}
            )
        self._pref_busy = False
        if applied:
            self._sync_preferences_widgets()
        else:
            # A response for a scope the user has left: cache is updated, the
            # current form is intentionally untouched.
            self._apply_pref_widget_states()

    def _handle_preferences_task_error(self, name: str, result: dict[str, Any]) -> None:
        controller = self.preferences
        context = result.get("context") if isinstance(result.get("context"), dict) else {}
        error = str(result.get("error", "未知错误"))
        exception = result.get("exception")
        if name == "advice_preferences_load":
            if (
                str(context.get("locale") or "") != self.agent.settings.locale
                or not self.agent.ui_session_context_is_current(
                    context.get("session_context")
                )
            ):
                return
            controller.status_text = f"偏好加载失败:{error[:120]}"
            controller.status_level = "error"
            if hasattr(self, "pref_status"):
                self.pref_status.configure(
                    text=controller.status_text, style="NotificationError.TLabel"
                )
            return
        if not self.agent.ui_session_context_is_current(
            context.get("session_context")
        ):
            return
        scope_key = str(context.get("scope_key", ""))
        conflict_current = None
        if isinstance(exception, BackendHTTPError) and exception.status_code == 409:
            detail = exception.detail if isinstance(exception.detail, dict) else {}
            current = detail.get("current")
            conflict_current = current if isinstance(current, dict) else None
        applied = controller.apply_save_failure(
            scope_key, error[:160], conflict_current=conflict_current
        )
        self._pref_busy = False
        if applied:
            self._sync_preferences_widgets()
        else:
            self._apply_pref_widget_states()

    def _build_sources(self) -> None:
        self.sources_tab.columnconfigure(0, weight=1)
        self.sources_tab.rowconfigure(1, weight=1)
        switches = ttk.LabelFrame(self.sources_tab, text="采集开关", padding=14)
        switches.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.var_collection = tk.BooleanVar()
        self.var_active = tk.BooleanVar()
        self.var_visible_text = tk.BooleanVar()
        self.var_browser_history = tk.BooleanVar()
        self.var_clipboard = tk.BooleanVar()
        self.var_files = tk.BooleanVar()
        self.var_file_content = tk.BooleanVar()
        checks = [
            ("启用主动观察", self.var_collection),
            ("活动窗口与应用时长", self.var_active),
            ("当前应用可见文字", self.var_visible_text),
            ("Chrome / Edge 浏览活动", self.var_browser_history),
            ("剪贴板文本", self.var_clipboard),
            ("授权文件夹变化", self.var_files),
            ("读取文本文件内容", self.var_file_content),
        ]
        for index, (label, variable) in enumerate(checks):
            ttk.Checkbutton(switches, text=label, variable=variable).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 28), pady=5)

        folders = ttk.LabelFrame(self.sources_tab, text="授权文件夹", padding=12)
        folders.grid(row=1, column=0, sticky="nsew")
        folders.rowconfigure(0, weight=1)
        folders.columnconfigure(0, weight=1)
        self.folder_list = tk.Listbox(folders, font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        self.folder_list.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(folders, text="添加文件夹", command=self._add_folder).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Button(folders, text="移除", command=self._remove_folder).grid(row=1, column=1, sticky="w", padx=8, pady=(10, 0))
        ttk.Button(folders, text="保存信息源", style="Accent.TButton", command=self._save_sources).grid(row=1, column=2, sticky="e", pady=(10, 0))

    def _build_activity(self) -> None:
        self.activity_tab.rowconfigure(0, weight=1)
        self.activity_tab.columnconfigure(0, weight=1)
        columns = ("time", "source", "type", "state", "summary")
        self.activity_tree = ttk.Treeview(self.activity_tab, columns=columns, show="headings")
        headings = {"time": "时间", "source": "来源", "type": "类型", "state": "同步", "summary": "摘要"}
        widths = {"time": 130, "source": 165, "type": 165, "state": 70, "summary": 500}
        for key in columns:
            self.activity_tree.heading(key, text=headings[key])
            self.activity_tree.column(key, width=widths[key], anchor="center" if key == "state" else "w")
        self.activity_tree.grid(row=0, column=0, sticky="nsew")
        ttk.Button(self.activity_tab, text="刷新", command=self._refresh_local_views).grid(row=1, column=0, sticky="w", pady=(10, 0))

    def _build_settings(self) -> None:
        self.settings_tab.columnconfigure(0, weight=0)
        self.settings_tab.columnconfigure(1, weight=1)
        self.var_backend = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_cloud = tk.BooleanVar()
        self.var_notifications = tk.BooleanVar()
        self.var_startup = tk.BooleanVar()
        self.var_remote_full = tk.BooleanVar()
        self.var_poll = tk.StringVar()
        self.var_sync = tk.StringVar()
        self.var_review = tk.StringVar()
        self.var_locale = tk.StringVar()

        self.language_panel = ttk.LabelFrame(
            self.settings_tab,
            text="语言 / Language",
            padding=(14, 10),
        )
        self.language_panel.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, 10),
        )
        self.language_panel.columnconfigure(1, weight=1)
        ttk.Label(
            self.language_panel,
            text="界面与建言语言",
            style="Section.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 16))
        self.locale_box = ttk.Combobox(
            self.language_panel,
            textvariable=self.var_locale,
            values=("简体中文", "English"),
            state="readonly",
            width=20,
        )
        self.locale_box.grid(row=0, column=1, sticky="w")
        self.locale_box.bind("<<ComboboxSelected>>", self._on_locale_change)
        ttk.Label(
            self.language_panel,
            text="界面立即切换，后续建言按此语言输出；该偏好同步到账号",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        self.settings_backend_entry = ttk.Entry(
            self.settings_tab,
            textvariable=self.var_backend,
        )
        self._settings_row(1, "服务地址", self.settings_backend_entry)
        self.account_name = ttk.Label(self.settings_tab, text="", style="Section.TLabel")
        self._settings_row(2, "当前账号", self.account_name)
        self._settings_row(3, "采集间隔（秒）", ttk.Entry(self.settings_tab, textvariable=self.var_poll, width=12), sticky="w")
        self._settings_row(4, "同步间隔（秒）", ttk.Entry(self.settings_tab, textvariable=self.var_sync, width=12), sticky="w")
        self._settings_row(5, "主动复盘间隔（分钟）", ttk.Entry(self.settings_tab, textvariable=self.var_review, width=12), sticky="w")
        toggles = ttk.Frame(self.settings_tab)
        toggles.grid(row=6, column=1, sticky="w", pady=8)
        if os.environ.get("MOUCHEN_MODEL_PROVIDER") != "rules":
            ttk.Checkbutton(toggles, text="主动云分析", variable=self.var_cloud).pack(anchor="w", pady=3)
        else:
            ttk.Label(toggles, text="当前为电脑本地处理，外部模型调用已关闭。").pack(anchor="w", pady=3)
        ttk.Checkbutton(toggles, text="Windows 建言通知", variable=self.var_notifications).pack(anchor="w", pady=3)
        ttk.Checkbutton(toggles, text="登录 Windows 后自动运行", variable=self.var_startup).pack(anchor="w", pady=3)
        ttk.Checkbutton(toggles, text="允许远程后端接收全文", variable=self.var_remote_full).pack(anchor="w", pady=3)
        actions = ttk.Frame(self.settings_tab)
        actions.grid(row=7, column=1, sticky="w", pady=(14, 0))
        ttk.Button(actions, text="测试连接", command=self._test_connection).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存设置", style="Accent.TButton", command=self._save_settings).pack(side="left")
        self.account_action_button = ttk.Button(actions)
        self.account_action_button.pack(side="left", padx=(24, 0))
        self._configure_account_action(self.agent.settings)
        ttk.Button(actions, text="退出AI替身", command=self._quit).pack(side="left", padx=(24, 0))
        self.settings_status = ttk.Label(self.settings_tab, text="", style="Muted.TLabel")
        self.settings_status.grid(row=8, column=1, sticky="w", pady=12)

    @staticmethod
    def _form_row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget, sticky: str = "ew") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=8, padx=(0, 12))
        widget.grid(row=row, column=1, sticky=sticky, pady=8)

    @staticmethod
    def _settings_row(row: int, label: str, widget: tk.Widget, sticky: str = "ew") -> None:
        parent = widget.master
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=9, padx=(0, 16))
        widget.grid(row=row, column=1, sticky=sticky, pady=9)

    def _load_settings_controls(self, settings: AppSettings) -> None:
        self.var_backend.set(settings.backend_url)
        self.settings_backend_entry.configure(
            state="disabled" if settings.has_session_credentials() else "normal"
        )
        self.var_user.set(settings.user_id)
        self.var_token.set(settings.bearer_token)
        self.account_name.configure(
            text=(
                settings.session_username
                or settings.session_user_id
                if settings.auth_mode == "session"
                else f"旧版账号 · {settings.user_id}"
            )
        )
        self._configure_account_action(settings)
        self.var_cloud.set(settings.proactive_cloud_enabled)
        self.var_notifications.set(settings.notifications_enabled)
        self.var_startup.set(self.startup_manager.is_enabled())
        self.var_remote_full.set(settings.allow_remote_full_context)
        self.var_poll.set(str(settings.poll_seconds))
        self.var_sync.set(str(settings.sync_seconds))
        self.var_review.set(str(settings.proactive_review_minutes))
        self.var_locale.set("简体中文" if settings.locale == "zh-CN" else "English")
        self.var_collection.set(settings.collection_enabled)
        self.var_active.set(settings.active_window_enabled)
        self.var_visible_text.set(settings.visible_text_enabled)
        self.var_browser_history.set(settings.browser_history_enabled)
        self.var_clipboard.set(settings.clipboard_enabled)
        self.var_files.set(settings.file_watch_enabled)
        self.var_file_content.set(settings.file_content_enabled)
        self.folder_list.delete(0, "end")
        for folder in settings.watched_folders:
            self.folder_list.insert("end", folder)

    def _configure_account_action(self, settings: AppSettings) -> None:
        button = getattr(self, "account_action_button", None)
        if button is None:
            return
        if settings.auth_mode == "session":
            button.configure(text="退出账号", command=self._logout_account)
        else:
            button.configure(text="领取主人账号", command=self._claim_legacy_owner)

    def _on_locale_change(self, _event: Any = None) -> None:
        selected = self.var_locale.get()
        requested = "zh-CN" if selected in {"简体中文", "Simplified Chinese"} else "en-US"
        if requested == get_locale() and requested == self.agent.settings.locale:
            return
        previous = self.agent.settings.locale
        self._account_locale_generation += 1
        generation = self._account_locale_generation
        self._account_locale_update_pending = True
        session_context = self.agent.ui_session_context()
        self._pending_locale_previous = previous
        set_locale(requested)
        retranslate(self.root)
        self.tray.refresh_locale()
        self.var_locale.set("简体中文" if requested == "zh-CN" else "English")
        self._refresh_local_views()
        if self.preferences.loaded:
            self._sync_preferences_widgets()
        self.settings_status.configure(text=tr("正在保存语言偏好…"))
        self._run_task(
            "account_locale_update",
            lambda: self.agent.update_account_locale(
                requested,
                request_id=generation,
                expected_session_context=session_context,
            ),
            context={
                "locale": requested,
                "previous_locale": previous,
                "generation": generation,
                "session_context": session_context,
            },
        )

    def _settings_from_controls(self) -> AppSettings:
        settings = self.agent.settings
        requested_backend = self.var_backend.get().strip()
        if (
            settings.has_session_credentials()
            and requested_backend.rstrip("/") != settings.backend_url.rstrip("/")
        ):
            raise ValueError("请先退出账号，再更换服务地址。")
        settings.backend_url = requested_backend
        settings.proactive_cloud_enabled = (
            self.var_cloud.get() if os.environ.get("MOUCHEN_MODEL_PROVIDER") != "rules" else False
        )
        settings.notifications_enabled = self.var_notifications.get()
        settings.start_with_windows = self.var_startup.get()
        settings.allow_remote_full_context = self.var_remote_full.get()
        settings.poll_seconds = int(self.var_poll.get())
        settings.sync_seconds = int(self.var_sync.get())
        settings.proactive_review_minutes = int(self.var_review.get())
        settings.collection_enabled = self.var_collection.get()
        settings.active_window_enabled = self.var_active.get()
        settings.visible_text_enabled = self.var_visible_text.get()
        settings.browser_history_enabled = self.var_browser_history.get()
        settings.clipboard_enabled = self.var_clipboard.get()
        settings.file_watch_enabled = self.var_files.get()
        settings.file_content_enabled = self.var_file_content.get()
        settings.watched_folders = list(self.folder_list.get(0, "end"))
        settings.validate()
        return settings

    def _save_settings(self) -> None:
        try:
            settings = self._settings_from_controls()
            self._persist_settings(settings)
            self.settings_status.configure(text="设置已保存")
            self._load_goals()
        except (ValueError, OSError) as exc:
            messagebox.showerror("设置未保存", str(exc), parent=self.root)

    def _save_sources(self) -> None:
        self._save_settings()
        self.tabs.select(self.sources_tab)

    def _logout_account(self) -> None:
        if not messagebox.askyesno(
            "退出账号",
            "将撤销这台电脑的登录并清除本机缓存。是否继续？",
            parent=self.root,
        ):
            return
        self._account_locale_generation += 1
        self._account_locale_update_pending = False
        try:
            self.agent.stop()
        except RuntimeError as exc:
            messagebox.showerror("账号切换已暂停", str(exc), parent=self.root)
            return
        warning = ""
        try:
            self.agent.logout()
        except Exception as exc:
            warning = f"本机登录已清除；服务器暂时未确认撤销：{str(exc)[:140]}"
        set_locale(self.agent.settings.locale)
        retranslate(self.root)
        self._pref_busy = False
        self.preferences.reset_for_user_change()
        self._notification_queue.clear()
        self._queued_advice_ids.clear()
        self._notification_initialized = False
        self.root.withdraw()
        accepted = AccountDialog(self.root, self.agent, warning).show()
        if not accepted:
            self._quit()
            return
        set_locale(self.agent.settings.locale)
        retranslate(self.root)
        self.tray.refresh_locale()
        if not self._ensure_collection_consent():
            self._quit()
            return
        self._load_settings_controls(self.agent.settings)
        self._initialize_notification_queue()
        self.agent.start()
        self._load_goals()
        self._load_advice_preferences()
        self._refresh_local_views()
        self.root.deiconify()

    def _claim_legacy_owner(self) -> None:
        """Stop all legacy work before opening the one-time owner claim."""
        self._account_locale_generation += 1
        self._account_locale_update_pending = False
        try:
            self.agent.stop()
        except RuntimeError as exc:
            messagebox.showerror("账号领取已暂停", str(exc), parent=self.root)
            return
        self._pref_busy = False
        self.preferences.reset_for_user_change()
        self.root.withdraw()
        accepted = AccountDialog(
            self.root,
            self.agent,
            claim_legacy_owner=True,
        ).show()
        if not accepted:
            # Never fall back to observation with the legacy token after a
            # cancelled or failed claim attempt.
            self._quit()
            return
        set_locale(self.agent.settings.locale)
        retranslate(self.root)
        self.tray.refresh_locale()
        if not self._ensure_collection_consent():
            self._quit()
            return
        self._load_settings_controls(self.agent.settings)
        self._initialize_notification_queue()
        self.agent.start()
        self._load_goals()
        self._load_advice_preferences()
        self._refresh_local_views()
        self.root.deiconify()

    def _test_connection(self) -> None:
        try:
            settings = self._settings_from_controls()
            self._persist_settings(settings)
        except (ValueError, OSError) as exc:
            messagebox.showerror("连接设置错误", str(exc), parent=self.root)
            return
        self.settings_status.configure(text="正在连接")
        self._run_task("health", self.agent.health)

    def _persist_settings(self, settings: AppSettings) -> None:
        previous_startup = self.startup_manager.is_enabled()
        previous_settings = self.agent.settings
        notifications_were_enabled = previous_settings.notifications_enabled
        identity_changed = (
            settings.backend_url != previous_settings.backend_url
        )
        if (
            settings.auth_mode == "session"
            and settings.has_current_collection_consent()
            and not identity_changed
        ):
            # Pressing Save is an explicit update to this account's choices.
            # A changed server URL cannot inherit consent from the old server.
            settings.save_current_collection_consent()
        self.startup_manager.set_enabled(settings.start_with_windows)
        try:
            self.agent.update_settings(settings)
        except Exception:
            self.startup_manager.set_enabled(previous_startup)
            raise
        if identity_changed:
            # The cached preferences belong to the previous backend/user:
            # drop them and reload for the new identity.
            self.preferences.reset_for_user_change()
            if hasattr(self, "pref_status"):
                self._sync_preferences_widgets()
            self._load_advice_preferences()
        if settings.notifications_enabled and not notifications_were_enabled:
            self._enqueue_replayable_advice()
            self._notification_next_dispatch_at = 0.0
            self._process_notification_queue(force=True)

    def _submit_problem(self) -> None:
        value = self.problem_text.get("1.0", "end").strip()
        domain = self.problem_domain.get().strip() or None
        try:
            self.agent.submit_problem(value, domain)
        except ValueError as exc:
            messagebox.showwarning("无法提交", str(exc), parent=self.root)
            return
        self.problem_text.delete("1.0", "end")
        self.agent_state.configure(text="问题已进入分析队列")

    def _toggle_pause(self) -> None:
        self.agent.set_paused(not self.agent.paused)
        self.pause_button.configure(text="继续观察" if self.agent.paused else "暂停观察")

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="选择授权文件夹")
        if selected and selected not in self.folder_list.get(0, "end"):
            self.folder_list.insert("end", selected)

    def _remove_folder(self) -> None:
        selection = self.folder_list.curselection()
        if selection:
            self.folder_list.delete(selection[0])

    def _load_goals(self) -> None:
        self._run_task("goals", self.agent.list_goals)

    def _save_goal(self) -> None:
        quote = self.goal_quote.get("1.0", "end").strip()
        title = self.goal_title.get().strip()
        domain = self.goal_domain.get().strip()
        if not quote or not title or not domain:
            messagebox.showwarning("目标不完整", "领域、标题和主公原话不能为空", parent=self.root)
            return
        try:
            weekly_hours = float(self.goal_hours.get())
        except ValueError:
            messagebox.showwarning("目标不完整", "每周小时必须是数字", parent=self.root)
            return
        target = {
            "keywords": self._split_values(self.goal_keywords.get()),
            "packages": self._split_values(self.goal_packages.get()),
            "weekly_hours": weekly_hours,
        }
        payload = {"domain": domain, "title": title, "quote": quote, "target": target, "is_redline": False}
        self._run_task("create_goal", lambda: self.agent.create_goal(payload))

    @staticmethod
    def _split_values(value: str) -> list[str]:
        return [item.strip() for item in re.split(r"[,，;；\n]+", value) if item.strip()]

    def _clear_goal_form(self) -> None:
        self.goal_title.set("")
        self.goal_keywords.set("")
        self.goal_packages.set("")
        self.goal_quote.delete("1.0", "end")

    def _show_goal(self, _: Any = None) -> None:
        selection = self.goal_tree.selection()
        if not selection:
            return
        goal = next((item for item in self.goals if str(item.get("id")) == selection[0]), None)
        if not goal:
            return
        target = goal.get("target", {})
        self.goal_domain.set(str(goal.get("domain", "")))
        self.goal_title.set(str(goal.get("title", "")))
        self.goal_keywords.set(", ".join(str(item) for item in target.get("keywords", [])))
        self.goal_packages.set(", ".join(str(item) for item in target.get("packages", [])))
        self.goal_hours.set(str(target.get("weekly_hours", "")))
        self.goal_quote.delete("1.0", "end")
        self.goal_quote.insert("1.0", str(goal.get("quote", "")))

    def _refresh_local_views(self) -> None:
        stats = self.store.stats()
        self.metric_events.configure(text=str(stats["events_24h"]))
        self.metric_pending.configure(text=str(stats["pending"]))
        self.metric_advice.configure(text=str(stats["advice"]))
        advice = self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT)
        self._fill_advice_tree(self.advice_tree, advice, detailed=True)
        self._fill_advice_tree(self.overview_advice, advice[:12], detailed=False)
        if getattr(self, "selected_advice_id", None):
            selected = self._selected_advice()
            if selected is not None:
                self._render_advice_detail(selected)
        self._refresh_notification_status()
        for item in self.activity_tree.get_children():
            self.activity_tree.delete(item)
        for event in self.store.recent_events(limit=120):
            facts = event.get("facts", {})
            summary = facts.get("app_label") or facts.get("file_name") or facts.get("visible_text") or ""
            state = tr("已同步" if event.get("synced") else "待同步")
            self.activity_tree.insert(
                "",
                "end",
                iid=str(event["local_id"]),
                values=(self._display_time(event.get("occurred_at")), event.get("source"), event.get("type"), state, str(summary)[:180]),
            )

    def _fill_advice_tree(self, tree: ttk.Treeview, advice: list[dict[str, Any]], detailed: bool) -> None:
        selected = tuple(str(item) for item in tree.selection())
        focused = str(tree.focus() or "")
        if tree is getattr(self, "advice_tree", None) and self.selected_advice_id:
            selected = (self.selected_advice_id, *selected)
        for item in tree.get_children():
            tree.delete(item)
        for value in advice:
            advice_id = str(value.get("id"))
            level = value.get("effective_level", value.get("requested_level", 1))
            presentation = project_advice(value, get_locale())
            if detailed:
                row = (
                    self._display_time(value.get("created_at")),
                    f"L{level}",
                    value.get("domain", ""),
                    presentation.action,
                    self._advice_list_status(value),
                )
            else:
                row = (f"L{level}", value.get("domain", ""), presentation.action)
            tree.insert("", "end", iid=advice_id, values=row)
        restore = next(
            (item for item in (*selected, focused) if item and tree.exists(item)),
            None,
        )
        if restore:
            tree.selection_set(restore)
            tree.focus(restore)
            tree.see(restore)
        elif tree is getattr(self, "advice_tree", None) and self.selected_advice_id:
            self.selected_advice_id = None
            self._feedback_status_advice_id = None
            self._clear_advice_detail()
            self._update_feedback_controls(None)

    def _show_advice_detail(self, _: Any = None) -> None:
        selection = self.advice_tree.selection()
        if not selection:
            return
        advice_id = str(selection[0])
        selection_changed = advice_id != self._feedback_status_advice_id
        self.selected_advice_id = advice_id
        self._feedback_status_advice_id = advice_id
        advice = self._selected_advice()
        if not advice:
            return
        delivery_state = self.store.load_state(
            _notification_delivery_key(self.selected_advice_id)
        )
        if delivery_state and not delivery_state.get("acknowledged_at"):
            popup = self._advice_popups_by_id.get(self.selected_advice_id)
            if popup is not None and self._popup_exists(popup):
                self._close_advice_popup(
                    self.selected_advice_id,
                    popup,
                    "opened",
                )
            else:
                self._acknowledge_advice_notification(self.selected_advice_id, "opened")
        self._render_advice_detail(advice)
        self._update_feedback_controls(advice)
        if selection_changed and not self._feedback_inflight:
            snoozed_until = self._future_snoozed_until(advice)
            if snoozed_until:
                message = f"已安排稍后处理至 {self._display_time(snoozed_until)}"
            elif str(advice.get("status", "active")) == "active":
                message = "待处理"
            else:
                message = f"{self._status_label(str(advice.get('status', '')))}；仍可发送文字指导"
            self.feedback_status.configure(text=message, style="Muted.TLabel")

    def _render_advice_detail(self, advice: dict[str, Any]) -> None:
        lines = self._advice_detail_lines(advice)
        self.advice_detail.configure(state="normal")
        self.advice_detail.delete("1.0", "end")
        self.advice_detail.insert("1.0", "\n".join(lines))
        self.advice_detail.configure(state="disabled")

    def _advice_detail_lines(self, advice: dict[str, Any]) -> list[str]:
        """Project translated advice while preserving every original source value."""
        presentation = project_advice(advice, get_locale())
        prediction = advice.get("prediction", {})
        source_label = tr("原始来源：") if get_locale() == "en-US" else tr("依据：")
        lines = [f"{source_label}{advice.get('goal_quote', '')}"]
        for evidence in advice.get("evidence", []):
            fact = evidence.get("fact") if isinstance(evidence, dict) else evidence
            if fact is not None and str(fact):
                lines.append(f"{tr('证据：')}{fact}")
        lines.extend([
            "",
            f"{tr('建议：')}{presentation.action}",
            f"{tr('第一步：')}{presentation.first_step}",
            f"{tr('替代路径：')}{presentation.alternative or tr('无')}",
            f"{tr('预测：')}{presentation.prediction_outcome}",
            f"{tr('核验时间：')}{self._display_time(prediction.get('deadline'))}",
        ])
        if presentation.adopted_expected_result:
            lines.append(
                f"{tr('采纳后的预期结果：')}{presentation.adopted_expected_result}"
            )
        return lines

    def _selected_advice(self) -> dict[str, Any] | None:
        advice_id = self.selected_advice_id
        if not advice_id:
            return None
        return next(
            (
                item
                for item in self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT)
                if str(item.get("id")) == advice_id
            ),
            None,
        )

    def _clear_advice_detail(self) -> None:
        detail = getattr(self, "advice_detail", None)
        if detail is None:
            return
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.configure(state="disabled")

    def _update_feedback_controls(self, advice: dict[str, Any] | None) -> None:
        busy = bool(getattr(self, "_feedback_inflight", False))
        active = bool(advice and str(advice.get("status", "active")) == "active")
        action_state = "normal" if active and not busy else "disabled"
        for button in getattr(self, "feedback_buttons", {}).values():
            button.configure(state=action_state)
        guidance_state = "normal" if advice and not busy else "disabled"
        guidance = getattr(self, "guidance_button", None)
        if guidance is not None:
            guidance.configure(state=guidance_state)
        note = getattr(self, "feedback_note", None)
        if note is not None:
            note.configure(state=guidance_state)

    def _set_feedback_busy(self, busy: bool, message: str) -> None:
        self._feedback_inflight = busy
        self._update_feedback_controls(self._selected_advice())
        status = getattr(self, "feedback_status", None)
        if status is not None:
            status.configure(text=message, style="Muted.TLabel")

    def _open_selected_overview_advice(self) -> None:
        selection = self.overview_advice.selection()
        if not selection:
            return
        self.tabs.select(self.advice_tab)
        if self.advice_tree.exists(selection[0]):
            self.advice_tree.selection_set(selection[0])
            self.advice_tree.see(selection[0])
            self._show_advice_detail()

    def _feedback(self, kind: str) -> None:
        if self._feedback_inflight:
            return
        if not self.selected_advice_id:
            messagebox.showwarning("未选择建言", "请先选择一条建言", parent=self.root)
            return
        advice = self._selected_advice()
        if advice is None:
            messagebox.showwarning("建言已更新", "这条建言已不在本地列表中", parent=self.root)
            return
        if kind != "guidance" and str(advice.get("status", "active")) != "active":
            messagebox.showwarning("建言已处理", "该建言已结束，仍可发送文字指导", parent=self.root)
            self._update_feedback_controls(advice)
            return
        note = self.feedback_note.get("1.0", "end-1c").strip()
        if kind == "guidance" and not note:
            messagebox.showwarning("回复为空", "请输入要告诉AI替身的内容", parent=self.root)
            return
        if len(note) > 2000:
            messagebox.showwarning("回复过长", "回复不能超过 2000 个字符", parent=self.root)
            return
        advice_id = self.selected_advice_id
        label = _FEEDBACK_ACTION_LABELS.get(kind, "反馈")
        self._set_feedback_busy(True, f"正在记录{label}...")

        def submit() -> dict[str, Any]:
            return {
                "advice_id": advice_id,
                "kind": kind,
                "note": note,
                "response": self.agent.feedback(advice_id, kind, note or None),
            }

        self._run_task("feedback", submit)

    def _run_task(
        self,
        name: str,
        function: Callable[[], Any],
        context: dict[str, Any] | None = None,
    ) -> None:
        def run() -> None:
            try:
                self.ui_queue.put(
                    ("task", {"name": name, "value": function(), "context": context})
                )
            except Exception as exc:
                self.ui_queue.put(
                    (
                        "task_error",
                        {
                            "name": name,
                            "error": str(exc),
                            "exception": exc,
                            "context": context,
                        },
                    )
                )

        threading.Thread(target=run, name=f"mouchen-ui-{name}", daemon=True).start()

    def _queue_session_event(self, kind: str, value: dict[str, Any]) -> None:
        """Fence account-authored UI events across a queued account switch."""
        self.ui_queue.put(
            (
                kind,
                {
                    "_mouchen_scoped_ui_event": True,
                    "value": value,
                    "session_context": self.agent.ui_session_context(),
                },
            )
        )

    def _current_session_event(self, queued: Any) -> dict[str, Any] | None:
        if not (
            isinstance(queued, dict)
            and queued.get("_mouchen_scoped_ui_event") is True
        ):
            return queued if isinstance(queued, dict) else None
        if not self.agent.ui_session_context_is_current(queued.get("session_context")):
            return None
        value = queued.get("value")
        return value if isinstance(value, dict) else None

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, value = self.ui_queue.get_nowait()
                if kind == "status":
                    self._apply_status(value)
                elif kind == "advice":
                    current = self._current_session_event(value)
                    if current is not None:
                        self._handle_new_advice(current)
                elif kind == "attention":
                    current = self._current_session_event(value)
                    if current is not None:
                        self._handle_attention_claim(current)
                elif kind == "event":
                    self._refresh_local_views()
                elif kind == "task":
                    self._handle_task(value)
                elif kind == "task_error":
                    self._handle_task_error(value)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_queue)

    def _apply_status(self, status: dict[str, Any]) -> None:
        self._last_status = status
        account_locale = str(status.get("account_locale") or "")
        locale_context = status.get("account_locale_context")
        agent_locale = self.agent.settings.locale
        locale_context_current = (
            locale_context is None
            or self.agent.ui_session_context_is_current(locale_context)
        )
        if (
            account_locale
            and not getattr(self, "_account_locale_update_pending", False)
            and locale_context_current
            and account_locale == agent_locale
            and account_locale != get_locale()
        ):
            set_locale(account_locale)
            retranslate(self.root)
            self.tray.refresh_locale()
            self.var_locale.set("简体中文" if get_locale() == "zh-CN" else "English")
            self._refresh_local_views()
            if self.preferences.loaded:
                self._sync_preferences_widgets()
            self._load_advice_preferences(refresh=True)
        online = status.get("online")
        self.connection_label.configure(text="后端在线" if online else "后端离线" if online is False else "后端检测中")
        current_app = status.get("current_app") or "尚无活动窗口"
        self.current_app.configure(text=current_app)
        self.current_title.configure(text=status.get("current_title", ""))
        self.agent_state.configure(text=_analysis_state_text(status))
        self.metric_events.configure(text=str(status.get("events_24h", 0)))
        self.metric_pending.configure(text=str(status.get("pending", 0)))
        self.metric_advice.configure(text=str(status.get("advice", 0)))

    def _handle_new_advice(self, advice: dict[str, Any]) -> None:
        """Synchronize display state only; server claims are the sole notification trigger."""
        self._refresh_local_views()
        advice_id = str(advice.get("id", ""))
        if not advice_id:
            return
        status = str(advice.get("status", "active"))
        if status != "active":
            self._finalize_advice_notification(advice_id, status)
        if bool(advice.get(_LOCALLY_NEW_ADVICE_KEY)):
            self._advance_notification_watermark(advice)

    def _handle_attention_claim(self, claim: dict[str, Any]) -> None:
        if str(claim.get("status", "")) != "claimed":
            return
        advice = claim.get("advice")
        claim_token = str(claim.get("claim_token", "")).strip()
        advice_id = str(advice.get("id", "")).strip() if isinstance(advice, dict) else ""
        try:
            delivery_number = int(claim.get("delivery_number", 0))
        except (TypeError, ValueError):
            delivery_number = 0
        if not advice_id or not claim_token:
            return

        normalized_claim = dict(claim)
        normalized_claim["delivery_number"] = delivery_number
        self._attention_claims[advice_id] = normalized_claim
        if delivery_number not in {1, 2}:
            self._report_attention_result(
                advice_id,
                delivered=False,
                reason="invalid_delivery_number",
            )
            return

        self.store.save_advice(advice)
        if str(advice.get("status", "active")) != "active":
            self._report_attention_result(
                advice_id,
                delivered=False,
                reason="advice_not_active",
            )
            self._finalize_advice_notification(
                advice_id,
                str(advice.get("status", "withdrawn")),
            )
            return

        # If this delivery was already shown before an app/network interruption,
        # acknowledge the server claim without showing the same popup again.
        state = self._delivery_state_for(advice_id)
        try:
            shown_number = int(state.get("delivery_number", 0))
        except (TypeError, ValueError):
            shown_number = 0
        if state.get("delivered_at") and shown_number >= delivery_number:
            self._report_attention_result(advice_id, delivered=True)
            return

        self._enqueue_advice_notification(advice_id)
        self._process_notification_queue(force=True)

    def _initialize_notification_queue(
        self,
        *,
        startup_at: datetime | None = None,
    ) -> None:
        if self._notification_initialized:
            return
        advice = self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT)
        by_id = {str(item.get("id", "")): item for item in advice if item.get("id")}
        last_delivery = self.store.load_state(_NOTIFICATION_STATE_KEY)
        watermark_state = self.store.load_state(_NOTIFICATION_WATERMARK_KEY)
        candidates = [self._advice_watermark(item) for item in advice]
        # The wall-clock boundary suppresses backend history first downloaded after startup.
        candidates.append((_notification_timestamp(startup_at or datetime.now(timezone.utc)), ""))
        if watermark_state:
            candidates.append(self._state_watermark(watermark_state, by_id))
        if last_delivery:
            candidates.append(self._state_watermark(last_delivery, by_id))
        self._notification_watermark = max(candidates, default=("", ""))
        self._notification_startup_watermark = self._notification_watermark
        self._save_notification_watermark()
        self._notification_initialized = True
        self._reconcile_advice_notifications(advice)
        self._refresh_notification_status()

    @staticmethod
    def _advice_watermark(advice: dict[str, Any]) -> tuple[str, str]:
        return (
            _notification_timestamp(advice.get("created_at")),
            str(advice.get("id", "")),
        )

    @classmethod
    def _state_watermark(
        cls,
        state: dict[str, Any],
        advice_by_id: dict[str, dict[str, Any]],
    ) -> tuple[str, str]:
        advice_id = str(state.get("advice_id", ""))
        created_at = str(state.get("advice_created_at", ""))
        if not created_at and advice_id in advice_by_id:
            return cls._advice_watermark(advice_by_id[advice_id])
        return (_notification_timestamp(created_at), advice_id)

    def _advance_notification_watermark(self, advice: dict[str, Any]) -> None:
        candidate = self._advice_watermark(advice)
        if candidate <= self._notification_watermark:
            return
        self._notification_watermark = candidate
        self._save_notification_watermark()

    def _save_notification_watermark(self) -> None:
        created_at, advice_id = self._notification_watermark
        self.store.save_state(
            _NOTIFICATION_WATERMARK_KEY,
            {"advice_id": advice_id, "advice_created_at": created_at},
        )

    def _enqueue_advice_notification(self, advice_id: str) -> None:
        if (
            not advice_id
            or advice_id in self._queued_advice_ids
            or (
                advice_id in self._visible_advice_popup_ids
                and advice_id not in self._attention_claims
            )
        ):
            return
        bisect.insort(self._notification_queue, advice_id)
        self._queued_advice_ids.add(advice_id)

    def _remove_queued_advice(self, advice_id: str) -> None:
        self._queued_advice_ids.discard(advice_id)
        try:
            self._notification_queue.remove(advice_id)
        except ValueError:
            pass

    def _process_notification_queue(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> bool:
        if not self._notification_initialized:
            self._initialize_notification_queue()
        if not self.agent.settings.notifications_enabled:
            for advice_id in list(self._attention_claims):
                self._remove_queued_advice(advice_id)
                self._report_attention_result(
                    advice_id,
                    delivered=False,
                    reason="notifications_disabled",
                )
            self._refresh_notification_status()
            return False
        moment = time.monotonic() if now is None else float(now)
        if not force and moment < self._notification_next_dispatch_at:
            return False

        advice_by_id = {
            str(item.get("id", "")): item
            for item in self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT)
            if item.get("id")
        }
        for advice_id in list(self._notification_queue):
            advice = advice_by_id.get(advice_id)
            if not advice:
                self._remove_queued_advice(advice_id)
                continue
            if str(advice.get("status", "active")) != "active":
                self._finalize_advice_notification(
                    advice_id, str(advice.get("status", "withdrawn"))
                )
                continue
            if advice_id not in self._attention_claims:
                self._remove_queued_advice(advice_id)
                continue

            self._remove_queued_advice(advice_id)
            delivered = self._deliver_advice_notification(advice)
            self._notification_next_dispatch_at = moment + _NOTIFICATION_RATE_LIMIT_SECONDS
            state = self._delivery_state_for(advice_id)
            self._report_attention_result(
                advice_id,
                delivered=delivered,
                reason=str(state.get("last_error", "notification_delivery_failed")),
            )
            return delivered
        return False

    def _report_attention_result(
        self,
        advice_id: str,
        *,
        delivered: bool,
        reason: str | None = None,
    ) -> None:
        claim = self._attention_claims.pop(advice_id, None)
        if not claim:
            return
        claim_token = str(claim.get("claim_token", ""))
        if not claim_token:
            return

        state_key = _notification_delivery_key(advice_id)
        state = self.store.load_state(state_key)
        state["advice_id"] = advice_id
        state["server_report_status"] = "pending_complete" if delivered else "pending_fail"
        state["server_report_action"] = "complete" if delivered else "fail"
        state["server_claim_token"] = claim_token
        state["server_report_reason"] = str(reason or "")[:500]
        state["server_lease_expires_at"] = str(claim.get("lease_expires_at", ""))
        self.store.save_state(state_key, state)
        latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
        if str(latest.get("advice_id", "")) == advice_id:
            self.store.save_state(_NOTIFICATION_STATE_KEY, state)

        def report() -> dict[str, Any]:
            response = (
                self.agent.complete_attention(advice_id, claim_token)
                if delivered
                else self.agent.fail_attention(advice_id, claim_token, reason)
            )
            return {
                "advice_id": advice_id,
                "delivered": delivered,
                "response": response,
            }

        self._run_task(f"attention_report:{advice_id}", report)

    def _delivery_state_for(self, advice_id: str) -> dict[str, Any]:
        state = self.store.load_state(_notification_delivery_key(advice_id))
        if state:
            return state
        latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
        return latest if str(latest.get("advice_id", "")) == advice_id else {}

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def _future_snoozed_until(
        self,
        advice: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        snoozed_until = self._parse_timestamp(advice.get("snoozed_until"))
        current = now or datetime.now(timezone.utc)
        return snoozed_until if snoozed_until and snoozed_until > current else None

    def _advice_list_status(self, advice: dict[str, Any]) -> str:
        status = str(advice.get("status", "active"))
        snoozed_until = self._future_snoozed_until(advice)
        if status == "active" and snoozed_until:
            return tr(f"稍后至 {self._display_time(snoozed_until)}")
        return self._status_label(status)

    def _notify_latest_unseen_advice(self) -> None:
        """Compatibility entrypoint for older launch callbacks and integrations."""
        self._initialize_notification_queue()
        self._process_notification_queue()

    def _deliver_advice_notification(self, advice: dict[str, Any]) -> bool:
        level = int(advice.get("effective_level", advice.get("requested_level", 1)))
        presentation = project_advice(advice, get_locale())
        title = tr(f"AI替身 L{level} · {advice.get('domain', 'general')}")
        message = (
            f"{presentation.action}\n"
            f"{tr('第一步：')}{presentation.first_step}"
        )
        advice_id = str(advice.get("id", ""))
        claim = getattr(self, "_attention_claims", {}).get(advice_id, {})
        try:
            delivery_number = int(claim.get("delivery_number", 0))
        except (TypeError, ValueError):
            delivery_number = 0
        attempted = datetime.now(timezone.utc)
        attempted_at = attempted.isoformat()
        previous = self.store.load_state(_notification_delivery_key(advice_id)) if advice_id else {}
        tray_delivered = False
        popup_delivered = False
        channels: list[str] = []
        errors: list[str] = []
        if self.agent.settings.notifications_enabled:
            try:
                tray_delivered = self.tray.notify(title, message, level)
            except Exception as exc:
                errors.append(f"Windows 托盘通知异常：{exc}")
            if tray_delivered:
                channels.append("windows_tray")
            elif not errors:
                errors.append(
                    str(
                        getattr(self.tray, "last_notification_error", None)
                        or "Windows 未接受托盘通知"
                    )
                )
            popup_delivered = self._show_advice_popup(
                title,
                message,
                advice_id,
                level,
            )
            if popup_delivered:
                channels.append("in_app_popup")
            else:
                errors.append("应用内建言弹窗创建失败")
        else:
            errors.append("建言通知已关闭")
        delivered = tray_delivered or popup_delivered
        if advice_id:
            state = {
                "advice_id": advice_id,
                "advice_created_at": str(advice.get("created_at", "")),
                "attempted_at": attempted_at,
                "attempt_count": int(previous.get("attempt_count", 0)) + 1,
                "delivery_status": (
                    "awaiting_acknowledgement"
                    if popup_delivered
                    else "delivered_unconfirmed"
                    if tray_delivered
                    else "failed"
                ),
                "channel": "+".join(channels),
                "tray_status": "accepted" if tray_delivered else "failed",
                "popup_status": "visible" if popup_delivered else "failed",
                "last_error": "; ".join(errors),
            }
            if delivery_number in {1, 2}:
                state["delivery_number"] = delivery_number
            if (
                str(previous.get("delivery_status", "")) == "snoozed"
                and previous.get("snoozed_until")
            ):
                state["snoozed_until"] = str(previous["snoozed_until"])
            if delivered:
                state["delivered_at"] = attempted_at
            else:
                retry_seconds = min(
                    _NOTIFICATION_RETRY_MAX_SECONDS,
                    _NOTIFICATION_RETRY_BASE_SECONDS
                    * (2 ** min(state["attempt_count"] - 1, 8)),
                )
                state["next_retry_at"] = (
                    attempted + timedelta(seconds=retry_seconds)
                ).isoformat()
            self.store.save_state(_notification_delivery_key(advice_id), state)
            self.store.save_state(_NOTIFICATION_STATE_KEY, state)
            self._refresh_notification_status()
        if level >= 3:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except RuntimeError:
                pass
        return delivered

    def _acknowledge_advice_notification(self, advice_id: str, acknowledgement: str) -> None:
        if not advice_id:
            return
        state_key = _notification_delivery_key(advice_id)
        state = self.store.load_state(state_key)
        if not state:
            latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
            state = latest if str(latest.get("advice_id", "")) == advice_id else {}
        if not state:
            return
        if (
            str(state.get("delivery_status", "")) in _TERMINAL_ADVICE_STATUSES
            or state.get("terminal_status")
        ):
            return
        state = dict(state)
        state["delivery_status"] = "acknowledged"
        acknowledged_at = datetime.now(timezone.utc).isoformat()
        state["acknowledged_at"] = acknowledged_at
        state["acknowledgement"] = acknowledgement
        if state.get("popup_status") == "visible":
            state["popup_status"] = "closed"
            state["closed_at"] = acknowledged_at
        self.store.save_state(state_key, state)
        latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
        if str(latest.get("advice_id", "")) == advice_id:
            self.store.save_state(_NOTIFICATION_STATE_KEY, state)
        self._refresh_notification_status()

    def _submit_popup_acknowledgement(self, advice_id: str) -> None:
        """Record the explicit Windows "知道了" action on the server."""
        if not advice_id or not self._advice_is_active(advice_id):
            return

        def submit() -> dict[str, Any]:
            return {
                "advice_id": advice_id,
                "kind": "acknowledged",
                "note": None,
                "response": self.agent.feedback(advice_id, "acknowledged", None),
            }

        self._run_task("feedback", submit)

    def _reconcile_advice_notifications(
        self,
        advice: list[dict[str, Any]] | None = None,
    ) -> None:
        items = (
            advice
            if advice is not None
            else self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT)
        )
        for item in items:
            advice_id = str(item.get("id", ""))
            status = str(item.get("status", "active"))
            snoozed_until = self._parse_timestamp(item.get("snoozed_until"))
            if advice_id and status == "active" and snoozed_until:
                self._snooze_advice_notification(
                    advice_id,
                    snoozed_until,
                    advice=item,
                )
            elif advice_id and status != "active":
                self._finalize_advice_notification(advice_id, status)

    def _snooze_advice_notification(
        self,
        advice_id: str,
        snoozed_until: datetime,
        *,
        advice: dict[str, Any] | None = None,
        make_latest: bool = False,
    ) -> None:
        until_text = snoozed_until.astimezone(timezone.utc).isoformat()
        state = self._delivery_state_for(advice_id)
        recorded_snooze = state.get("snoozed_until")
        if not recorded_snooze and str(state.get("delivery_status", "")) == "snoozed":
            recorded_snooze = state.get("next_retry_at")
        if _notification_timestamp(recorded_snooze) == _notification_timestamp(until_text):
            if (
                str(state.get("delivery_status", "")) == "snoozed"
                and advice_id in self._attention_claims
            ):
                self._enqueue_advice_notification(advice_id)
            return

        popup = self._advice_popups_by_id.pop(advice_id, None)
        self._visible_advice_popup_ids.discard(advice_id)
        if popup is not None:
            try:
                if self._popup_exists(popup):
                    popup.destroy()
            except (tk.TclError, AttributeError):
                pass
        self._advice_popups = [
            item
            for item in self._advice_popups
            if item is not popup and self._popup_exists(item)
        ]
        self._remove_queued_advice(advice_id)

        snoozed_state = dict(state)
        snoozed_state.update(
            {
                "advice_id": advice_id,
                "advice_created_at": str((advice or {}).get("created_at", "")),
                "delivery_status": "snoozed",
                "popup_status": "closed",
                "snoozed_at": datetime.now(timezone.utc).isoformat(),
                "snoozed_until": until_text,
                "next_retry_at": until_text,
            }
        )
        for key in (
            "acknowledged_at",
            "acknowledgement",
            "terminal_status",
            "closed_at",
        ):
            snoozed_state.pop(key, None)
        self.store.save_state(_notification_delivery_key(advice_id), snoozed_state)
        latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
        if make_latest or str(latest.get("advice_id", "")) == advice_id:
            self.store.save_state(_NOTIFICATION_STATE_KEY, snoozed_state)
        if advice_id in self._attention_claims:
            self._enqueue_advice_notification(advice_id)
        self._refresh_notification_status()

    def _finalize_advice_notification(self, advice_id: str, status: str) -> None:
        claim = getattr(self, "_attention_claims", {}).pop(advice_id, None)
        state = self._delivery_state_for(advice_id)
        popup = self._advice_popups_by_id.get(advice_id)
        affected = bool(
            claim
            or state
            or popup is not None
            or advice_id in self._queued_advice_ids
            or advice_id in self._visible_advice_popup_ids
        )
        self._remove_queued_advice(advice_id)
        self._visible_advice_popup_ids.discard(advice_id)
        self._advice_popups_by_id.pop(advice_id, None)
        if popup is not None:
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except (tk.TclError, AttributeError):
                pass
        self._advice_popups = [
            item
            for item in self._advice_popups
            if item is not popup and self._popup_exists(item)
        ]
        if not affected:
            return

        terminal_status = status if status != "active" else "withdrawn"
        terminal_state = dict(state)
        terminal_state.update(
            {
                "advice_id": advice_id,
                "delivery_status": terminal_status,
                "terminal_status": terminal_status,
                "popup_status": "closed",
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        terminal_state.pop("next_retry_at", None)
        self.store.save_state(_notification_delivery_key(advice_id), terminal_state)
        latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
        if str(latest.get("advice_id", "")) == advice_id:
            self.store.save_state(_NOTIFICATION_STATE_KEY, terminal_state)
        self._refresh_notification_status()

    @staticmethod
    def _popup_exists(popup: Any) -> bool:
        try:
            return bool(popup.winfo_exists())
        except (tk.TclError, AttributeError):
            return False

    def _advice_is_active(self, advice_id: str) -> bool:
        return self._advice_status(advice_id) == "active"

    def _advice_status(self, advice_id: str) -> str | None:
        for item in self.store.list_advice(limit=_ADVICE_RECONCILE_LIMIT):
            if str(item.get("id", "")) == advice_id:
                return str(item.get("status", "active"))
        return None

    def _close_advice_popup(
        self,
        advice_id: str,
        popup: Any,
        acknowledgement: str,
    ) -> bool:
        status = self._advice_status(advice_id) if advice_id else "active"
        if advice_id and status is not None and status != "active":
            self._finalize_advice_notification(advice_id, status)
            return False
        if advice_id:
            self._visible_advice_popup_ids.discard(advice_id)
            if self._advice_popups_by_id.get(advice_id) is popup:
                self._advice_popups_by_id.pop(advice_id, None)
            self._acknowledge_advice_notification(advice_id, acknowledgement)
        try:
            if self._popup_exists(popup):
                popup.destroy()
        except (tk.TclError, AttributeError):
            pass
        self._advice_popups = [
            item for item in self._advice_popups if item is not popup and self._popup_exists(item)
        ]
        if advice_id and acknowledgement == "acknowledged":
            self._submit_popup_acknowledgement(advice_id)
        return True

    def _refresh_notification_status(self) -> None:
        label = getattr(self, "notification_status", None)
        if label is None:
            return
        state = self.store.load_state(_NOTIFICATION_STATE_KEY)
        if not state:
            label.configure(text="通知状态：暂无投递记录", style="Muted.TLabel")
            return
        delivery_status = str(state.get("delivery_status", ""))
        if not delivery_status:
            delivery_status = (
                "awaiting_acknowledgement"
                if "in_app_popup" in str(state.get("channel", ""))
                else "delivered_unconfirmed"
                if state.get("channel")
                else "failed"
            )
        occurred_at = (
            state.get("acknowledged_at")
            or state.get("delivered_at")
            or state.get("attempted_at")
        )
        when = self._display_time(occurred_at)
        suffix = f" · {when}" if when else ""
        channel_error = str(state.get("last_error") or "")[:140]
        if delivery_status in _TERMINAL_ADVICE_STATUSES:
            status_text = self._status_label(delivery_status)
            text = f"通知状态：建言{status_text}{suffix}"
            style = "Muted.TLabel"
        elif delivery_status == "snoozed":
            snoozed_until = self._display_time(state.get("next_retry_at"))
            text = f"通知状态：稍后提醒 · {snoozed_until}" if snoozed_until else "通知状态：稍后提醒"
            style = "Muted.TLabel"
        elif delivery_status == "acknowledged":
            text = f"通知状态：已确认{suffix}"
            style = "Muted.TLabel"
        elif delivery_status == "awaiting_acknowledgement":
            text = f"通知状态：应用内建言已显示，等待确认{suffix}"
            style = "Muted.TLabel"
            if channel_error:
                text = f"{text}；部分渠道失败：{channel_error}"
                style = "NotificationError.TLabel"
        elif delivery_status == "delivered_unconfirmed":
            text = f"通知状态：系统托盘已接收；建言列表可随时查看{suffix}"
            style = "Muted.TLabel"
            if channel_error:
                text = f"{text}；部分渠道失败：{channel_error}"
                style = "NotificationError.TLabel"
        else:
            error = str(state.get("last_error") or "通知渠道不可用")[:180]
            text = f"通知失败：{error}；建言已保存在列表{suffix}"
            style = "NotificationError.TLabel"
        label.configure(text=text, style=style)

    def _show_advice_popup(
        self,
        title: str,
        message: str,
        advice_id: str,
        level: int,
    ) -> bool:
        """Show a Tk popup as a delivery channel independent of Windows Focus Assist."""
        popup: tk.Toplevel | None = None
        try:
            existing = self._advice_popups_by_id.get(advice_id) if advice_id else None
            if existing is not None and self._popup_exists(existing):
                existing.lift()
                return True
            self._advice_popups = [
                item for item in getattr(self, "_advice_popups", []) if self._popup_exists(item)
            ]
            visible_ids = getattr(self, "_visible_advice_popup_ids", None)
            if visible_ids is None:
                visible_ids = set()
                self._visible_advice_popup_ids = visible_ids
            popup = tk.Toplevel(self.root)
            self._advice_popups.append(popup)
            if advice_id:
                visible_ids.add(advice_id)
                self._advice_popups_by_id[advice_id] = popup
            popup.title(title)
            popup.configure(bg=COLORS["surface"])
            popup.resizable(False, False)
            popup.attributes("-topmost", True)
            try:
                popup.attributes("-toolwindow", True)
            except tk.TclError:
                pass

            header_color = COLORS["danger"] if level >= 3 else COLORS["accent"]
            header = tk.Label(
                popup,
                bg=header_color,
                fg="#ffffff",
                anchor="w",
                padx=16,
                pady=10,
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            set_raw_widget_text(header, title)
            header.pack(fill="x")
            body = tk.Label(
                popup,
                bg=COLORS["surface"],
                fg=COLORS["text"],
                justify="left",
                anchor="nw",
                wraplength=388,
                padx=16,
                pady=16,
                font=("Microsoft YaHei UI", 10),
            )
            set_raw_widget_text(body, message)
            body.pack(fill="both", expand=True)

            buttons = tk.Frame(popup, bg=COLORS["surface"])
            buttons.pack(fill="x", padx=14, pady=(0, 14))

            def close_popup(acknowledgement: str) -> bool:
                return self._close_advice_popup(advice_id, popup, acknowledgement)

            def open_advice() -> None:
                if not close_popup("opened"):
                    return
                self._show_window()
                self._refresh_local_views()
                self.tabs.select(self.advice_tab)
                if advice_id and self.advice_tree.exists(advice_id):
                    self.advice_tree.selection_set(advice_id)
                    self.advice_tree.see(advice_id)
                    self._show_advice_detail()

            popup.protocol("WM_DELETE_WINDOW", lambda: close_popup("dismissed"))

            tk.Button(
                buttons,
                text="查看建言",
                command=open_advice,
                bg=COLORS["accent"],
                fg="#ffffff",
                activebackground=COLORS["accent_dark"],
                activeforeground="#ffffff",
                relief="flat",
                padx=14,
                pady=5,
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(side="right")
            tk.Button(
                buttons,
                text="知道了",
                command=lambda: close_popup("acknowledged"),
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                relief="flat",
                padx=12,
                pady=5,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="right", padx=(0, 8))

            popup.update_idletasks()
            width = max(420, popup.winfo_reqwidth())
            height = popup.winfo_reqheight()
            stack_offset = min(len(self._advice_popups) - 1, 2) * (height + 12)
            x = max(12, popup.winfo_screenwidth() - width - 24)
            y = max(12, popup.winfo_screenheight() - height - 72 - stack_offset)
            popup.geometry(f"{width}x{height}+{x}+{y}")
            popup.lift()
            return True
        except (tk.TclError, AttributeError):
            if advice_id:
                getattr(self, "_visible_advice_popup_ids", set()).discard(advice_id)
                getattr(self, "_advice_popups_by_id", {}).pop(advice_id, None)
            if popup is not None:
                try:
                    if self._popup_exists(popup):
                        popup.destroy()
                except (tk.TclError, AttributeError):
                    pass
                self._advice_popups = [
                    item
                    for item in getattr(self, "_advice_popups", [])
                    if item is not popup and self._popup_exists(item)
                ]
            return False

    def _account_locale_result_is_current(self, result: dict[str, Any]) -> bool:
        context = result.get("context") if isinstance(result.get("context"), dict) else {}
        try:
            generation = int(context.get("generation", -1))
        except (TypeError, ValueError):
            return False
        session_context = context.get("session_context")
        return (
            generation == self._account_locale_generation
            and session_context is not None
            and self.agent.ui_session_context_is_current(session_context)
        )

    def _handle_task(self, result: dict[str, Any]) -> None:
        name, value = result["name"], result["value"]
        if name == "health":
            self.settings_status.configure(text=f"连接成功 · {value.get('version', '')}")
        elif name == "account_locale_update":
            if not self._account_locale_result_is_current(result):
                return
            if isinstance(value, dict) and value.get("_discarded"):
                self._account_locale_update_pending = False
                return
            self._account_locale_update_pending = False
            applied = str(value.get("locale") or get_locale()) if isinstance(value, dict) else get_locale()
            set_locale(applied)
            retranslate(self.root)
            self.tray.refresh_locale()
            self.var_locale.set("简体中文" if get_locale() == "zh-CN" else "English")
            self._refresh_local_views()
            if self.preferences.loaded:
                self._sync_preferences_widgets()
            self._load_advice_preferences(refresh=True)
            self.settings_status.configure(text="语言偏好已同步到账号")
        elif name == "goals":
            self.goals = value
            self._render_goals()
            # Keep the preferences tab's goal list in sync; the controller
            # preserves the current selection and any unsaved text.
            self._load_advice_preferences(refresh=True)
        elif name == "create_goal":
            self._clear_goal_form()
            self._load_goals()
        elif name in {
            "advice_preferences_load",
            "advice_preferences_save",
            "advice_preferences_delete",
        }:
            self._handle_preferences_task(name, result)
        elif name == "feedback":
            payload = value if isinstance(value, dict) else {}
            kind = str(payload.get("kind", ""))
            response = payload.get("response")
            updated = response.get("advice") if isinstance(response, dict) else None
            if isinstance(updated, dict):
                self.store.save_advice(updated)
            note = getattr(self, "feedback_note", None)
            if note is not None:
                note.configure(state="normal")
                note.delete("1.0", "end")
            self._feedback_inflight = False
            advice_id = str(payload.get("advice_id", ""))
            if advice_id:
                # Any explicit feedback ends this local delivery attempt. The
                # server owns future eligibility (including "later").
                self._finalize_advice_notification(advice_id, "feedback")
            self._refresh_local_views()
            current = self._selected_advice()
            self._update_feedback_controls(current)
            if kind == "later" and current:
                snoozed_until = self._future_snoozed_until(current)
                if snoozed_until:
                    self._snooze_advice_notification(
                        str(current.get("id", "")),
                        snoozed_until,
                        advice=current,
                        make_latest=True,
                    )
                else:
                    self._reconcile_advice_notifications([current])
            else:
                self._reconcile_advice_notifications([current] if current else [])
            messages = {
                "adopted": "已采纳，AI替身已记录",
                "irrelevant": "已标记无关，后续会降低同类建言",
                "fact_error": "已标记事实有误，后续会提高证据要求",
                "later": "已安排稍后提醒",
                "acknowledged": "已知悉，本条不再提醒",
                "guidance": "指导已收到，后续建言会据此调整",
            }
            status = getattr(self, "feedback_status", None)
            if status is not None:
                status.configure(
                    text=messages.get(kind, "反馈已记录"),
                    style="Muted.TLabel",
                )
        elif name.startswith("attention_report:"):
            payload = value if isinstance(value, dict) else {}
            advice_id = str(payload.get("advice_id", ""))
            if advice_id:
                state_key = _notification_delivery_key(advice_id)
                state = self.store.load_state(state_key)
                response = payload.get("response")
                state["server_report_status"] = str(
                    response.get("status", "reported")
                    if isinstance(response, dict)
                    else "reported"
                )
                for field in (
                    "server_report_action",
                    "server_claim_token",
                    "server_report_reason",
                    "server_report_error",
                ):
                    state.pop(field, None)
                self.store.save_state(state_key, state)
                latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
                if str(latest.get("advice_id", "")) == advice_id:
                    self.store.save_state(_NOTIFICATION_STATE_KEY, state)
                self._refresh_notification_status()

    def _handle_task_error(self, result: dict[str, Any]) -> None:
        name = result.get("name", "操作")
        error = result.get("error", "未知错误")
        if name == "health":
            self.settings_status.configure(text=f"连接失败 · {error}")
        elif name == "account_locale_update":
            if not self._account_locale_result_is_current(result):
                return
            self._account_locale_update_pending = False
            context = result.get("context") if isinstance(result.get("context"), dict) else {}
            # The agent has already restored the last value confirmed by the
            # server.  This can differ from the local value seen before the
            # latest click when an older successful PUT preceded this failure.
            rollback_locale = str(
                self.agent.settings.locale
                or context.get("previous_locale")
                or getattr(self, "_pending_locale_previous", "zh-CN")
            )
            set_locale(rollback_locale)
            retranslate(self.root)
            self.tray.refresh_locale()
            self.var_locale.set("简体中文" if get_locale() == "zh-CN" else "English")
            self._refresh_local_views()
            if self.preferences.loaded:
                self._sync_preferences_widgets()
            self._load_advice_preferences(refresh=True)
            self.settings_status.configure(text=f"语言切换失败：{error}", style="NotificationError.TLabel")
            messagebox.showerror("语言切换失败", error, parent=self.root)
        elif name == "goals":
            self.goals = []
            self._render_goals()
        elif name in {
            "advice_preferences_load",
            "advice_preferences_save",
            "advice_preferences_delete",
        }:
            self._handle_preferences_task_error(name, result)
        elif name == "feedback":
            self._feedback_inflight = False
            self._update_feedback_controls(self._selected_advice())
            status = getattr(self, "feedback_status", None)
            if status is not None:
                status.configure(
                    text=f"提交失败：{error[:160]}",
                    style="NotificationError.TLabel",
                )
            messagebox.showerror("反馈提交失败", error, parent=self.root)
        elif name.startswith("attention_report:"):
            advice_id = name.partition(":")[2]
            state_key = _notification_delivery_key(advice_id)
            state = self.store.load_state(state_key)
            state["server_report_status"] = "error"
            state["server_report_error"] = str(error)[:300]
            self.store.save_state(state_key, state)
            latest = self.store.load_state(_NOTIFICATION_STATE_KEY)
            if str(latest.get("advice_id", "")) == advice_id:
                self.store.save_state(_NOTIFICATION_STATE_KEY, state)
            self._refresh_notification_status()
        else:
            messagebox.showerror("操作失败", error, parent=self.root)

    def _render_goals(self) -> None:
        for item in self.goal_tree.get_children():
            self.goal_tree.delete(item)
        domains = []
        for goal in self.goals:
            goal_id = str(goal.get("id"))
            domain = str(goal.get("domain", ""))
            domains.append(domain)
            self.goal_tree.insert("", "end", iid=goal_id, values=(domain, goal.get("title", "")))
        self.problem_domain_box.configure(values=domains)
        if domains and self.problem_domain.get() not in domains:
            self.problem_domain.set(domains[0])

    def _drain_tray(self) -> None:
        try:
            while True:
                action = self.tray.actions.get_nowait()
                if action == "show":
                    self._show_window()
                elif action == "toggle_pause":
                    self._toggle_pause()
                elif action == "quit":
                    self._quit()
        except queue.Empty:
            pass
        self.root.after(350, self._drain_tray)

    def _periodic_refresh(self) -> None:
        self._refresh_local_views()
        self._reconcile_advice_notifications()
        self._process_notification_queue()
        self.root.after(2_000, self._periodic_refresh)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _on_close(self) -> None:
        if self.tray.available:
            self.root.withdraw()
        else:
            self._quit()

    def _quit(self) -> None:
        try:
            self.agent.stop()
            self.tray.stop()
            self.store.close()
        finally:
            self.root.destroy()

    @staticmethod
    def _display_time(value: Any) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%m-%d %H:%M")
        except ValueError:
            return str(value)[:16]

    @staticmethod
    def _status_label(value: str) -> str:
        label = {
            "active": "待处理",
            "adopted": "已采纳",
            "dismissed": "已忽略",
            "feedback": "已反馈",
            "withdrawn": "已撤回",
            "verified": "已核验",
        }.get(value)
        return tr(label) if label is not None else value


def run() -> None:
    root = tk.Tk()
    MouchenWindow(root)
    root.mainloop()
