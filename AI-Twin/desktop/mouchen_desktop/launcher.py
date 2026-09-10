from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from .single_instance import run_single_instance


def _self_test_report_path(arguments: list[str]) -> Path | None:
    if "--self-test-report" not in arguments:
        return None
    index = arguments.index("--self-test-report")
    if index + 1 >= len(arguments):
        raise ValueError("--self-test-report requires a path")
    return Path(arguments[index + 1]).expanduser().resolve()


def run_packaged_self_test(arguments: list[str] | None = None) -> int:
    """Verify the frozen package without initializing the graphical stack."""
    from . import __version__
    from .settings import AppSettings

    args = list(sys.argv[1:] if arguments is None else arguments)
    report_path = _self_test_report_path(args)
    settings = AppSettings()
    settings.validate()
    payload = {
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "entrypoint": Path(sys.executable).name,
        # The real AccountDialog is imported, rendered, and inspected by the
        # separate UI smoke probe.  This probe deliberately avoids querying
        # the frozen import graph: Python 3.14 may raise ValueError for modules
        # preloaded by a freezer with no __spec__.
        "first_run_fields": [
            "service_address",
            "username",
            "password",
            "password_confirmation",
            "registration_code",
        ],
        "first_run_actions": ["login", "register"],
        "embedded_credentials": bool(settings.bearer_token or settings.session_user_id),
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(report_path)
    # Source-level tests exercise the same probe before freezing; the build
    # script separately requires payload["frozen"] for the packaged artifact.
    return 0 if not payload["embedded_credentials"] else 3


def _run_probe_safely(name: str, callback) -> int:
    """Turn probe failures into a report and exit code, never a modal traceback."""
    try:
        return int(callback())
    except BaseException as exc:  # A windowed executable must not block on an error dialog.
        report_path: Path | None = None
        try:
            report_path = _self_test_report_path(list(sys.argv[1:]))
        except BaseException:
            pass
        if report_path is not None:
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = report_path.with_suffix(report_path.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(
                        {
                            "probe": name,
                            "success": False,
                            "error_type": type(exc).__name__,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                temporary.replace(report_path)
            except BaseException:
                pass
        return 90


def run_packaged_ui_smoke(arguments: list[str] | None = None) -> int:
    """Open the real first-run account dialog without loading user state."""
    import tkinter as tk

    from .settings import AppSettings
    from .ui import AccountDialog

    args = list(sys.argv[1:] if arguments is None else arguments)
    report_path = _self_test_report_path(args)
    root = tk.Tk()
    root.geometry("1x1+-100+-100")
    root.update_idletasks()
    dialog = AccountDialog(root, SimpleNamespace(settings=AppSettings()))
    try:
        root.update_idletasks()
        root.update()
        password_mask = str(dialog.password_entry.cget("show"))
        password_confirmation_mask = str(
            dialog.password_confirmation_entry.cget("show")
        )
        payload = {
            "window_visible": bool(dialog.window.winfo_viewable()),
            "title": str(dialog.window.title()),
            "service_address": str(dialog.backend.get()),
            "login_action": str(dialog.login_button.cget("text")),
            "register_action": str(dialog.register_button.cget("text")),
            "password_masked": bool(password_mask)
            and password_mask == password_confirmation_mask,
        }
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = report_path.with_suffix(report_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(report_path)
        return 0 if all(
            (
                payload["window_visible"],
                payload["service_address"],
                payload["login_action"],
                payload["register_action"],
                payload["password_masked"],
            )
        ) else 4
    finally:
        dialog.window.destroy()
        root.destroy()


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _run_probe_safely("self-test", run_packaged_self_test)
    if "--ui-smoke" in sys.argv[1:]:
        return _run_probe_safely("ui-smoke", run_packaged_ui_smoke)
    from .ui import run

    run_single_instance(run)
    return 0
