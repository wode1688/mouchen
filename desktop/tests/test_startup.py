from __future__ import annotations

from pathlib import Path

from mouchen_desktop.startup import StartupManager, default_launcher_path


def test_powershell_launcher_honors_remote_https_settings_before_local_model_setup():
    script = (
        Path(__file__).resolve().parents[1]
        / "Start-Mouchen-Windows.ps1"
    ).read_text(encoding="utf-8")

    remote_branch = script.index("if ($null -ne $remoteBackendUri)")
    provider_setup = script.index("$env:MOUCHEN_MODEL_PROVIDER")
    codex_check = script.index('if ($expectedModelProvider -eq "codex_cli")')

    assert 'Mouchen\\Desktop\\settings.json' in script
    assert '$candidateUri.Scheme -eq "https"' in script
    assert "Get-MouchenBackendHealth $configuredBackendUrl 5" in script
    assert remote_branch < provider_setup < codex_check
    assert "Start-Process -FilePath $pythonw" in script[remote_branch:provider_setup]


def test_startup_manager_registers_only_its_owned_launcher(tmp_path):
    launcher = tmp_path / "Start-Mouchen-Windows.cmd"
    launcher.write_text("@echo off\n", encoding="utf-8")
    startup = tmp_path / "Startup"
    manager = StartupManager(startup_dir=startup, launcher_path=launcher)

    assert manager.is_enabled() is False
    manager.set_enabled(True)

    assert manager.is_enabled() is True
    content = manager.registration_path.read_text(encoding="utf-8")
    assert str(launcher.resolve()) in content
    assert manager.registration_path.name == "Mouchen-Desktop.cmd"

    manager.set_enabled(False)
    assert manager.registration_path.exists() is False


def test_packaged_startup_points_to_the_frozen_executable(monkeypatch, tmp_path):
    executable = tmp_path / "Mouchen.exe"
    monkeypatch.setattr("mouchen_desktop.startup.sys.frozen", True, raising=False)
    monkeypatch.setattr("mouchen_desktop.startup.sys.executable", str(executable))

    assert default_launcher_path() == executable.resolve()
