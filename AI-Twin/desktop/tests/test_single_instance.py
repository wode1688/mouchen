from __future__ import annotations

import ast
import os
from pathlib import Path
from uuid import uuid4

import pytest

from mouchen_desktop.single_instance import SingleInstance, run_single_instance


DESKTOP_ROOT = Path(__file__).resolve().parents[1]


class FakeInstance:
    def __init__(self, *, already_running: bool = False) -> None:
        self.already_running = already_running
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_single_instance_runner_holds_mutex_until_application_returns():
    instance = FakeInstance()
    observed = []

    started = run_single_instance(
        lambda: observed.append(instance.closed),
        instance_factory=lambda: instance,
        already_running_handler=lambda: pytest.fail("unexpected duplicate"),
    )

    assert started is True
    assert observed == [False]
    assert instance.closed is True


def test_single_instance_runner_closes_duplicate_handle_without_running_app():
    instance = FakeInstance(already_running=True)
    observed = []

    started = run_single_instance(
        lambda: pytest.fail("duplicate instance started the application"),
        instance_factory=lambda: instance,
        already_running_handler=lambda: observed.append("notified"),
    )

    assert started is False
    assert observed == ["notified"]
    assert instance.closed is True


def test_single_instance_runner_releases_mutex_when_application_fails():
    instance = FakeInstance()

    def fail() -> None:
        raise RuntimeError("startup failed")

    with pytest.raises(RuntimeError, match="startup failed"):
        run_single_instance(
            fail,
            instance_factory=lambda: instance,
            already_running_handler=lambda: None,
        )

    assert instance.closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex behavior")
def test_windows_named_mutex_detects_existing_owner_and_releases_handles():
    name = f"Local\\MouchenDesktopTest-{uuid4()}"
    first = SingleInstance(name)
    second = SingleInstance(name)
    try:
        assert first.already_running is False
        assert second.already_running is True
        second.close()

        while_first_is_open = SingleInstance(name)
        try:
            assert while_first_is_open.already_running is True
        finally:
            while_first_is_open.close()
    finally:
        second.close()
        first.close()

    after_release = SingleInstance(name)
    try:
        assert after_release.already_running is False
    finally:
        after_release.close()


def test_both_python_entrypoints_delegate_to_shared_launcher():
    for relative_path, expected_module in (
        ("MouchenDesktop.pyw", "mouchen_desktop.launcher"),
        ("mouchen_desktop/__main__.py", "launcher"),
    ):
        tree = ast.parse((DESKTOP_ROOT / relative_path).read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert any(
            node.module == expected_module
            and any(alias.name == "main" for alias in node.names)
            for node in imports
        )


def test_powershell_checks_existing_mutex_before_provider_or_backend_work():
    source = (DESKTOP_ROOT / "Start-Mouchen-Windows.ps1").read_text(encoding="utf-8")

    open_existing = source.index("[System.Threading.Mutex]::OpenExisting")
    early_exit = source.index("exit 0", open_existing)
    provider_configuration = source.index("$env:MOUCHEN_MODEL_PROVIDER")
    backend_probe = source.index("Get-MouchenBackendHealth")

    assert open_existing < early_exit < provider_configuration
    assert open_existing < early_exit < backend_probe
    assert "$existingInstance.Dispose()" in source[open_existing:early_exit]
