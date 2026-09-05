from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


_VISIBLE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|OPENSSH PRIVATE KEY)-----.*?-----END [^-]+-----",
    re.IGNORECASE | re.DOTALL,
)
_VISIBLE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b"
    r"\s*[:=]\s*([^\s,;]{4,})"
)
_VISIBLE_PAYMENT = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_VISIBLE_TOKEN = re.compile(
    r"(?i)\b(?:bearer\s+)?(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}|"
    r"xox[baprs]-[a-z0-9-]{16,}|AIza[a-z0-9_-]{20,}|"
    r"mch_at_[0-9a-f]{32}\.[a-z0-9_-]{40,}|"
    r"eyJ[a-z0-9_-]{10,}\.[a-z0-9._-]{10,})\b"
)
_VISIBLE_ONE_TIME_SECRET = re.compile(
    r"(?i)(验证码|动态码|支付密码|交易密码|安全码|otp|one[ _-]?time[ _-]?code|"
    r"passcode|pin|cvv|cvc)\s*[:：=]?\s*\d{3,10}"
)


def _safe_visible_text(value: str) -> str:
    result = _VISIBLE_PRIVATE_KEY.sub("[private-key]", value)
    result = _VISIBLE_SECRET.sub(lambda match: f"{match.group(1)}=[secret]", result)
    result = _VISIBLE_TOKEN.sub("[token]", result)
    result = _VISIBLE_ONE_TIME_SECRET.sub("[secret]", result)
    return _VISIBLE_PAYMENT.sub("[payment-number]", result)


_UIA_POWERSHELL = r"""
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$sig='[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();'
$native=Add-Type -MemberDefinition $sig -Name NativeForeground -Namespace Mouchen -PassThru
$hwnd=$native::GetForegroundWindow()
if ($hwnd -eq [IntPtr]::Zero) { '[]'; exit 0 }
$root=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
if ($null -eq $root) { '[]'; exit 0 }
$items=$root.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition
)
$out=[System.Collections.Generic.List[string]]::new()
$seen=[System.Collections.Generic.HashSet[string]]::new()
$maximum=[Math]::Min($items.Count,800)
for($i=0;$i -lt $maximum;$i++) {
  $item=$items.Item($i)
  try { if([bool]$item.Current.IsPassword) { continue } } catch { continue }
  try { if([bool]$item.Current.IsOffscreen) { continue } } catch { continue }
  $parts=[System.Collections.Generic.List[string]]::new()
  try {
    $name=[string]$item.Current.Name
    if(-not [string]::IsNullOrWhiteSpace($name)) { $parts.Add($name) }
  } catch {}
  try {
    $pattern=$null
    if($item.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
      $value=[string]$pattern.Current.Value
      if(-not [string]::IsNullOrWhiteSpace($value)) { $parts.Add($value) }
    }
  } catch {}
  try {
    $pattern=$null
    if($item.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern,[ref]$pattern)) {
      $value=[string]$pattern.DocumentRange.GetText(1200)
      if(-not [string]::IsNullOrWhiteSpace($value)) { $parts.Add($value) }
    }
  } catch {}
  foreach($part in $parts) {
    $clean=($part -replace '\s+',' ').Trim()
    if($clean.Length -gt 1200) { $clean=$clean.Substring(0,1200) }
    if($clean.Length -gt 1 -and $seen.Add($clean)) { $out.Add($clean) }
    if($out.Count -ge 160) { break }
  }
  if($out.Count -ge 160) { break }
}
$out | ConvertTo-Json -Compress
"""


@dataclass(slots=True)
class Signal:
    source: str
    type: str
    facts: dict[str, Any]
    sensitivity: str = "sensitive"
    confidence: float = 1.0
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_local_event(self) -> dict[str, Any]:
        local_id = str(uuid.uuid4())
        return {
            "local_id": local_id,
            "source": self.source,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "facts": self.facts,
            "entities": [],
            "confidence": self.confidence,
            "sensitivity": self.sensitivity,
            "consent_scope": "windows.private_alpha",
            "evidence_ref": f"windows:{local_id}",
        }


@dataclass(frozen=True, slots=True)
class ForegroundWindow:
    process: str
    title: str
    pid: int


class WindowsForegroundReader:
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._configure()

    def read(self) -> ForegroundWindow | None:
        handle = self._user32.GetForegroundWindow()
        if not handle:
            return None
        length = self._user32.GetWindowTextLengthW(handle)
        title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self._user32.GetWindowTextW(handle, title_buffer, len(title_buffer))
        title = title_buffer.value.strip()
        if not title:
            return None
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = self._process_name(pid.value)
        return ForegroundWindow(process=process, title=title[:500], pid=int(pid.value))

    def _process_name(self, pid: int) -> str:
        handle = self._kernel32.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return f"pid-{pid}"
        try:
            size = wintypes.DWORD(32_768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name.casefold()
            return f"pid-{pid}"
        finally:
            self._kernel32.CloseHandle(handle)

    def _configure(self) -> None:
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class WindowsClipboardReader:
    _CF_UNICODETEXT = 13

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._configure()

    def read(self) -> str | None:
        if not self._user32.IsClipboardFormatAvailable(self._CF_UNICODETEXT):
            return None
        if not self._user32.OpenClipboard(None):
            return None
        memory = None
        try:
            handle = self._user32.GetClipboardData(self._CF_UNICODETEXT)
            if not handle:
                return None
            memory = self._kernel32.GlobalLock(handle)
            if not memory:
                return None
            return ctypes.wstring_at(memory)
        finally:
            if memory:
                self._kernel32.GlobalUnlock(handle)
            self._user32.CloseClipboard()

    def _configure(self) -> None:
        self._user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.CloseClipboard.argtypes = []
        self._kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self._kernel32.GlobalLock.restype = wintypes.LPVOID
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


class ActiveWindowCollector:
    def __init__(
        self,
        reader: Callable[[], ForegroundWindow | None] | None = None,
        minimum_session_seconds: float = 5.0,
        maximum_session_seconds: float = 300.0,
    ) -> None:
        native_reader = WindowsForegroundReader()
        self.reader = reader or native_reader.read
        self.minimum_session_seconds = minimum_session_seconds
        self.maximum_session_seconds = maximum_session_seconds
        self._current: ForegroundWindow | None = None
        self._started_at: float | None = None

    @property
    def current(self) -> ForegroundWindow | None:
        return self._current

    def poll(self, now: float | None = None) -> list[Signal]:
        moment = time.monotonic() if now is None else now
        observed = self.reader()
        if observed and observed.pid == os.getpid():
            observed = None
        if self._current is None:
            self._current = observed
            self._started_at = moment if observed else None
            return []
        elapsed = moment - (self._started_at or moment)
        same = observed == self._current
        if same and elapsed < self.maximum_session_seconds:
            return []
        signals = self._finish(elapsed)
        self._current = observed
        self._started_at = moment if observed else None
        return signals

    def flush(self, now: float | None = None) -> list[Signal]:
        if self._current is None or self._started_at is None:
            return []
        moment = time.monotonic() if now is None else now
        signals = self._finish(moment - self._started_at)
        self._current = None
        self._started_at = None
        return signals

    def _finish(self, elapsed: float) -> list[Signal]:
        if self._current is None or elapsed < self.minimum_session_seconds:
            return []
        duration_ms = int(elapsed * 1_000)
        utc_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        timezone_offset = int(utc_offset.total_seconds() / 60)
        return [
            Signal(
                source="windows.foreground",
                type="app.foreground_session",
                facts={
                    "package": self._current.process,
                    "app_label": self._current.title,
                    "window_title": self._current.title,
                    "duration_ms": duration_ms,
                    "timezone_offset_minutes": timezone_offset,
                    "platform": "windows",
                },
                sensitivity="personal",
                confidence=0.98,
            )
        ]


class WindowsUIAutomationReader:
    """Read text exposed by the foreground application's accessibility tree.

    The helper is hidden, read-only and never invokes or focuses a control. It
    deliberately does not use screenshots or simulate user input.
    """

    def __init__(self, timeout_seconds: float = 2.5) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))

    def read(self) -> list[str]:
        if os.name != "nt":
            return []
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    _UIA_POWERSHELL,
                ],
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        try:
            import json

            value = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [" ".join(str(item).split()) for item in value if str(item).strip()]


class VisibleTextCollector:
    """Collect bounded, deduplicated text currently exposed by UI Automation."""

    _CHAT_APPS = {"wechat.exe", "weixin.exe", "wxwork.exe", "dingtalk.exe", "slack.exe"}
    _VIDEO_MARKERS = ("youtube", "bilibili", "腾讯视频", "爱奇艺", "优酷")
    _SEARCH_MARKERS = ("search", "搜索", "百度", "bing", "google")
    _BROWSERS = {"chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"}

    def __init__(
        self,
        foreground_reader: Callable[[], ForegroundWindow | None] | None = None,
        text_reader: Callable[[], list[str]] | None = None,
        *,
        minimum_interval_seconds: float = 5.0,
        duplicate_seconds: float = 10 * 60.0,
        max_chars: int = 12_000,
    ) -> None:
        foreground = WindowsForegroundReader()
        uia = WindowsUIAutomationReader()
        self.foreground_reader = foreground_reader or foreground.read
        self.text_reader = text_reader or uia.read
        self.minimum_interval_seconds = max(1.0, float(minimum_interval_seconds))
        self.duplicate_seconds = max(10.0, float(duplicate_seconds))
        self.max_chars = max(500, min(20_000, int(max_chars)))
        self._last_poll: float | None = None
        self._seen: dict[str, float] = {}

    def poll(
        self,
        excluded_apps: Iterable[str] = (),
        now: float | None = None,
    ) -> list[Signal]:
        moment = time.monotonic() if now is None else float(now)
        if self._last_poll is not None and moment - self._last_poll < self.minimum_interval_seconds:
            return []
        self._last_poll = moment
        try:
            window = self.foreground_reader()
        except OSError:
            return []
        if window is None or window.pid == os.getpid():
            return []
        process = window.process.casefold()
        excluded = {str(item).strip().casefold() for item in excluded_apps}
        if process in excluded or process in {"mouchen.exe", "pythonw.exe", "python.exe"}:
            return []
        try:
            parts = self.text_reader()
        except (OSError, RuntimeError):
            return []
        unique: list[str] = []
        seen_parts: set[str] = set()
        for part in parts:
            clean = " ".join(str(part).split())
            if len(clean) < 2 or clean in seen_parts:
                continue
            seen_parts.add(clean)
            unique.append(clean)
        text = _safe_visible_text("\n".join(unique))[: self.max_chars]
        if not text:
            return []
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = hashlib.sha256(f"{process}\0{window.title}\0{digest}".encode("utf-8")).hexdigest()
        last_seen = self._seen.get(key)
        if last_seen is not None and moment - last_seen < self.duplicate_seconds:
            return []
        self._seen[key] = moment
        self._seen = {
            item: stamp
            for item, stamp in self._seen.items()
            if moment - stamp < self.duplicate_seconds
        }
        kind = self._content_kind(process, window.title)
        session_key = hashlib.sha256(
            f"windows-visible-v1\0{process}\0{window.pid}".encode("utf-8")
        ).hexdigest()[:32]
        return [
            Signal(
                source="windows.visible_text",
                type="ui.visible_text",
                facts={
                    "visible_text": text,
                    "context": "foreground_visible_text",
                    "package": process,
                    "window_title": _safe_visible_text(window.title)[:500],
                    "content_kind": kind,
                    "speaker": "unknown",
                    "message_direction": "unknown",
                    "visible_only": True,
                    "evidence_strength": "contextual",
                    "session_key": session_key,
                    "content_hash": digest,
                    "resolution_state": "unknown",
                    "character_count": len(text),
                    "truncated": sum(len(item) + 1 for item in unique) > len(text),
                },
                sensitivity="restricted",
                confidence=0.88,
            )
        ]

    @classmethod
    def _content_kind(cls, process: str, title: str) -> str:
        folded = title.casefold()
        if process in cls._CHAT_APPS:
            return "chat"
        if any(marker in folded for marker in cls._VIDEO_MARKERS):
            return "video"
        if any(marker in folded for marker in cls._SEARCH_MARKERS):
            return "search"
        if process in cls._BROWSERS:
            return "web_page"
        return "app_ui"


class ClipboardCollector:
    def __init__(self, reader: Callable[[], str | None] | None = None, max_chars: int = 8_000) -> None:
        native_reader = WindowsClipboardReader()
        self.reader = reader or native_reader.read
        self.max_chars = max_chars
        self._last_digest: str | None = None
        self._initialized = False

    def poll(self) -> list[Signal]:
        try:
            value = (self.reader() or "").strip()
        except OSError:
            return []
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""
        if not self._initialized:
            self._last_digest = digest
            self._initialized = True
            return []
        if not value or digest == self._last_digest:
            return []
        self._last_digest = digest
        visible = value[: self.max_chars]
        content_hash = hashlib.sha256(visible.encode("utf-8")).hexdigest()
        return [
            Signal(
                source="windows.clipboard",
                type="ui.visible_text",
                facts={
                    "visible_text": visible,
                    "context": "clipboard",
                    "character_count": len(value),
                    "truncated": len(value) > len(visible),
                    "content_kind": "unknown",
                    "speaker": "unknown",
                    "message_direction": "unknown",
                    "visible_only": False,
                    "evidence_strength": "contextual",
                    "session_key": f"clipboard-{uuid.uuid4().hex[:24]}",
                    "content_hash": content_hash,
                    "resolution_state": "unknown",
                },
                sensitivity="restricted",
                confidence=0.96,
            )
        ]


@dataclass(frozen=True, slots=True)
class ChromiumHistoryProfile:
    browser: str
    profile: str
    history_path: Path


def discover_chromium_history_profiles() -> list[ChromiumHistoryProfile]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    roots = (
        ("Chrome", Path(local) / "Google" / "Chrome" / "User Data"),
        ("Edge", Path(local) / "Microsoft" / "Edge" / "User Data"),
        ("Brave", Path(local) / "BraveSoftware" / "Brave-Browser" / "User Data"),
    )
    result: list[ChromiumHistoryProfile] = []
    for browser, root in roots:
        if not root.is_dir():
            continue
        try:
            directories = sorted(
                (
                    path
                    for path in root.iterdir()
                    if path.is_dir()
                    and path.name != "System Profile"
                    and (path.name == "Default" or path.name.startswith("Profile "))
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue
        for directory in directories:
            history = directory / "History"
            if history.is_file():
                result.append(ChromiumHistoryProfile(browser, directory.name, history))
    return result


class BrowserHistoryCollector:
    """Reads explicitly enabled Chromium history without retaining full URLs."""

    _CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

    def __init__(
        self,
        state_loader: Callable[[], dict[str, Any]] | None = None,
        state_saver: Callable[[dict[str, Any]], None] | None = None,
        profile_provider: Callable[[], list[ChromiumHistoryProfile]] | None = None,
        lookback_days: int = 7,
        initial_limit: int = 300,
        incremental_limit: int = 200,
        minimum_interval_seconds: float = 15.0,
    ) -> None:
        self.state_saver = state_saver or (lambda _: None)
        self.profile_provider = profile_provider or discover_chromium_history_profiles
        self.lookback_days = max(1, min(30, int(lookback_days)))
        self.initial_limit = max(1, min(2_000, int(initial_limit)))
        self.incremental_limit = max(1, min(1_000, int(incremental_limit)))
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._last_poll: float | None = None
        loaded = state_loader() if state_loader else {}
        self._cursors: dict[str, int] = {}
        for key, value in loaded.items() if isinstance(loaded, dict) else ():
            try:
                self._cursors[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                continue

    def poll(
        self,
        now: float | None = None,
        now_utc: datetime | None = None,
        force: bool = False,
    ) -> list[Signal]:
        moment = time.monotonic() if now is None else now
        if (
            not force
            and self._last_poll is not None
            and moment - self._last_poll < self.minimum_interval_seconds
        ):
            return []
        self._last_poll = moment
        current_time = now_utc or datetime.now(timezone.utc)
        signals: list[Signal] = []
        changed = False
        for profile in self.profile_provider():
            key = str(profile.history_path.resolve()).casefold()
            first_poll = key not in self._cursors
            cursor = self._cursors.get(key, 0)
            try:
                rows, next_cursor, historical = self._read_profile(
                    profile,
                    cursor,
                    first_poll,
                    current_time,
                )
            except (OSError, sqlite3.Error, ValueError):
                continue
            if next_cursor != cursor or first_poll:
                self._cursors[key] = next_cursor
                changed = True
            for visit_id, url, title, visit_time, visit_count in rows:
                signal = self._to_signal(
                    profile,
                    int(visit_id),
                    str(url or ""),
                    str(title or ""),
                    int(visit_time or 0),
                    int(visit_count or 0),
                    historical,
                )
                if signal is not None:
                    signals.append(signal)
        if changed:
            self.state_saver(dict(self._cursors))
        return signals

    def _read_profile(
        self,
        profile: ChromiumHistoryProfile,
        cursor: int,
        first_poll: bool,
        now_utc: datetime,
    ) -> tuple[list[tuple[Any, ...]], int, bool]:
        with tempfile.TemporaryDirectory(prefix="mouchen-browser-") as temporary:
            snapshot = Path(temporary) / "History"
            shutil.copy2(profile.history_path, snapshot)
            for suffix in ("-wal", "-shm"):
                source = Path(f"{profile.history_path}{suffix}")
                if source.is_file():
                    try:
                        shutil.copy2(source, Path(f"{snapshot}{suffix}"))
                    except OSError:
                        pass
            connection = sqlite3.connect(snapshot)
            try:
                maximum_id = int(
                    connection.execute("SELECT COALESCE(MAX(id), 0) FROM visits").fetchone()[0]
                )
                historical = first_poll or maximum_id < cursor
                if historical:
                    cutoff = self._to_chromium_time(now_utc - timedelta(days=self.lookback_days))
                    rows = connection.execute(
                        """SELECT visits.id, urls.url, urls.title, visits.visit_time, urls.visit_count
                           FROM visits JOIN urls ON urls.id=visits.url
                           WHERE visits.visit_time>=?
                           ORDER BY visits.visit_time DESC, visits.id DESC
                           LIMIT ?""",
                        (cutoff, self.initial_limit),
                    ).fetchall()
                    rows.reverse()
                    return rows, maximum_id, True
                rows = connection.execute(
                    """SELECT visits.id, urls.url, urls.title, visits.visit_time, urls.visit_count
                       FROM visits JOIN urls ON urls.id=visits.url
                       WHERE visits.id>?
                       ORDER BY visits.id ASC
                       LIMIT ?""",
                    (cursor, self.incremental_limit),
                ).fetchall()
                next_cursor = int(rows[-1][0]) if rows else cursor
                return rows, next_cursor, False
            finally:
                connection.close()

    def _to_signal(
        self,
        profile: ChromiumHistoryProfile,
        visit_id: int,
        url: str,
        title: str,
        visit_time: int,
        visit_count: int,
        historical: bool,
    ) -> Signal | None:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        domain = parsed.hostname.casefold().removeprefix("www.")[:253]
        page_title = " ".join(title.split())[:500]
        visible_text = page_title if page_title else domain
        if page_title and domain not in page_title.casefold():
            visible_text = f"{page_title} · {domain}"
        folded = f"{domain} {page_title}".casefold()
        content_kind = (
            "video"
            if any(marker in folded for marker in ("youtube", "bilibili", "视频"))
            else "search"
            if any(marker in folded for marker in ("search", "搜索", "百度", "bing", "google"))
            else "web_page"
        )
        content_hash = hashlib.sha256(visible_text.encode("utf-8")).hexdigest()
        session_key = hashlib.sha256(
            f"browser-v1\0{profile.browser}\0{profile.profile}\0{domain}".encode("utf-8")
        ).hexdigest()[:32]
        return Signal(
            source="windows.browser_history",
            type="ui.visible_text",
            facts={
                "visible_text": visible_text,
                "context": "browser_history",
                "browser": profile.browser,
                "profile": profile.profile,
                "site_domain": domain,
                "page_title": page_title,
                "visit_id": visit_id,
                "visit_count": visit_count,
                "historical_backfill": historical,
                "content_kind": content_kind,
                "speaker": "author",
                "message_direction": "unknown",
                "visible_only": False,
                "evidence_strength": "contextual",
                "session_key": session_key,
                "content_hash": content_hash,
                "resolution_state": "unknown",
            },
            sensitivity="personal",
            confidence=0.98,
            occurred_at=self._from_chromium_time(visit_time).isoformat(),
        )

    @classmethod
    def _from_chromium_time(cls, value: int) -> datetime:
        try:
            return cls._CHROMIUM_EPOCH + timedelta(microseconds=max(0, int(value)))
        except (OverflowError, ValueError):
            return datetime.now(timezone.utc)

    @classmethod
    def _to_chromium_time(cls, value: datetime) -> int:
        normalized = value.astimezone(timezone.utc)
        return int((normalized - cls._CHROMIUM_EPOCH).total_seconds() * 1_000_000)


class FileActivityCollector:
    _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".py", ".js", ".ts"}
    _IGNORED_PARTS = {".git", ".idea", ".venv", "node_modules", "build", "__pycache__"}

    def __init__(self, max_files: int = 2_500, max_content_chars: int = 12_000) -> None:
        self.max_files = max_files
        self.max_content_chars = max_content_chars
        self._state: dict[str, tuple[int, int]] = {}
        self._initialized = False
        self._signature: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def poll(
        self,
        folders: Iterable[str],
        extensions: Iterable[str],
        include_content: bool,
    ) -> list[Signal]:
        roots = tuple(sorted(str(Path(item).expanduser()) for item in folders if item))
        suffixes = tuple(sorted(item.casefold() for item in extensions))
        signature = (roots, suffixes)
        if signature != self._signature:
            self._state = {}
            self._initialized = False
            self._signature = signature
        current: dict[str, tuple[int, int]] = {}
        signals: list[Signal] = []
        for root_text in roots:
            root = Path(root_text)
            if not root.is_dir():
                continue
            for path in self._iter_files(root, set(suffixes)):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = str(path.resolve())
                stamp = (stat.st_mtime_ns, stat.st_size)
                current[key] = stamp
                previous = self._state.get(key)
                if self._initialized and previous != stamp:
                    action = "created" if previous is None else "modified"
                    signals.append(self._signal(root, path, action, stat.st_size, include_content))
                if len(current) >= self.max_files:
                    break
            if len(current) >= self.max_files:
                break
        self._state = current
        self._initialized = True
        return signals

    def _iter_files(self, root: Path, extensions: set[str]) -> Iterable[Path]:
        try:
            candidates = root.rglob("*")
            for path in candidates:
                if any(part.casefold() in self._IGNORED_PARTS for part in path.parts):
                    continue
                if not path.is_file() or path.name.startswith("~$") or path.suffix.casefold() == ".tmp":
                    continue
                if extensions and path.suffix.casefold() not in extensions:
                    continue
                yield path
        except (OSError, PermissionError):
            return

    def _signal(self, root: Path, path: Path, action: str, size: int, include_content: bool) -> Signal:
        relative = str(path.relative_to(root))
        content = self._read_text(path) if include_content else ""
        description = content or f"{path.name} file {action}"
        content_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
        session_key = hashlib.sha256(
            f"file-v1\0{relative.casefold()}".encode("utf-8")
        ).hexdigest()[:32]
        return Signal(
            source="windows.file_activity",
            type="ui.visible_text",
            facts={
                "visible_text": description,
                "file_name": path.name,
                "relative_path": relative,
                "extension": path.suffix.casefold(),
                "action": action,
                "size_bytes": size,
                "content_included": bool(content),
                "content_kind": "document",
                "speaker": "unknown",
                "message_direction": "unknown",
                "visible_only": False,
                "evidence_strength": "contextual",
                "session_key": session_key,
                "content_hash": content_hash,
                "resolution_state": "unknown",
            },
            sensitivity="restricted" if content else "personal",
            confidence=0.99,
        )

    def _read_text(self, path: Path) -> str:
        if path.suffix.casefold() not in self._TEXT_EXTENSIONS:
            return ""
        try:
            raw = path.read_bytes()[: self.max_content_chars * 4]
        except OSError:
            return ""
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                return raw.decode(encoding)[: self.max_content_chars]
            except UnicodeDecodeError:
                continue
        return ""
