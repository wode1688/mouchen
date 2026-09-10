from __future__ import annotations

import os
import sys
from pathlib import Path


def default_launcher_path() -> Path:
    """Return the relocatable launcher used by the per-user startup entry."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve().parents[1] / "Start-Mouchen-Windows.cmd"


class StartupManager:
    """Owns one narrowly scoped per-user Windows startup command file."""

    def __init__(
        self,
        startup_dir: Path | None = None,
        launcher_path: Path | None = None,
    ) -> None:
        self.startup_dir = startup_dir or self._default_startup_dir()
        self.launcher_path = launcher_path or default_launcher_path()
        self.registration_path = self.startup_dir / "Mouchen-Desktop.cmd"

    def is_enabled(self) -> bool:
        if not self.registration_path.is_file():
            return False
        try:
            return self.registration_path.read_text(encoding="utf-8") == self._content()
        except OSError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            if not self.launcher_path.is_file():
                raise FileNotFoundError(f"AI替身启动器不存在：{self.launcher_path}")
            self.startup_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.registration_path.with_suffix(".tmp")
            temporary.write_text(self._content(), encoding="utf-8", newline="\r\n")
            temporary.replace(self.registration_path)
            return
        if self.registration_path.exists():
            self.registration_path.unlink()

    def _content(self) -> str:
        launcher = str(self.launcher_path.resolve()).replace("%", "%%")
        return f'@echo off\nstart "" "{launcher}"\n'

    @staticmethod
    def _default_startup_dir() -> Path:
        roaming = os.environ.get("APPDATA")
        if not roaming:
            raise RuntimeError("APPDATA is unavailable")
        return Path(roaming) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
