from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes

from .i18n import tr


_WM_APP = 0x8000
_WM_TRAY = _WM_APP + 41
_WM_BALLOON = _WM_APP + 42
_WM_TOOLTIP = _WM_APP + 43
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_COMMAND = 0x0111
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010

_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_NIF_INFO = 0x00000010
_NIIF_INFO = 0x00000001
_NIIF_WARNING = 0x00000002
_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_IDI_APPLICATION = 32512


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid),
        ("hBalloonIcon", wintypes.HICON),
    ]


_LRESULT = ctypes.c_ssize_t
if hasattr(ctypes, "WINFUNCTYPE"):
    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
else:
    # Non-Windows (CI/test) import path: keep the module importable so the UI
    # test suite can run headless. start() already refuses when os.name != "nt",
    # so this stub calling convention never talks to Windows.
    _WNDPROC = ctypes.CFUNCTYPE(
        _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        # HCURSOR is a HANDLE alias that ctypes only defines on Windows.
        ("hCursor", getattr(wintypes, "HCURSOR", wintypes.HANDLE)),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class TrayIcon:
    def __init__(self, tooltip: str | None = None) -> None:
        self.tooltip = tooltip or tr("AI替身 · 事业助手")
        self.actions: queue.Queue[str] = queue.Queue()
        self._balloons: queue.Queue[
            tuple[str, str, int, threading.Event, dict[str, object]]
        ] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._nid: _NotifyIconData | None = None
        self._wndproc: _WNDPROC | None = None
        self.error: str | None = None
        self.last_notification_error: str | None = None
        self._pending_tooltip: str | None = None
        self.available = False

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="mouchen-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)
        return self.available

    def notify(self, title: str, message: str, level: int = 1) -> bool:
        if not self.available or not self._hwnd:
            self.last_notification_error = self.error or "Windows tray is unavailable"
            return False
        acknowledged = threading.Event()
        outcome: dict[str, object] = {"cancelled": False}
        self._balloons.put((title[:63], message[:255], level, acknowledged, outcome))
        if not ctypes.windll.user32.PostMessageW(self._hwnd, _WM_BALLOON, 0, 0):
            outcome["cancelled"] = True
            self.last_notification_error = "Unable to post notification to the tray window"
            return False
        if not acknowledged.wait(timeout=2):
            outcome["cancelled"] = True
            self.last_notification_error = "Tray notification acknowledgement timed out"
            return False
        delivered = bool(outcome.get("delivered"))
        if delivered:
            self.last_notification_error = None
        else:
            self.last_notification_error = str(outcome.get("error") or "Windows rejected the notification")
        return delivered

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    def refresh_locale(self) -> None:
        """Update visible tray chrome without restarting collection."""
        self.tooltip = tr("AI替身 · 事业助手")
        self._pending_tooltip = self.tooltip
        if self.available and self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_TOOLTIP, 0, 0)

    def _run(self) -> None:
        try:
            self._configure_api()
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            instance = kernel32.GetModuleHandleW(None)
            class_name = f"MouchenTrayWindow{os.getpid()}"
            self._wndproc = _WNDPROC(self._window_proc)
            window_class = _WndClass(
                0,
                self._wndproc,
                0,
                0,
                instance,
                None,
                None,
                None,
                None,
                class_name,
            )
            atom = user32.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError()
            hwnd = user32.CreateWindowExW(
                0, class_name, "Mouchen", 0, 0, 0, 0, 0, None, None, instance, None
            )
            if not hwnd:
                raise ctypes.WinError()
            icon_resource = ctypes.cast(ctypes.c_void_p(_IDI_APPLICATION), wintypes.LPCWSTR)
            icon = user32.LoadIconW(None, icon_resource)
            nid = _NotifyIconData()
            nid.cbSize = ctypes.sizeof(_NotifyIconData)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            nid.uCallbackMessage = _WM_TRAY
            nid.hIcon = icon
            nid.szTip = self.tooltip[:127]
            if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError()
            self._hwnd = hwnd
            self._nid = nid
            self.available = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = str(exc)
            self.available = False
            self._ready.set()

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        if message == _WM_TRAY:
            if lparam == _WM_LBUTTONDBLCLK:
                self.actions.put("show")
                return 0
            if lparam == _WM_RBUTTONUP:
                self._show_menu(hwnd)
                return 0
        elif message == _WM_COMMAND:
            return 0
        elif message == _WM_BALLOON:
            self._show_next_balloon(shell32)
            return 0
        elif message == _WM_TOOLTIP:
            if self._nid is not None and self._pending_tooltip is not None:
                self._nid.uFlags = _NIF_TIP
                self._nid.szTip = self._pending_tooltip[:127]
                shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid))
                self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
                self._pending_tooltip = None
            return 0
        elif message == _WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        elif message == _WM_DESTROY:
            if self._nid is not None:
                shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
            self.available = False
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        try:
            user32.AppendMenuW(menu, _MF_STRING, 1, tr("打开AI替身"))
            user32.AppendMenuW(menu, _MF_STRING, 2, tr("暂停 / 继续"))
            user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, _MF_STRING, 3, tr("退出"))
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            action = {1: "show", 2: "toggle_pause", 3: "quit"}.get(command)
            if action:
                self.actions.put(action)
        finally:
            user32.DestroyMenu(menu)

    def _show_next_balloon(self, shell32: ctypes.LibraryLoader) -> None:
        if self._nid is None:
            return
        while True:
            try:
                title, message, level, acknowledged, outcome = self._balloons.get_nowait()
            except queue.Empty:
                return
            if not outcome.get("cancelled"):
                break
            acknowledged.set()
        try:
            self._nid.uFlags = _NIF_INFO
            self._nid.szInfoTitle = title
            self._nid.szInfo = message
            self._nid.dwInfoFlags = _NIIF_WARNING if level >= 3 else _NIIF_INFO
            delivered = bool(shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(self._nid)))
            outcome["delivered"] = delivered
            if not delivered:
                outcome["error"] = "Shell_NotifyIconW rejected NIM_MODIFY"
        except Exception as exc:
            outcome["delivered"] = False
            outcome["error"] = str(exc)
        finally:
            self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            acknowledged.set()

    @staticmethod
    def _configure_api() -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = _LRESULT
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIconData)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
