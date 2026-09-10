#!/usr/bin/env python3
"""Fail-closed SQLite backup, migration-preflight, and restore primitives.

The shell release scripts mount this file into an isolated Python container.
It deliberately uses only the standard library so the same code can validate a
host backup and a Docker named volume.  It never prints database contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterable, Iterator


RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class GuardError(RuntimeError):
    """A release invariant failed."""


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _quote_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise GuardError("unsafe SQLite identifier")
    return f'"{identifier}"'


def _application_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _full_integrity_check(connection: sqlite3.Connection) -> None:
    results = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if results != ["ok"]:
        raise GuardError("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise GuardError("SQLite foreign_key_check failed")


def verify_database(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise GuardError("database must be a regular non-symlink file")
    candidate = path.resolve()
    if not candidate.is_file():
        raise GuardError("database must be a regular non-symlink file")
    if candidate.stat().st_size <= 0:
        raise GuardError("database is empty")
    with _read_only_connection(candidate) as connection:
        _full_integrity_check(connection)
        table_count = len(_application_tables(connection))
    with candidate.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "bytes": candidate.stat().st_size,
        "sha256": digest,
        "table_count": table_count,
    }


def verify_checksum(database: Path, checksum: Path) -> dict[str, object]:
    metadata = verify_database(database)
    if checksum.is_symlink() or not checksum.is_file():
        raise GuardError("checksum must be a regular non-symlink file")
    try:
        content = checksum.read_text(encoding="ascii")
    except UnicodeError as exc:
        raise GuardError("checksum is not ASCII") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n?", content)
    if match is None or match.group(2) != database.name:
        raise GuardError("checksum sidecar format or filename is invalid")
    if match.group(1) != metadata["sha256"]:
        raise GuardError("checksum digest mismatch")
    return metadata


def finalize_snapshot_artifacts(
    database: Path, *, exclusive_directory: bool = False
) -> dict[str, object]:
    """Remove only known SQLite sidecars after rejecting every unknown residue."""

    if database.is_symlink() or not database.is_file() or database.parent.is_symlink():
        raise GuardError("snapshot database path is unsafe")
    database = database.resolve()
    parent = database.parent
    known = {Path(f"{database}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES}
    for entry in parent.iterdir():
        if entry == database or entry in known:
            continue
        if exclusive_directory or entry.name.startswith(f"{database.name}-"):
            raise GuardError("snapshot directory contains an unknown artifact")
    for sidecar in known:
        if sidecar.exists() or sidecar.is_symlink():
            if sidecar.is_symlink() or not sidecar.is_file():
                raise GuardError("snapshot SQLite sidecar is unsafe")
    for sidecar in known:
        sidecar.unlink(missing_ok=True)
    if any(sidecar.exists() or sidecar.is_symlink() for sidecar in known):
        raise GuardError("snapshot SQLite sidecar cleanup failed")
    return {"removed_sidecars": True, "exclusive_directory": exclusive_directory}


def snapshot_database(source: Path, destination: Path) -> dict[str, object]:
    if source.is_symlink() or destination.is_symlink() or destination.parent.is_symlink():
        raise GuardError("snapshot paths must not use symbolic links")
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise GuardError("source database is unavailable")
    if destination.exists():
        raise GuardError("snapshot destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _read_only_connection(source) as source_database:
            _full_integrity_check(source_database)
            with closing(sqlite3.connect(destination)) as destination_database:
                source_database.backup(destination_database)
                # sqlite3_backup can copy the source database header's WAL
                # persistence bit. A later read-only integrity check would then
                # create -wal/-shm next to an otherwise self-contained backup.
                # Convert the completed private snapshot before publishing it.
                journal_mode_row = destination_database.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
                journal_mode = (
                    str(journal_mode_row[0]).casefold() if journal_mode_row else ""
                )
                if journal_mode != "delete":
                    raise GuardError("snapshot could not enter DELETE journal mode")
                _full_integrity_check(destination_database)
        # The connection is closed and DELETE mode is durable. SQLite may leave
        # empty WAL or a shared-memory index behind; remove only these exact new
        # derived files and reject non-regular replacements.
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{destination}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise GuardError("snapshot SQLite sidecar is unsafe")
                sidecar.unlink()
        destination.chmod(0o600)
        metadata = verify_database(destination)
        if any(
            Path(f"{destination}{suffix}").exists()
            or Path(f"{destination}{suffix}").is_symlink()
            for suffix in SQLITE_SIDECAR_SUFFIXES
        ):
            raise GuardError("snapshot verification left a SQLite sidecar")
        metadata["journal_mode"] = "delete"
        return metadata
    except BaseException:
        # This is a new incomplete artifact, never pre-existing user data.
        destination.unlink(missing_ok=True)
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{destination}{suffix}")
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
        raise


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()[0]
    )


def _table_layout(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    columns = tuple(str(row[1]) for row in rows)
    primary_key = tuple(
        str(row[1])
        for row in sorted(
            (row for row in rows if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    )
    if not columns:
        raise GuardError(f"table {table!r} has no inspectable columns")
    if not primary_key:
        # Rowid is not an application identity and can change during a table
        # rebuild. A persistent table without an explicit key cannot be proven
        # lossless, so release must stop for operator review.
        raise GuardError(f"table {table!r} has no explicit primary key")
    return columns, primary_key


def _canonical_sqlite_value(value: object) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bytes):
        return ("blob", value.hex())
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        return ("real", value.hex())
    return ("text", str(value))


def _row_fingerprint(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    order_columns: tuple[str, ...],
) -> str:
    selected = ",".join(_quote_identifier(column) for column in columns)
    ordered = ",".join(_quote_identifier(column) for column in order_columns)
    digest = hashlib.sha256()
    digest.update(
        json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    digest.update(b"\n")
    rows = connection.execute(
        f"SELECT {selected} FROM {_quote_identifier(table)} ORDER BY {ordered}"
    )
    for row in rows:
        encoded = tuple(_canonical_sqlite_value(value) for value in row)
        digest.update(
            json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _database_fingerprints(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    """Fingerprint every persistent application row, including provisional state."""

    result: dict[str, dict[str, object]] = {}
    for table in sorted(_application_tables(connection)):
        columns, primary_key = _table_layout(connection, table)
        result[table] = {
            "columns": columns,
            "primary_key": primary_key,
            "row_count": _row_count(connection, table),
            "primary_key_sha256": _row_fingerprint(
                connection, table, primary_key, primary_key
            ),
            "stable_rows_sha256": _row_fingerprint(
                connection, table, columns, primary_key
            ),
        }
    return result


def _verify_fingerprints_preserved(
    before: dict[str, dict[str, object]],
    connection: sqlite3.Connection,
) -> None:
    after_tables = _application_tables(connection)
    missing_tables = set(before) - after_tables
    if missing_tables:
        raise GuardError("migration removed application tables")
    for table, expected in before.items():
        before_columns = tuple(str(value) for value in expected["columns"])
        before_primary_key = tuple(str(value) for value in expected["primary_key"])
        after_columns, _after_primary_key = _table_layout(connection, table)
        if not set(before_columns).issubset(after_columns):
            raise GuardError(f"migration removed stable columns from {table}")
        if not set(before_primary_key).issubset(after_columns):
            raise GuardError(f"migration removed primary-key columns from {table}")
        actual_count = _row_count(connection, table)
        if actual_count != int(expected["row_count"]):
            raise GuardError(f"migration changed persistent row count in {table}")
        actual_primary_key = _row_fingerprint(
            connection, table, before_primary_key, before_primary_key
        )
        if actual_primary_key != expected["primary_key_sha256"]:
            raise GuardError(f"migration changed primary-key identity in {table}")
        actual_rows = _row_fingerprint(
            connection, table, before_columns, before_primary_key
        )
        if actual_rows != expected["stable_rows_sha256"]:
            raise GuardError(f"migration changed stable row content in {table}")


def _tenant_ids(connection: sqlite3.Connection) -> set[str]:
    tables = _application_tables(connection)
    values: set[str] = set()
    for table in sorted(tables):
        columns = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            )
        }
        column = "id" if table == "users" else "user_id"
        if column not in columns:
            continue
        rows = connection.execute(
            f"SELECT DISTINCT {_quote_identifier(column)} "
            f"FROM {_quote_identifier(table)} "
            f"WHERE {_quote_identifier(column)} IS NOT NULL "
            f"AND TRIM({_quote_identifier(column)}) <> ''"
        )
        values.update(str(row[0]) for row in rows)
    return values


def preflight_migration(source: Path, work_database: Path) -> dict[str, object]:
    """Run the candidate Repository migrations only on a disposable snapshot."""

    source_metadata = verify_database(source)
    if work_database.exists() or work_database.is_symlink():
        raise GuardError("preflight work database already exists")
    snapshot_database(source, work_database)
    with _read_only_connection(work_database) as before:
        before_fingerprints = _database_fingerprints(before)
        before_tenants = _tenant_ids(before)

    try:
        # Import only for migration preflight.  In release use this runs inside
        # the newly built image, so it tests the exact candidate application.
        from app.storage import Repository  # type: ignore[import-not-found]

        repository = Repository(work_database)
        try:
            if repository.auth_migration_blocked:
                reason = repository.auth_migration_reason or "authentication migration blocked"
                raise GuardError(reason)
        finally:
            repository.close()

        migrated_metadata = verify_database(work_database)
        with _read_only_connection(work_database) as after:
            _verify_fingerprints_preserved(before_fingerprints, after)
            after_tenants = _tenant_ids(after)
        if not before_tenants.issubset(after_tenants):
            raise GuardError("migration lost tenant ownership")
        return {
            "before_sha256": source_metadata["sha256"],
            "after_sha256": migrated_metadata["sha256"],
            "protected_rows": sum(
                int(signature["row_count"])
                for signature in before_fingerprints.values()
            ),
            "tenant_count": len(after_tenants),
            "table_count": migrated_metadata["table_count"],
        }
    finally:
        # The disposable migrated copy is not user data and must not accumulate.
        work_database.unlink(missing_ok=True)
        Path(f"{work_database}-wal").unlink(missing_ok=True)
        Path(f"{work_database}-shm").unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_database(
    backup: Path,
    target: Path,
    *,
    release_id: str,
    expected_parent: Path | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, object]:
    """Replace the stopped live DB while retaining displaced files in-volume."""

    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise GuardError("invalid release id")
    backup_metadata = verify_database(backup)
    if target.is_symlink() or target.parent.is_symlink():
        raise GuardError("restore target must not use symbolic links")
    target = target.resolve()
    target_parent = target.parent
    if expected_parent is not None and target_parent != expected_parent.resolve():
        raise GuardError("restore target is outside the expected data directory")
    target_parent.mkdir(parents=True, exist_ok=True)

    stage = target_parent / f".{target.name}.restore-{uuid.uuid4().hex}"
    displaced_root = target_parent / "rollback-displaced"
    displaced = displaced_root / release_id
    if displaced.exists() or displaced.is_symlink():
        raise GuardError("rollback displacement directory already exists")
    try:
        with backup.open("rb") as source, stage.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        if verify_database(stage)["sha256"] != backup_metadata["sha256"]:
            raise GuardError("restore staging digest mismatch")
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    if owner_uid is not None and owner_gid is not None:
        os.chown(stage, owner_uid, owner_gid)
    stage.chmod(0o600)

    moved: list[tuple[Path, Path]] = []
    stage_installed = False
    try:
        displaced.mkdir(parents=True, mode=0o700)
        displaced_root.chmod(0o700)
        displaced.chmod(0o700)
        for current in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
            if current.exists():
                if current.is_symlink() or not current.is_file():
                    raise GuardError("live SQLite path is not a regular file")
                retained = displaced / current.name
                os.replace(current, retained)
                moved.append((current, retained))
        os.replace(stage, target)
        stage_installed = True
        if owner_uid is not None and owner_gid is not None:
            os.chown(target, owner_uid, owner_gid)
            os.chown(displaced_root, owner_uid, owner_gid)
            os.chown(displaced, owner_uid, owner_gid)
            for _, retained in moved:
                os.chown(retained, owner_uid, owner_gid)
        target.chmod(0o600)
        _fsync_directory(target_parent)
        restored_metadata = verify_database(target)
        if restored_metadata["sha256"] != backup_metadata["sha256"]:
            raise GuardError("restored database digest mismatch")
        return {
            "sha256": restored_metadata["sha256"],
            "retained_file_count": len(moved),
            "displaced_directory": str(displaced),
        }
    except BaseException:
        target_was_displaced = any(current == target for current, _ in moved)
        if stage_installed and target.exists():
            if target_was_displaced and displaced.exists():
                failed_candidate = displaced / "failed-restored.db"
                if not failed_candidate.exists():
                    os.replace(target, failed_candidate)
            elif not target_was_displaced:
                target.unlink(missing_ok=True)
        for current, retained in reversed(moved):
            if retained.exists() and not current.exists():
                os.replace(retained, current)
        stage.unlink(missing_ok=True)
        raise


def _json_result(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--checksum", type=Path)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--destination", type=Path, required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--database", type=Path, required=True)
    preflight.add_argument("--work-database", type=Path, required=True)

    finalize = subparsers.add_parser("finalize-artifacts")
    finalize.add_argument("--database", type=Path, required=True)
    finalize.add_argument("--exclusive-directory", action="store_true")

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--release-id", required=True)
    restore.add_argument("--expected-parent", type=Path)
    restore.add_argument("--owner-uid", type=int)
    restore.add_argument("--owner-gid", type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "verify":
            result = (
                verify_checksum(arguments.database, arguments.checksum)
                if arguments.checksum is not None
                else verify_database(arguments.database)
            )
        elif arguments.command == "snapshot":
            result = snapshot_database(arguments.source, arguments.destination)
        elif arguments.command == "preflight":
            result = preflight_migration(arguments.database, arguments.work_database)
        elif arguments.command == "finalize-artifacts":
            result = finalize_snapshot_artifacts(
                arguments.database,
                exclusive_directory=arguments.exclusive_directory,
            )
        else:
            if (arguments.owner_uid is None) != (arguments.owner_gid is None):
                raise GuardError("owner uid and gid must be provided together")
            result = restore_database(
                arguments.backup,
                arguments.target,
                release_id=arguments.release_id,
                expected_parent=arguments.expected_parent,
                owner_uid=arguments.owner_uid,
                owner_gid=arguments.owner_gid,
            )
        _json_result(result)
        return 0
    except (GuardError, OSError, sqlite3.Error) as exc:
        print(f"release guard failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
