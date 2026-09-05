from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
GUARD_PATH = REPOSITORY_ROOT / "deploy" / "scripts" / "sqlite_release_guard.py"
SPEC = importlib.util.spec_from_file_location("sqlite_release_guard", GUARD_PATH)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def _database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
        connection.execute("INSERT INTO records(value) VALUES(?)", (value,))
        connection.commit()


def _value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute("SELECT value FROM records").fetchone()[0])


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))


def test_snapshot_is_independent_and_verified(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    snapshot = tmp_path / "backup.db"
    _database(source, "before")

    metadata = GUARD.snapshot_database(source, snapshot)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE records SET value='after'")
        connection.commit()

    assert _value(snapshot) == "before"
    assert metadata["sha256"] == GUARD.verify_database(snapshot)["sha256"]
    if os.name != "nt":
        assert snapshot.stat().st_mode & 0o777 == 0o600


def test_wal_snapshot_is_published_as_one_delete_mode_file(tmp_path: Path) -> None:
    source = tmp_path / "live-wal.db"
    snapshot = tmp_path / "backup.db.partial"
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records(value) VALUES('from wal source')")
        connection.commit()

    metadata = GUARD.snapshot_database(source, snapshot)

    assert metadata["journal_mode"] == "delete"
    with closing(sqlite3.connect(snapshot)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "from wal source"
    assert not any(path.exists() or path.is_symlink() for path in _sidecars(snapshot))
    GUARD.verify_database(snapshot)
    assert not any(path.exists() or path.is_symlink() for path in _sidecars(snapshot))


def test_artifact_finalizer_rejects_unknown_before_cleaning_known_sidecars(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    database = stage / "backup.db.partial"
    _database(database, "safe")
    wal, shm = _sidecars(database)[1:]
    wal.write_bytes(b"")
    shm.write_bytes(b"shared-memory-index")
    unknown = stage / "unexpected.bin"
    unknown.write_bytes(b"do not delete me")

    with pytest.raises(GUARD.GuardError, match="unknown artifact"):
        GUARD.finalize_snapshot_artifacts(database, exclusive_directory=True)

    assert database.exists() and wal.exists() and shm.exists() and unknown.exists()
    unknown.unlink()
    GUARD.finalize_snapshot_artifacts(database, exclusive_directory=True)
    assert database.exists()
    assert not any(path.exists() or path.is_symlink() for path in _sidecars(database))


def test_verify_rejects_corrupt_database(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a SQLite database")
    with pytest.raises((GUARD.GuardError, sqlite3.DatabaseError)):
        GUARD.verify_database(corrupt)


def test_checksum_sidecar_is_bound_to_exact_backup_name(tmp_path: Path) -> None:
    database = tmp_path / "mouchen-test.db"
    checksum = tmp_path / "mouchen-test.db.sha256"
    _database(database, "safe")
    digest = GUARD.verify_database(database)["sha256"]
    checksum.write_text(f"{digest}  {database.name}\n", encoding="ascii")

    assert GUARD.verify_checksum(database, checksum)["sha256"] == digest
    checksum.write_text(f"{digest}  ../{database.name}\n", encoding="ascii")
    with pytest.raises(GUARD.GuardError, match="format or filename"):
        GUARD.verify_checksum(database, checksum)


def test_candidate_migration_preserves_business_rows(tmp_path: Path, monkeypatch) -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        from app.storage import Repository

        database = tmp_path / "before.db"
        repository = Repository(database)
        repository._connection.execute(
            """INSERT INTO users(
                 id,username_normalized,username_display,password_hash,is_active,created_at
               ) VALUES('owner','owner','owner',NULL,1,'2026-08-12T00:00:00+00:00')"""
        )
        repository._connection.execute(
            """INSERT INTO goals(
                 id,user_id,domain,version,quote,payload_json,created_at,reaffirmed_at
               ) VALUES(
                 'g1','owner','work',1,'keep this goal','{}',
                 '2026-08-12T00:00:00+00:00','2026-08-12T00:00:00+00:00'
               )"""
        )
        event_payload = '{"facts":{"text":"preserve quota accounting"}}'
        repository._connection.execute(
            """INSERT INTO events(
                 id,user_id,source,type,occurred_at,evidence_ref,payload_json,created_at
               ) VALUES(
                 'e1','owner','release.guard','ui.visible_text',
                 '2026-08-12T00:00:00+00:00','guard:e1',?,
                 '2026-08-12T00:00:00+00:00'
               )""",
            (event_payload,),
        )
        repository._connection.execute(
            """INSERT INTO tenant_storage_usage(user_id,event_bytes,updated_at)
               VALUES('owner',?,'2026-08-12T00:00:00+00:00')""",
            (len(event_payload.encode("utf-8")),),
        )
        repository._connection.commit()
        repository.close()
        work = tmp_path / "work.db"

        result = GUARD.preflight_migration(database, work)

        assert result["protected_rows"] >= 1
        assert result["tenant_count"] == 1
        assert not work.exists()
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
            assert connection.execute(
                """SELECT event_bytes,updated_at FROM tenant_storage_usage
                WHERE user_id='owner'"""
            ).fetchone() == (
                len(event_payload.encode("utf-8")),
                "2026-08-12T00:00:00+00:00",
            )
    finally:
        sys.path.remove(str(BACKEND_ROOT))


def test_candidate_migration_fails_closed_for_unmapped_legacy_tenant(
    tmp_path: Path, monkeypatch
) -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        from app.storage import Repository

        monkeypatch.delenv("MOUCHEN_LEGACY_USERNAMES_JSON", raising=False)
        database = tmp_path / "legacy.db"
        repository = Repository(database)
        repository._connection.execute(
            "INSERT INTO users(id,created_at) VALUES('unmapped','2026-08-12T00:00:00+00:00')"
        )
        repository._connection.commit()
        repository.close()

        with pytest.raises(GUARD.GuardError, match="explicit username map"):
            GUARD.preflight_migration(database, tmp_path / "work.db")
    finally:
        sys.path.remove(str(BACKEND_ROOT))


def test_candidate_migration_rejects_same_count_content_corruption(
    tmp_path: Path, monkeypatch
) -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        import app.storage as storage

        database = tmp_path / "content.db"
        repository = storage.Repository(database)
        repository._connection.execute(
            "INSERT INTO users(id,created_at) VALUES('owner','2026-08-12T00:00:00+00:00')"
        )
        repository._connection.execute(
            """INSERT INTO goals(
                 id,user_id,domain,version,quote,payload_json,created_at,reaffirmed_at
               ) VALUES(
                 'g1','owner','work',1,'original promise','{}',
                 '2026-08-12T00:00:00+00:00','2026-08-12T00:00:00+00:00'
               )"""
        )
        repository._connection.commit()
        repository.close()

        class CorruptingRepository:
            auth_migration_blocked = False
            auth_migration_reason = None

            def __init__(self, path):
                self.connection = sqlite3.connect(path)
                self.connection.execute(
                    "UPDATE goals SET quote='silently replaced' WHERE id='g1'"
                )
                self.connection.commit()

            def close(self):
                self.connection.close()

        monkeypatch.setattr(storage, "Repository", CorruptingRepository)
        with pytest.raises(GUARD.GuardError, match="stable row content"):
            GUARD.preflight_migration(database, tmp_path / "work.db")
    finally:
        sys.path.remove(str(BACKEND_ROOT))


def test_candidate_migration_protects_previously_unlisted_tables(
    tmp_path: Path, monkeypatch
) -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        import app.storage as storage

        database = tmp_path / "extra-table.db"
        repository = storage.Repository(database)
        repository.close()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE future_business_data(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO future_business_data(id,value) VALUES('keep','valuable')"
            )
            connection.commit()

        class DeletingRepository:
            auth_migration_blocked = False
            auth_migration_reason = None

            def __init__(self, path):
                self.connection = sqlite3.connect(path)
                self.connection.execute("DELETE FROM future_business_data")
                self.connection.commit()

            def close(self):
                self.connection.close()

        monkeypatch.setattr(storage, "Repository", DeletingRepository)
        with pytest.raises(GUARD.GuardError, match="persistent row count"):
            GUARD.preflight_migration(database, tmp_path / "work.db")
    finally:
        sys.path.remove(str(BACKEND_ROOT))


def test_candidate_migration_rejects_preexisting_table_without_primary_key(
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    try:
        from app.storage import Repository

        database = tmp_path / "unkeyed.db"
        repository = Repository(database)
        repository.close()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE unsafe_legacy(value TEXT NOT NULL)")
            connection.execute("INSERT INTO unsafe_legacy(value) VALUES('cannot identify')")
            connection.commit()

        with pytest.raises(GUARD.GuardError, match="no explicit primary key"):
            GUARD.preflight_migration(database, tmp_path / "work.db")
    finally:
        sys.path.remove(str(BACKEND_ROOT))


def test_restore_keeps_displaced_live_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live = data / "mouchen.db"
    backup = tmp_path / "backup.db"
    _database(live, "failed-release")
    _database(backup, "pre-release")

    result = GUARD.restore_database(
        backup,
        live,
        release_id="release-1",
        expected_parent=data,
    )

    displaced = Path(str(result["displaced_directory"])) / "mouchen.db"
    assert _value(live) == "pre-release"
    assert _value(displaced) == "failed-release"
    assert result["retained_file_count"] == 1


def test_restore_refuses_reused_release_without_touching_live(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live = data / "mouchen.db"
    backup = tmp_path / "backup.db"
    _database(live, "live")
    _database(backup, "backup")
    (data / "rollback-displaced" / "release-1").mkdir(parents=True)

    with pytest.raises(GUARD.GuardError, match="already exists"):
        GUARD.restore_database(backup, live, release_id="release-1")

    assert _value(live) == "live"


def test_restore_move_failure_never_unlinks_live_database(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    live = data / "mouchen.db"
    backup = tmp_path / "backup.db"
    _database(live, "live")
    _database(backup, "backup")
    original_replace = GUARD.os.replace

    def fail_live_move(source, destination):
        if Path(source) == live:
            raise PermissionError("simulated live move failure")
        return original_replace(source, destination)

    monkeypatch.setattr(GUARD.os, "replace", fail_live_move)
    with pytest.raises(PermissionError, match="simulated"):
        GUARD.restore_database(backup, live, release_id="release-2")

    assert live.exists()
    assert _value(live) == "live"


def test_release_shells_keep_all_database_safety_gates() -> None:
    scripts = REPOSITORY_ROOT / "deploy" / "scripts"
    deploy = (scripts / "deploy.sh").read_text(encoding="utf-8")
    rollback = (scripts / "rollback.sh").read_text(encoding="utf-8")
    backup = (scripts / "backup.sh").read_text(encoding="utf-8")
    preflight = (scripts / "preflight-upgrade.sh").read_text(encoding="utf-8")
    capture = (scripts / "capture-compose-contract.sh").read_text(encoding="utf-8")
    verify = (scripts / "verify.sh").read_text(encoding="utf-8")
    invites = (scripts / "invites.sh").read_text(encoding="utf-8")
    image_guard = (scripts / "runtime_image_guard.py").read_text(encoding="utf-8")
    commercial_guard = (scripts / "validate-commercial-config.py").read_text(
        encoding="utf-8"
    )

    assert 'backup.sh" --print-path' in deploy
    assert 'MOUCHEN_BACKUP_HELPER_IMAGE="$previous_image_protection"' in deploy
    assert deploy.index('"$IMAGE_GUARD" protect') < deploy.index("compose build backend")
    assert deploy.index("compose build backend") < deploy.index('"$IMAGE_GUARD" verify')
    assert "previous_image_protection" in deploy + rollback
    assert '"$IMAGE_GUARD" verify --probe' in rollback
    assert "format=3" in deploy
    assert "docker image tag" not in deploy
    assert '"docker", "image", "tag"' in image_guard
    assert 'preflight-upgrade.sh"' in deploy
    assert 'rollback.sh" "$state_file"' in deploy
    assert "--volume \"$volume_name:/source:ro\"" in backup
    assert "--volume \"$stage_dir:/host-backups:rw\"" in backup
    assert "--volume \"$BACKUP_DIR:/host-backups:rw\"" not in backup
    assert backup.count("--user 10001:10001") == 2
    assert 'install -d -o 10001 -g 10001 -m 0700 "$stage_dir"' in backup
    assert 'chown 0:0 "$host_partial"' in backup
    assert "--volume \"$volume_name:/data:rw\"" in rollback
    assert "--expected-parent /data" in rollback
    assert 'install -d -o 10001 -g 10001 -m 0700 "$restore_stage"' in rollback
    assert 'install -o 10001 -g 10001 -m 0400 "$backup" "$restore_input"' in rollback
    assert '--user 10001:10001' in rollback
    assert '--volume "$restore_input:/restore/mouchen.db:ro"' in rollback
    assert '--volume "$backup:/restore/mouchen.db:ro"' not in rollback
    assert '--user 0:0' not in rollback
    assert 'finalize-artifacts --database "$stage_partial"' in backup
    assert 'finalize-artifacts --database "$host_partial"' in backup
    assert "unknown SQLite staging artifact" in backup
    assert 'COMPOSE_PROJECT=mouchen' in deploy + rollback + backup + capture
    assert '-p "$COMPOSE_PROJECT"' in deploy
    assert '-p "$COMPOSE_PROJECT"' in rollback
    assert '-p "$COMPOSE_PROJECT"' in backup
    assert 'com.docker.compose.project="$COMPOSE_PROJECT"' in backup
    assert "active-compose.state" in deploy + rollback + capture
    assert "previous_compose_name" in deploy + rollback
    assert 'MOUCHEN_COMPOSE_FILE="$previous_contract"' in rollback
    assert "disabled legacy owner token still grants API access" in verify
    assert "validate-commercial-config.py" in deploy + verify
    assert "exact host prefixes" in commercial_guard
    assert deploy.index("docker network create") < deploy.index(
        'compose config --format json | python3 "$COMMERCIAL_CONFIG_GUARD"'
    )
    assert "com.docker.compose.project=$COMPOSE_PROJECT" in deploy
    assert "--environment-list --gateway" in verify
    assert "rollback would re-enable the retired legacy owner token" in rollback
    assert "active-compose.state" in rollback
    assert 'COMPOSE_PROJECT=mouchen' in invites
    assert '-p "$COMPOSE_PROJECT"' in invites
    assert "down -v" not in deploy + rollback + backup
    assert "docker volume rm" not in deploy + rollback + backup
    assert "--memory 2g" in preflight
    assert "size=64m,uid=10001,gid=10001,mode=1700" in preflight
    assert "size=1g,uid=10001,gid=10001,mode=0700" in preflight
    assert "size=512m,uid=10001,gid=10001,mode=0700" not in preflight
    assert '"docker", "image", "rm"' not in image_guard
    assert '"docker", "image", "prune"' not in image_guard


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires a root Linux permission model",
)
def test_restore_staging_matches_root600_and_uid10001_volume_permissions(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    source = backup_root / "source.db"
    source.write_bytes(b"verified backup")
    source.chmod(0o600)

    restore_stage = backup_root / "restore-stage"
    restore_stage.mkdir(mode=0o700)
    os.chown(restore_stage, 10001, 10001)
    staged = restore_stage / "mouchen.db"
    shutil.copyfile(source, staged)
    os.chown(staged, 10001, 10001)
    staged.chmod(0o400)

    # A direct bind mount exposes the staged inode without requiring traversal
    # of its host parent. A hard link in this test models that inode exposure.
    container_view = tmp_path / "container-view"
    container_view.mkdir(mode=0o755)
    mounted_input = container_view / "mouchen.db"
    os.link(staged, mounted_input)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    os.chown(data, 10001, 10001)

    script = """
from pathlib import Path
import sys
source, mounted, data = map(Path, sys.argv[1:])
try:
    source.read_bytes()
except PermissionError:
    pass
else:
    raise SystemExit('uid10001 unexpectedly read the root600 host backup')
assert mounted.read_bytes() == b'verified backup'
(data / 'write-test').write_bytes(mounted.read_bytes())
"""

    def drop_to_application_user() -> None:
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)

    subprocess.run(
        [sys.executable, "-c", script, str(source), str(mounted_input), str(data)],
        check=True,
        preexec_fn=drop_to_application_user,
    )


def test_compose_project_name_environment_cannot_redirect_the_release() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    environment = os.environ.copy()
    environment["COMPOSE_PROJECT_NAME"] = "attacker-selected-project"
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(REPOSITORY_ROOT / "deploy" / "env.example"),
            "-p",
            "mouchen",
            "-f",
            str(REPOSITORY_ROOT / "deploy" / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    import json

    assert json.loads(result.stdout)["name"] == "mouchen"


def test_rendered_compose_contract_preserves_hashes_and_commercial_default(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    compose = REPOSITORY_ROOT / "deploy" / "compose.yaml"
    env_file = REPOSITORY_ROOT / "deploy" / "env.example"
    source_prefix = [
        docker,
        "compose",
        "--env-file",
        str(env_file),
        "-p",
        "mouchen",
        "-f",
        str(compose),
    ]
    rendered = tmp_path / "rendered.yaml"
    result = subprocess.run(
        [*source_prefix, "config"], check=True, capture_output=True, text=True
    )
    rendered.write_text(result.stdout, encoding="utf-8")
    rendered_prefix = [
        docker,
        "compose",
        "-p",
        "mouchen",
        "-f",
        str(rendered),
    ]

    for service in ("backend", "stt"):
        source_hash = subprocess.run(
            [*source_prefix, "config", "--hash", service],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        rendered_hash = subprocess.run(
            [*rendered_prefix, "config", "--hash", service],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert rendered_hash == source_hash

    configuration = subprocess.run(
        [*rendered_prefix, "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    backend_environment = json.loads(configuration.stdout)["services"]["backend"][
        "environment"
    ]
    assert backend_environment["MOUCHEN_LEGACY_AUTH_ENABLED"] == "false"

    claim_environment = os.environ.copy()
    claim_environment["MOUCHEN_LEGACY_AUTH_ENABLED"] = "true"
    claim_configuration = subprocess.run(
        [*source_prefix, "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        env=claim_environment,
    )
    claim_backend_environment = json.loads(claim_configuration.stdout)["services"][
        "backend"
    ]["environment"]
    assert claim_backend_environment["MOUCHEN_LEGACY_AUTH_ENABLED"] == "true"
