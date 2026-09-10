"""An isolated Windows profile for local rules and cloud transport only."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .security import WindowsDataProtector
from .settings import AppSettings, SettingsRepository

APP_ROOT = Path(__file__).resolve().parents[2]


def default_profile() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local:
        raise RuntimeError("Local desktop mode requires Windows and its private user data directory")
    return Path(local) / "Mouchen" / "LocalMode"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _protect(value: str, protector) -> str:
    return base64.b64encode(protector.protect(value.encode("utf-8"))).decode("ascii")


def _unprotect(value: str, protector) -> str:
    return protector.unprotect(base64.b64decode(value, validate=True)).decode("utf-8")


def setup(profile: Path, *, port: int = 8788, device_id: str = "desktop", protector=None) -> Path:
    """Provision normal account sessions without modifying an existing profile.

    The random recovery password and access token remain DPAPI protected. No
    collection consent is created, and no cloud model permission is inherited.
    """
    if not 1024 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", device_id):
        raise ValueError("invalid local port or device id")
    profile = profile.expanduser().resolve()
    source_root = APP_ROOT.parent
    if profile == source_root or source_root in profile.parents:
        raise ValueError("private profile must be outside the source repository")
    config_path = profile / "bridge-config.json"
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing.get("backend_url") != f"http://127.0.0.1:{port}" or existing.get("desktop_device_id") != device_id:
            raise ValueError("existing profile has a different port or device; use its original arguments")
        return config_path
    protector = protector or WindowsDataProtector()
    database = profile / "backend" / "mouchen-local.db"
    if database.exists():
        raise RuntimeError("unfinished local profile requires recovery; existing database was preserved")
    sys.path.insert(0, str(APP_ROOT / "backend"))
    from app.storage import Repository

    password = secrets.token_urlsafe(36)
    username = "local-owner"
    repository = Repository(database)
    try:
        issued = repository.register_account(
            username=username, password=password, device_id=device_id,
            device_name="Local desktop", expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        )
        session = {
            "user_id": issued.principal.user_id, "username": username,
            "device_id": device_id, "locale": "zh-CN",
        }
        desktop_dir = profile / "desktop"
        settings = AppSettings(backend_url=f"http://127.0.0.1:{port}")
        settings.apply_session(issued.access_token, session)
        settings.disable_unconsented_collection()
        SettingsRepository(desktop_dir / "settings.json", protector=protector).save(settings)
        # Register the same device identity in the native store so attention
        # acknowledgments are bound to this authenticated session's device.
        from .store import EventStore
        store = EventStore(desktop_dir / "mouchen-desktop.db", protector=protector)
        try:
            store.save_state("device_identity:windows", {"device_id": device_id, "platform": "windows"})
        finally:
            store.close()
        config = {
            "version": 1, "backend_url": settings.backend_url,
            "desktop_device_id": device_id, "session_user_id": issued.principal.user_id,
            "bearer_token_protected": _protect(issued.access_token, protector),
            "token_protection": "windows-dpapi", "dpapi_entropy": "mouchen-desktop-v1",
            "desktop_data_dir": str(desktop_dir), "database_path": str(database),
            "username": username, "recovery_password_protected": _protect(password, protector),
        }
        _write_json(config_path, config)
        return config_path
    finally:
        repository.close()


def runtime_environment(config: dict) -> dict[str, str]:
    # Do not inherit a different deployment's auth, proxy, model, or database
    # switches. The gateway also refuses models when the profile is rules-only.
    env = {key: value for key, value in os.environ.items() if not key.startswith("MOUCHEN_")}
    env.update({
        "MOUCHEN_MODEL_PROVIDER": "rules", "MOUCHEN_LEGACY_AUTH_ENABLED": "false",
        "MOUCHEN_REGISTRATION_MODE": "closed", "MOUCHEN_ALLOW_OPEN_REGISTRATION": "false",
        "MOUCHEN_ANALYSIS_BACKGROUND_ENABLED": "false", "MOUCHEN_CLAUDE_REVIEW_PROVIDER": "disabled",
        "MOUCHEN_CODEX_CLI_ENABLED": "false", "MOUCHEN_DB_PATH": config["database_path"],
        "MOUCHEN_DESKTOP_DATA_DIR": config["desktop_data_dir"],
        "MOUCHEN_BACKEND_URL": config["backend_url"],
        "MOUCHEN_RELAY_DEVICE_ID": config["desktop_device_id"],
    })
    return env


def start(profile: Path, *, open_ui: bool = True) -> None:
    from urllib.parse import urlparse
    import httpx

    profile = profile.expanduser().resolve()
    config = json.loads((profile / "bridge-config.json").read_text(encoding="utf-8"))
    endpoint = urlparse(config["backend_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port or endpoint.path:
        raise ValueError("the local profile must use a loopback endpoint")
    protector = WindowsDataProtector()
    token = _unprotect(config["bearer_token_protected"], protector)
    env = runtime_environment(config)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=2) as client:
        def health():
            try:
                response = client.get(config["backend_url"] + "/health")
                return response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None

        current = health()
        process = None
        if current is None:
            log_path = profile / "backend-runtime.log"
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(endpoint.port),
                     "--no-access-log"], cwd=APP_ROOT / "backend", env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=creationflags,
                )
            for _ in range(80):
                current = health()
                if current is not None:
                    break
                if process.poll() is not None:
                    raise RuntimeError("local backend failed; inspect its private runtime log")
                time.sleep(.25)
        if not current or current.get("model_provider") != "rules" or current.get("runtime_contract") != "proactive-analysis-v2":
            raise RuntimeError("local port is unavailable or belongs to a different backend")
        session_response = client.get(config["backend_url"] + "/v1/session",
                                      headers={"Authorization": f"Bearer {token}"})
        if session_response.status_code != 200:
            raise RuntimeError("local session is expired or signed out; sign in using this profile's recovery credentials")
        if session_response.json().get("user_id") != config["session_user_id"]:
            raise RuntimeError("local backend account does not match the private profile")
    if process is not None:
        _write_json(profile / "backend-process.json", {"pid": process.pid, "backend_url": config["backend_url"]})
    if open_ui:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pythonw if pythonw.exists() else Path(sys.executable)),
                          str(APP_ROOT / "desktop" / "MouchenDesktop.pyw")],
                         cwd=APP_ROOT / "desktop", env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or start the isolated local AI Twin profile")
    parser.add_argument("action", choices=("setup", "start", "run"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--device-id", default="desktop")
    parser.add_argument("--no-ui", action="store_true")
    args = parser.parse_args(argv)
    profile = args.profile or default_profile()
    if args.action in {"setup", "run"}:
        setup(profile, port=args.port, device_id=args.device_id)
    if args.action in {"start", "run"}:
        start(profile, open_ui=not args.no_ui)
    print("Local profile ready. Credentials remain protected in its private directory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
