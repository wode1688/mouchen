#!/usr/bin/env python3
"""Consolidate two private-alpha user partitions into one owner partition.

The command is intentionally dry-run by default.  ``--apply`` requires a new
backup path and operates inside one SQLite transaction.  It is designed for an
offline database copy; callers should stop the backend before replacing a live
database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SPECIAL_TABLES = {
    "advice_preferences",
    "charters",
    "devices",
    "speaking_freezes",
    "topic_suppressions",
    "trust_accounts",
}
JSON_TABLES = {"advice", "events", "goals"}
NONTERMINAL_ADVICE = {"provisional", "active", "adopted"}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _set_advice_status(
    connection: sqlite3.Connection,
    user_id: str,
    advice_id: str,
    status: str,
) -> None:
    raw = connection.execute(
        "SELECT payload_json FROM advice WHERE user_id=? AND id=?", (user_id, advice_id)
    ).fetchone()
    if raw is None:
        return
    payload = json.loads(raw[0])
    payload["status"] = status
    connection.execute(
        "UPDATE advice SET status=?,payload_json=? WHERE user_id=? AND id=?",
        (
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            user_id,
            advice_id,
        ),
    )
    if _table_exists(connection, "advice_attention"):
        current = datetime.now(timezone.utc).isoformat()
        claim = connection.execute(
            """SELECT claim_token FROM advice_attention
            WHERE user_id=? AND advice_id=?""", (user_id, advice_id)
        ).fetchone()
        if claim is not None and claim[0] and _table_exists(
            connection, "advice_attention_attempts"
        ):
            connection.execute(
                """UPDATE advice_attention_attempts
                SET state='cancelled',completed_at=?
                WHERE claim_token=? AND state='claimed'""",
                (current, claim[0]),
            )
        connection.execute(
            """UPDATE advice_attention SET state='resolved',claim_token=NULL,
              claim_device_id=NULL,claim_expires_at=NULL,next_eligible_at=NULL,
              auto_close_at=NULL,resolved_at=?,resolved_reason='owner_merge_duplicate',
              updated_at=? WHERE user_id=? AND advice_id=?""",
            (current, current, user_id, advice_id),
        )


def _resolve_cross_owner_advice_collisions(
    connection: sqlite3.Connection,
    source: str,
    target: str,
) -> None:
    """Withdraw the losing live row before owner-scoped unique keys converge."""

    columns = _columns(connection, "advice")
    if not {"status", "created_at", "payload_json"}.issubset(columns):
        return
    selectors = []
    if "topic_key" in columns:
        selectors.append("(source.topic_key IS NOT NULL AND target.topic_key=source.topic_key)")
    if "evidence_ref" in columns:
        selectors.append(
            "(source.evidence_ref IS NOT NULL AND target.evidence_ref=source.evidence_ref)"
        )
    if not selectors:
        return
    rows = connection.execute(
        f"""
        SELECT source.id AS source_id,source.status AS source_status,
               source.created_at AS source_created,
               target.id AS target_id,target.status AS target_status,
               target.created_at AS target_created
        FROM advice source JOIN advice target ON ({' OR '.join(selectors)})
        WHERE source.user_id=? AND target.user_id=?
          AND source.status IN ('provisional','active','adopted')
          AND target.status IN ('provisional','active','adopted')
        ORDER BY source.id,target.id
        """,
        (source, target),
    ).fetchall()
    for row in rows:
        source_row = connection.execute(
            """SELECT status,created_at FROM advice
            WHERE user_id=? AND id=?""", (source, row[0])
        ).fetchone()
        target_row = connection.execute(
            """SELECT status,created_at FROM advice
            WHERE user_id=? AND id=?""", (target, row[3])
        ).fetchone()
        if source_row is None or target_row is None:
            continue
        if source_row[0] not in NONTERMINAL_ADVICE:
            continue
        if target_row[0] not in NONTERMINAL_ADVICE:
            continue
        source_rank = (
            0 if source_row[0] == "adopted" else 1,
            source_row[1],
            row[0],
        )
        target_rank = (
            0 if target_row[0] == "adopted" else 1,
            target_row[1],
            row[3],
        )
        loser_is_target = source_rank < target_rank
        loser = row[3] if loser_is_target else row[0]
        _set_advice_status(
            connection, target if loser_is_target else source, loser, "withdrawn"
        )


def _drop_duplicate_source_events(
    connection: sqlite3.Connection,
    source: str,
    target: str,
) -> None:
    if "evidence_ref" not in _columns(connection, "events"):
        return
    duplicate_ids = [
        row[0]
        for row in connection.execute(
            """
            SELECT source.id FROM events source JOIN events target
              ON target.evidence_ref=source.evidence_ref
            WHERE source.user_id=? AND target.user_id=?
              AND source.evidence_ref IS NOT NULL
            """,
            (source, target),
        )
    ]
    if not duplicate_ids:
        return
    if _table_exists(connection, "analysis_jobs"):
        connection.executemany(
            "DELETE FROM analysis_jobs WHERE user_id=? AND event_id=?",
            ((source, value) for value in duplicate_ids),
        )
    connection.executemany(
        "DELETE FROM events WHERE user_id=? AND id=?",
        ((source, value) for value in duplicate_ids),
    )


def _merge_devices(connection: sqlite3.Connection, source: str, target: str) -> None:
    if not _table_exists(connection, "devices"):
        return
    connection.execute(
        """
        INSERT INTO devices(user_id,device_id,platform,app_version,created_at,last_seen_at)
        SELECT ?,device_id,platform,app_version,created_at,last_seen_at
        FROM devices WHERE user_id=?
        ON CONFLICT(user_id,device_id) DO UPDATE SET
          platform=COALESCE(excluded.platform,devices.platform),
          app_version=COALESCE(excluded.app_version,devices.app_version),
          created_at=MIN(devices.created_at,excluded.created_at),
          last_seen_at=MAX(devices.last_seen_at,excluded.last_seen_at)
        """,
        (target, source),
    )
    connection.execute("DELETE FROM devices WHERE user_id=?", (source,))


def _merge_advice_preferences(
    connection: sqlite3.Connection,
    source: str,
    target: str,
) -> None:
    if not _table_exists(connection, "advice_preferences"):
        return
    connection.execute(
        """
        INSERT INTO advice_preferences(
          user_id,scope_key,goal_id,direction,direction_mode,frequency_mode,
          event_types_json,revision,updated_at
        )
        SELECT ?,scope_key,goal_id,direction,direction_mode,frequency_mode,
               event_types_json,revision,updated_at
        FROM advice_preferences WHERE user_id=?
        ON CONFLICT(user_id,scope_key) DO UPDATE SET
          goal_id=CASE WHEN excluded.updated_at>=advice_preferences.updated_at
                       THEN excluded.goal_id ELSE advice_preferences.goal_id END,
          direction=CASE WHEN excluded.updated_at>=advice_preferences.updated_at
                         THEN excluded.direction ELSE advice_preferences.direction END,
          direction_mode=CASE WHEN excluded.updated_at>=advice_preferences.updated_at
                              THEN excluded.direction_mode ELSE advice_preferences.direction_mode END,
          frequency_mode=CASE WHEN excluded.updated_at>=advice_preferences.updated_at
                              THEN excluded.frequency_mode ELSE advice_preferences.frequency_mode END,
          event_types_json=CASE WHEN excluded.updated_at>=advice_preferences.updated_at
                                THEN excluded.event_types_json ELSE advice_preferences.event_types_json END,
          revision=MAX(advice_preferences.revision,excluded.revision),
          updated_at=MAX(advice_preferences.updated_at,excluded.updated_at)
        """,
        (target, source),
    )
    connection.execute("DELETE FROM advice_preferences WHERE user_id=?", (source,))


def _drop_duplicate_source_feedback(
    connection: sqlite3.Connection,
    source: str,
    target: str,
) -> None:
    if not _table_exists(connection, "feedback"):
        return
    columns = _columns(connection, "feedback")
    if "feedback_id" in columns:
        conflicts = connection.execute(
            """
            SELECT source.id,source.advice_id,source.kind,source.semantic_key,
                   target.advice_id,target.kind,target.semantic_key
            FROM feedback source JOIN feedback target
              ON target.feedback_id=source.feedback_id
            WHERE source.user_id=? AND target.user_id=?
              AND source.feedback_id IS NOT NULL
            """,
            (source, target),
        ).fetchall()
        for row in conflicts:
            if tuple(row[1:4]) != tuple(row[4:7]):
                raise RuntimeError("feedback_id collision has different content")
            connection.execute("DELETE FROM feedback WHERE id=?", (row[0],))
    if "semantic_key" in columns:
        connection.execute(
            """
            DELETE FROM feedback WHERE id IN (
              SELECT source.id FROM feedback source JOIN feedback target
                ON target.advice_id=source.advice_id
               AND target.semantic_key=source.semantic_key
              WHERE source.user_id=? AND target.user_id=?
                AND source.semantic_key IS NOT NULL
            )
            """,
            (source, target),
        )


def _tables_with_user_id(connection: sqlite3.Connection) -> list[str]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return [
        name
        for name in names
        if any(row[1] == "user_id" for row in connection.execute(f'PRAGMA table_info("{name}")'))
    ]


def _rewrite_payload_owners(
    connection: sqlite3.Connection,
    table: str,
    source_user: str,
    target_user: str,
) -> int:
    if table not in JSON_TABLES:
        return 0
    changed = 0
    rows = connection.execute(
        f'SELECT id, payload_json FROM "{table}" WHERE user_id=?',
        (source_user,),
    ).fetchall()
    for record_id, raw_payload in rows:
        payload = json.loads(raw_payload)
        if payload.get("user_id") != target_user:
            payload["user_id"] = target_user
            connection.execute(
                f'UPDATE "{table}" SET payload_json=? WHERE user_id=? AND id=?',
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    source_user,
                    record_id,
                ),
            )
            changed += 1
    return changed


def _merge_charters(connection: sqlite3.Connection, source: str, target: str) -> None:
    connection.execute(
        """
        INSERT INTO charters(user_id,domain,max_level,redline_authorized,updated_at)
        SELECT ?,domain,max_level,redline_authorized,updated_at
        FROM charters WHERE user_id=?
        ON CONFLICT(user_id,domain) DO UPDATE SET
          max_level=MAX(charters.max_level,excluded.max_level),
          redline_authorized=MAX(charters.redline_authorized,excluded.redline_authorized),
          updated_at=MAX(charters.updated_at,excluded.updated_at)
        """,
        (target, source),
    )
    connection.execute("DELETE FROM charters WHERE user_id=?", (source,))


def _merge_trust_accounts(connection: sqlite3.Connection, source: str, target: str) -> None:
    connection.execute(
        """
        INSERT INTO trust_accounts(
          user_id,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
          catastrophic_errors,updated_at
        )
        SELECT ?,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
               catastrophic_errors,updated_at
        FROM trust_accounts WHERE user_id=?
        ON CONFLICT(user_id,domain,level) DO UPDATE SET
          judged=trust_accounts.judged+excluded.judged,
          correct=trust_accounts.correct+excluded.correct,
          brier_sum=trust_accounts.brier_sum+excluded.brier_sum,
          utility_sum=trust_accounts.utility_sum+excluded.utility_sum,
          timing_sum=trust_accounts.timing_sum+excluded.timing_sum,
          catastrophic_errors=trust_accounts.catastrophic_errors+excluded.catastrophic_errors,
          updated_at=MAX(trust_accounts.updated_at,excluded.updated_at)
        """,
        (target, source),
    )
    connection.execute("DELETE FROM trust_accounts WHERE user_id=?", (source,))


def _merge_speaking_freezes(connection: sqlite3.Connection, source: str, target: str) -> None:
    connection.execute(
        """
        INSERT INTO speaking_freezes(
          user_id,level,error_count,allowed_errors,window_started_at,frozen_at,
          expires_at,reason
        )
        SELECT ?,level,error_count,allowed_errors,window_started_at,frozen_at,
               expires_at,reason
        FROM speaking_freezes WHERE user_id=?
        ON CONFLICT(user_id,level) DO UPDATE SET
          error_count=MAX(speaking_freezes.error_count,excluded.error_count),
          allowed_errors=MIN(speaking_freezes.allowed_errors,excluded.allowed_errors),
          window_started_at=MIN(speaking_freezes.window_started_at,excluded.window_started_at),
          frozen_at=MIN(speaking_freezes.frozen_at,excluded.frozen_at),
          expires_at=MAX(speaking_freezes.expires_at,excluded.expires_at),
          reason=CASE WHEN excluded.expires_at>=speaking_freezes.expires_at
                      THEN excluded.reason ELSE speaking_freezes.reason END
        """,
        (target, source),
    )
    connection.execute("DELETE FROM speaking_freezes WHERE user_id=?", (source,))


def _merge_topic_suppressions(connection: sqlite3.Connection, source: str, target: str) -> None:
    connection.execute(
        """
        INSERT INTO topic_suppressions(
          user_id,dedupe_key,source_advice_id,reason,created_at,updated_at,expires_at
        )
        SELECT ?,dedupe_key,source_advice_id,reason,created_at,updated_at,expires_at
        FROM topic_suppressions WHERE user_id=?
        ON CONFLICT(user_id,dedupe_key) DO UPDATE SET
          source_advice_id=CASE WHEN excluded.expires_at>=topic_suppressions.expires_at
                                THEN excluded.source_advice_id
                                ELSE topic_suppressions.source_advice_id END,
          reason=CASE WHEN excluded.expires_at>=topic_suppressions.expires_at
                      THEN excluded.reason ELSE topic_suppressions.reason END,
          created_at=MIN(topic_suppressions.created_at,excluded.created_at),
          updated_at=MAX(topic_suppressions.updated_at,excluded.updated_at),
          expires_at=MAX(topic_suppressions.expires_at,excluded.expires_at)
        """,
        (target, source),
    )
    connection.execute("DELETE FROM topic_suppressions WHERE user_id=?", (source,))


def _assert_no_evidence_collisions(
    connection: sqlite3.Connection,
    table: str,
    source: str,
    target: str,
) -> None:
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if "evidence_ref" not in columns:
        return
    collisions = connection.execute(
        f"""
        SELECT COUNT(*) FROM "{table}" source
        JOIN "{table}" target ON target.evidence_ref=source.evidence_ref
        WHERE source.user_id=? AND target.user_id=? AND source.evidence_ref IS NOT NULL
        """,
        (source, target),
    ).fetchone()[0]
    if collisions:
        raise RuntimeError(f"{table} has {collisions} cross-owner evidence collisions")


def consolidate(
    connection: sqlite3.Connection,
    source_user: str,
    target_user: str,
    *,
    prune_other_users: bool,
) -> dict[str, Any]:
    if source_user == target_user:
        raise ValueError("source and target users must differ")
    tables = _tables_with_user_id(connection)
    before = {
        table: {
            "source": connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?', (source_user,)
            ).fetchone()[0],
            "target": connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?', (target_user,)
            ).fetchone()[0],
        }
        for table in tables
    }

    connection.execute(
        "INSERT OR IGNORE INTO users(id,created_at) "
        "SELECT ?,created_at FROM users WHERE id=?",
        (target_user, source_user),
    )
    if "events" in tables:
        _drop_duplicate_source_events(connection, source_user, target_user)
        _assert_no_evidence_collisions(connection, "events", source_user, target_user)
    if "advice" in tables:
        _resolve_cross_owner_advice_collisions(connection, source_user, target_user)
    if "feedback" in tables:
        _drop_duplicate_source_feedback(connection, source_user, target_user)
    for table in JSON_TABLES.intersection(tables):
        _rewrite_payload_owners(connection, table, source_user, target_user)

    if "advice_preferences" in tables:
        _merge_advice_preferences(connection, source_user, target_user)
    if "charters" in tables:
        _merge_charters(connection, source_user, target_user)
    if "devices" in tables:
        _merge_devices(connection, source_user, target_user)
    if "trust_accounts" in tables:
        _merge_trust_accounts(connection, source_user, target_user)
    if "speaking_freezes" in tables:
        _merge_speaking_freezes(connection, source_user, target_user)
    if "topic_suppressions" in tables:
        _merge_topic_suppressions(connection, source_user, target_user)

    for table in tables:
        if table in SPECIAL_TABLES:
            continue
        connection.execute(
            f'UPDATE "{table}" SET user_id=? WHERE user_id=?',
            (target_user, source_user),
        )

    connection.execute("DELETE FROM users WHERE id=?", (source_user,))
    if prune_other_users:
        for table in tables:
            connection.execute(f'DELETE FROM "{table}" WHERE user_id<>?', (target_user,))
        connection.execute("DELETE FROM users WHERE id<>?", (target_user,))

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"foreign key check failed with {len(foreign_key_errors)} rows")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"integrity check failed: {integrity}")

    after = {
        table: connection.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?', (target_user,)
        ).fetchone()[0]
        for table in tables
    }
    return {
        "source_user": source_user,
        "target_user": target_user,
        "pruned_other_users": prune_other_users,
        "before": before,
        "after_target": after,
        "integrity": integrity,
    }


def _backup_database(source: sqlite3.Connection, backup_path: Path) -> None:
    if backup_path.exists():
        raise FileExistsError(f"backup path already exists: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    finally:
        backup.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--target-user", required=True)
    parser.add_argument("--prune-other-users", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    arguments = parser.parse_args()

    database = arguments.database.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    if arguments.apply and arguments.backup is None:
        parser.error("--apply requires --backup")

    if arguments.apply:
        connection = sqlite3.connect(database, timeout=1)
        try:
            _backup_database(connection, arguments.backup.resolve())
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                report = consolidate(
                    connection,
                    arguments.source_user,
                    arguments.target_user,
                    prune_other_users=arguments.prune_other_users,
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        finally:
            connection.close()
    else:
        source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        simulation = sqlite3.connect(":memory:")
        try:
            source.backup(simulation)
            simulation.execute("PRAGMA foreign_keys=ON")
            simulation.execute("BEGIN IMMEDIATE")
            report = consolidate(
                simulation,
                arguments.source_user,
                arguments.target_user,
                prune_other_users=arguments.prune_other_users,
            )
            simulation.rollback()
        finally:
            simulation.close()
            source.close()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
