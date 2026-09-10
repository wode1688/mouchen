from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes

from .i18n import tr


MUTEX_NAME = "Local\\MouchenDesktopPrivateAlpha"


class SingleInstance:
    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.handle: int | None = None
        self.already_running = False
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = kernel32.GetLastError() == self._ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def show_already_running_message() -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, tr("AI替身已经在运行。"), tr("AI替身"), 0x40)


def run_single_instance(
    application: Callable[[], None],
    *,
    instance_factory: Callable[[], SingleInstance] = SingleInstance,
    already_running_handler: Callable[[], None] = show_already_running_message,
) -> bool:
    """Run an application while holding the desktop process mutex."""

    instance = instance_factory()
    try:
        if instance.already_running:
            already_running_handler()
            return False
        application()
        return True
    finally:
        instance.close()
