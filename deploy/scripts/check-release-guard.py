#!/usr/bin/env python3
"""Local, credential-free release-guard syntax and reversibility check."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent
GUARD_PATH = SCRIPT_DIR / "sqlite_release_guard.py"
IMAGE_GUARD_PATH = SCRIPT_DIR / "runtime_image_guard.py"
SHELL_SCRIPTS = (
    SCRIPT_DIR / "backup.sh",
    SCRIPT_DIR / "preflight-upgrade.sh",
    SCRIPT_DIR / "rollback.sh",
    SCRIPT_DIR / "deploy.sh",
    SCRIPT_DIR / "capture-compose-contract.sh",
    SCRIPT_DIR / "verify.sh",
    SCRIPT_DIR / "invites.sh",
)


def _load_guard():
    specification = importlib.util.spec_from_file_location("release_guard_check", GUARD_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load SQLite release guard")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_image_guard():
    specification = importlib.util.spec_from_file_location(
        "runtime_image_guard_check", IMAGE_GUARD_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load runtime image guard")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _create_database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        database.execute("INSERT INTO marker(value) VALUES(?)", (value,))
        database.commit()


def _marker(path: Path) -> str:
    with closing(sqlite3.connect(path)) as database:
        return str(database.execute("SELECT value FROM marker").fetchone()[0])


def main() -> int:
    for python_script in (GUARD_PATH, IMAGE_GUARD_PATH, Path(__file__).resolve()):
        ast.parse(python_script.read_text(encoding="utf-8"), filename=str(python_script))

    shell = shutil.which("sh")
    if shell:
        for script in SHELL_SCRIPTS:
            subprocess.run([shell, "-n", str(script)], check=True)

    deploy = (SCRIPT_DIR / "deploy.sh").read_text(encoding="utf-8")
    rollback = (SCRIPT_DIR / "rollback.sh").read_text(encoding="utf-8")
    backup = (SCRIPT_DIR / "backup.sh").read_text(encoding="utf-8")
    preflight = (SCRIPT_DIR / "preflight-upgrade.sh").read_text(encoding="utf-8")
    capture = (SCRIPT_DIR / "capture-compose-contract.sh").read_text(encoding="utf-8")
    verify = (SCRIPT_DIR / "verify.sh").read_text(encoding="utf-8")
    invites = (SCRIPT_DIR / "invites.sh").read_text(encoding="utf-8")
    image_guard = IMAGE_GUARD_PATH.read_text(encoding="utf-8")
    required = (
        (deploy, 'backup.sh" --print-path'),
        (deploy, 'MOUCHEN_BACKUP_HELPER_IMAGE="$previous_image_protection"'),
        (deploy, '"$IMAGE_GUARD" protect'),
        (deploy, '"$IMAGE_GUARD" verify'),
        (deploy, 'preflight-upgrade.sh"'),
        (preflight, "--memory 2g"),
        (preflight, "size=64m,uid=10001,gid=10001,mode=1700"),
        (preflight, "size=1g,uid=10001,gid=10001,mode=0700"),
        (deploy, 'rollback.sh" "$state_file"'),
        (backup, '$volume_name:/source:ro'),
        (backup, '$stage_dir:/host-backups:rw'),
        (backup, '--user 10001:10001'),
        (backup, 'chown 0:0 "$host_partial"'),
        (backup, 'finalize-artifacts --database "$stage_partial"'),
        (backup, "unknown SQLite staging artifact"),
        (rollback, '$volume_name:/data:rw'),
        (rollback, '--user 10001:10001'),
        (rollback, '$restore_input:/restore/mouchen.db:ro'),
        (rollback, 'install -o 10001 -g 10001 -m 0400 "$backup" "$restore_input"'),
        (rollback, "--expected-parent /data"),
        (deploy, "active-compose.state"),
        (deploy, "previous_compose_name"),
        (rollback, "previous_compose_name"),
        (rollback, 'MOUCHEN_COMPOSE_FILE="$previous_contract"'),
        (rollback, "rollback would re-enable the retired legacy owner token"),
        (rollback, '"$IMAGE_GUARD" verify --probe'),
        (capture, 'com.docker.compose.config-hash'),
        (verify, "disabled legacy owner token still grants API access"),
        (invites, "COMPOSE_PROJECT=mouchen"),
        (invites, '-p "$COMPOSE_PROJECT"'),
    )
    if any(marker not in text for text, marker in required):
        raise RuntimeError("a mandatory release gate is missing")
    if not (
        deploy.index('"$IMAGE_GUARD" protect')
        < deploy.index("compose build backend")
        < deploy.index('"$IMAGE_GUARD" verify')
    ):
        raise RuntimeError("previous image must be protected before the mutable build")
    if '"docker", "image", "tag"' not in image_guard:
        raise RuntimeError("runtime image guard does not create a protection tag")
    for forbidden_image_action in (
        '"docker", "image", "rm"',
        '"docker", "image", "prune"',
    ):
        if forbidden_image_action in image_guard:
            raise RuntimeError("runtime image guard must retain rollback images")
    image_guard_module = _load_image_guard()
    old_id = "sha256:" + "1" * 64
    release_a = image_guard_module.protection_tag(old_id, "self-check-a")
    release_b = image_guard_module.protection_tag(old_id, "self-check-b")
    other_image = image_guard_module.protection_tag(
        "sha256:" + "2" * 64, "self-check-a"
    )
    if len({release_a, release_b, other_image}) != 3 or not release_a.startswith(
        "mouchen-release-guard:previous-self-check-a-"
    ):
        raise RuntimeError("runtime image protection tag is not release/content unique")
    combined = "\n".join((deploy, rollback, backup))
    for forbidden in ("down -v", "docker volume rm", "rm -rf", "rm -fr"):
        if forbidden in combined:
            raise RuntimeError(f"destructive release command is forbidden: {forbidden}")
    for script_text in (deploy, rollback, backup, capture, verify, invites):
        if "COMPOSE_PROJECT=mouchen" not in script_text or '-p "$COMPOSE_PROJECT"' not in script_text:
            raise RuntimeError("a release path does not pin the Compose project")

    guard = _load_guard()
    with tempfile.TemporaryDirectory(prefix="mouchen-release-guard-") as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        live = data / "mouchen.db"
        pre_release = root / "pre-release.db"
        verified_backup = root / "verified-backup.db"
        _create_database(live, "failed-release")
        _create_database(pre_release, "pre-release")
        guard.snapshot_database(pre_release, verified_backup)
        result = guard.restore_database(
            verified_backup,
            live,
            release_id="self-check",
            expected_parent=data,
        )
        displaced = Path(str(result["displaced_directory"])) / "mouchen.db"
        if _marker(live) != "pre-release" or _marker(displaced) != "failed-release":
            raise RuntimeError("reversible restore self-check failed")

    print("release guard syntax and reversible self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
