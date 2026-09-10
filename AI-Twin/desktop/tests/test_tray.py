from __future__ import annotations

import os
import threading

import pytest

from mouchen_desktop.tray import TrayIcon, _NotifyIconData


def test_unavailable_tray_returns_a_traceable_failure():
    tray = TrayIcon("Mouchen tray test")

    assert tray.notify("Title", "Message") is False
    assert tray.last_notification_error == "Windows tray is unavailable"


def test_cancelled_balloon_does_not_block_the_next_request():
    tray = TrayIcon("Mouchen tray test")
    tray._nid = _NotifyIconData()
    cancelled_ack = threading.Event()
    delivered_ack = threading.Event()
    tray._balloons.put(("old", "old", 1, cancelled_ack, {"cancelled": True}))
    delivered = {"cancelled": False}
    tray._balloons.put(("new", "new", 1, delivered_ack, delivered))

    class Shell:
        calls = 0

        def Shell_NotifyIconW(self, operation, payload):
            self.calls += 1
            return True

    shell = Shell()
    tray._show_next_balloon(shell)

    assert cancelled_ack.is_set()
    assert delivered_ack.is_set()
    assert delivered["delivered"] is True
    assert shell.calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows tray integration")
def test_tray_starts_on_64_bit_windows():
    tray = TrayIcon("Mouchen tray test")
    try:
        assert tray.start() is True, tray.error
        assert tray.available is True
        assert tray._hwnd
    finally:
        tray.stop()
