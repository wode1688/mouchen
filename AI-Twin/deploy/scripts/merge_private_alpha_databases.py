#!/usr/bin/env python3
"""Safely merge private-alpha user history from one SQLite DB into another.

The source is always opened read-only.  The target is simulated in memory by
default; ``--apply`` requires a fresh backup path and commits one immediate
transaction.  Runtime state (devices, attention claims, analysis jobs, model
usage, cloud audit rows and action drafts) is deliberately never replayed.

Every handled source row is recorded in ``data_import_ledger``.  Repeating the
same immutable snapshot and import label is therefore idempotent, including
aggregate trust rows.  A changed source row under an already-used label aborts
instead of silently double-counting it.  This is a one-time migration tool, not
an incremental synchronizer; merge a changed snapshot into a fresh pre-import
target copy rather than assigning a new label to overlapping aggregate data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid5


IMPORT_NAMESPACE = UUID("f51675e9-b0aa-4663-a005-d7cc7b967a83")
CORE_TABLES = ("goals", "events", "advice", "feedback")
HISTORY_TABLES = (
    "goals",
    "events",
    "advice",
    "feedback",
    "relevance_ledger",
    "outcomes",
    "charters",
    "trust_accounts",
    "advice_preferences",
    "topic_suppressions",
)
SKIPPED_TABLE_REASONS = {
    "advice_localizations": "derived display translations are rebuilt at destination",
    "devices": "device registration is destination-local",
    "advice_attention": "old notification state must never be replayed",
    "advice_attention_attempts": "old delivery attempts must never be replayed",
    "analysis_jobs": "old analysis work must not be re-executed",
    "analysis_model_usage": "runtime quota usage is destination-local",
    "analysis_model_reservations": "runtime quota reservations are destination-local",
    "global_model_usage": "direct model quota usage is destination-local",
    "direct_model_requests": "direct model rate-limit history is destination-local",
    "direct_model_leases": "direct model concurrency leases are destination-local",
    "stt_admission_requests": "speech-to-text rate-limit history is destination-local",
    "stt_admission_leases": "speech-to-text concurrency leases are destination-local",
    "account_export_requests": "account-export cooldown history is destination-local",
    "account_export_leases": "account-export concurrency leases are destination-local",
    "tenant_storage_usage": "derived tenant storage counters are destination-local",
    "cloud_slices": "cloud audit data is nonessential and may duplicate sensitive context",
    "action_drafts": "external-action state must not cross execution environments",
    "error_ledger": "stale enforcement rows are replaced by merged trust history",
    "speaking_freezes": "stale speaking freezes must not be reactivated",
    "data_import_ledger": "an upstream import ledger is not transitive history",
}
NONTERMINAL_ADVICE = frozenset({"provisional", "active", "adopted"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in sorted(rows, key=lambda item: item[5]) if row[5]]


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _load_payload(raw: Any, *, table: str, row_key: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{table} row {row_key} has invalid payload_json") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{table} row {row_key} payload_json is not an object")
    return payload


def _source_key(
    connection: sqlite3.Connection, table: str, row: Mapping[str, Any]
) -> str:
    keys = _primary_key_columns(connection, table)
    if not keys:
        raise RuntimeError(f"{table} has no primary key; refusing an unsafe import")
    # user_id is already a separate import-ledger dimension. Keeping it in a
    # post-migration composite PK would change idempotency keys for the same
    # source database merely because its schema was upgraded.
    if "user_id" in keys and len(keys) > 1:
        keys = [key for key in keys if key != "user_id"]
    return _canonical([row[key] for key in keys])


def _ensure_import_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS data_import_ledger(
          source_label TEXT NOT NULL,
          source_user_id TEXT NOT NULL,
          table_name TEXT NOT NULL,
          source_key TEXT NOT NULL,
          source_hash TEXT NOT NULL,
          target_key TEXT,
          action TEXT NOT NULL,
          detail_json TEXT NOT NULL DEFAULT '{}',
          imported_at TEXT NOT NULL,
          PRIMARY KEY(source_label,source_user_id,table_name,source_key)
        )
        """
    )


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
    *,
    omit: Iterable[str] = (),
) -> None:
    omitted = set(omit)
    target_columns = set(_columns(connection, table))
    selected = [key for key in values if key in target_columns and key not in omitted]
    if not selected:
        raise RuntimeError(f"no compatible columns for {table}")
    placeholders = ",".join("?" for _ in selected)
    column_sql = ",".join(f'"{key}"' for key in selected)
    connection.execute(
        f'INSERT INTO "{table}"({column_sql}) VALUES({placeholders})',
        tuple(values[key] for key in selected),
    )


def _semantic_payload(raw: Any, table: str) -> Any:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if not isinstance(payload, dict):
        return payload
    ignored = {"user_id", "created_at"}
    if table == "events":
        ignored.add("event_id")
    elif table == "goals":
        ignored.update({"id", "reaffirmed_at"})
    elif table == "advice":
        ignored.update({"id", "status", "snoozed_until"})
    return {key: value for key, value in payload.items() if key not in ignored}


def _semantic_digest(table: str, row: Mapping[str, Any]) -> str:
    ignored = {"id", "user_id", "created_at", "published_at"}
    if table == "advice":
        ignored.add("status")
    elif table == "goals":
        ignored.add("reaffirmed_at")
    material: dict[str, Any] = {}
    for key, value in row.items():
        if key in ignored:
            continue
        material[key] = _semantic_payload(value, table) if key == "payload_json" else value
    return _digest(material)


def _event_observation_digest(row: Mapping[str, Any]) -> str:
    """Compare the observation while ignoring ingestion/privacy metadata.

    Android may upload the same stable evidence reference through a local
    minimized-context path and later through the owner-full-context VPS path.
    The generated event IDs, ingestion timestamps and consent labels differ,
    but the observation is one event and must not be duplicated.
    """

    payload = _semantic_payload(row.get("payload_json"), "events")
    if isinstance(payload, dict):
        facts = payload.get("facts")
        if row.get("type") == "app.foreground_session" and isinstance(facts, dict):
            # Package label resolution varies by Android install context. The
            # package, timestamps and duration identify the observation; retain
            # the alternate label in the import ledger instead of duplicating it.
            facts = {key: value for key, value in facts.items() if key != "app_label"}
        payload = {
            key: (facts if key == "facts" else payload.get(key))
            for key in (
                "source",
                "type",
                "occurred_at",
                "facts",
                "entities",
                "confidence",
                "evidence_ref",
            )
        }
    return _digest(
        {
            "source": row.get("source"),
            "type": row.get("type"),
            "occurred_at": row.get("occurred_at"),
            "evidence_ref": row.get("evidence_ref"),
            "payload": payload,
        }
    )


def _deterministic_id(
    target: sqlite3.Connection,
    table: str,
    target_user: str,
    source_label: str,
    source_user: str,
    original_id: str,
    source_hash: str,
) -> str:
    for attempt in range(100):
        suffix = "" if attempt == 0 else f":{attempt}"
        candidate = str(
            uuid5(
                IMPORT_NAMESPACE,
                f"{source_label}:{source_user}:{table}:{original_id}:{source_hash}{suffix}",
            )
        )
        if target.execute(
            f'SELECT 1 FROM "{table}" WHERE user_id=? AND id=?',
            (target_user, candidate),
        ).fetchone() is None:
            return candidate
    raise RuntimeError(f"could not allocate a collision-safe id for {table}:{original_id}")


class DatabaseMerger:
    def __init__(
        self,
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        *,
        source_label: str,
        source_users: list[str],
        target_user: str,
    ) -> None:
        self.source = source
        self.target = target
        self.source_label = source_label.strip()
        self.source_users = list(dict.fromkeys(source_users))
        self.target_user = target_user
        self.now = _utc_now()
        self.maps: dict[tuple[str, str, str], str] = {}
        self.advice_overlap_users: set[str] = set()
        self.stats: dict[str, defaultdict[str, int]] = {
            table: defaultdict(int) for table in HISTORY_TABLES
        }
        self.details: list[dict[str, Any]] = []
        if not self.source_label:
            raise ValueError("source_label must not be blank")
        if not self.source_users:
            raise ValueError("at least one source_user is required")
        if not target_user:
            raise ValueError("target_user must not be blank")
        _ensure_import_ledger(self.target)

    def _prior(
        self,
        table: str,
        source_user: str,
        source_key: str,
        source_hash: str,
    ) -> sqlite3.Row | None:
        prior = self.target.execute(
            """SELECT source_hash,target_key,action,detail_json
            FROM data_import_ledger
            WHERE source_label=? AND source_user_id=? AND table_name=? AND source_key=?""",
            (self.source_label, source_user, table, source_key),
        ).fetchone()
        if prior is None:
            return None
        if prior["source_hash"] != source_hash:
            raise RuntimeError(
                f"source drift detected for {table} {source_key}; "
                "restore a pre-import target and merge the new immutable snapshot"
            )
        self.stats[table]["already_imported"] += 1
        return prior

    def _record(
        self,
        table: str,
        source_user: str,
        source_key: str,
        source_hash: str,
        *,
        target_key: str | None,
        action: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        detail_value = dict(detail or {})
        self.target.execute(
            """INSERT INTO data_import_ledger(
              source_label,source_user_id,table_name,source_key,source_hash,
              target_key,action,detail_json,imported_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                self.source_label,
                source_user,
                table,
                source_key,
                source_hash,
                target_key,
                action,
                _canonical(detail_value),
                self.now,
            ),
        )
        self.stats[table][action] += 1
        if detail_value:
            self.details.append(
                {
                    "table": table,
                    "source_user": source_user,
                    "source_key": source_key,
                    "action": action,
                    **detail_value,
                }
            )

    def _rows(self, table: str, source_user: str) -> list[dict[str, Any]]:
        if not _table_exists(self.source, table):
            return []
        rows = self.source.execute(
            f'SELECT * FROM "{table}" WHERE user_id=?', (source_user,)
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _begin_row(
        self, table: str, source_user: str, row: Mapping[str, Any]
    ) -> tuple[str, str, sqlite3.Row | None]:
        source_key = _source_key(self.source, table, row)
        source_hash = _digest(row)
        self.stats[table]["source_rows"] += 1
        return source_key, source_hash, self._prior(
            table, source_user, source_key, source_hash
        )

    def _restore_map_from_prior(
        self, table: str, source_user: str, original_id: str, prior: sqlite3.Row
    ) -> None:
        if prior["target_key"]:
            self.maps[(table, source_user, str(original_id))] = str(prior["target_key"])
        if table == "advice" and prior["action"] == "deduplicated":
            self.advice_overlap_users.add(source_user)

    def merge_goals(self) -> None:
        table = "goals"
        if not self._ready(table):
            return
        target_rows = [
            _row_dict(row)
            for row in self.target.execute(
                "SELECT * FROM goals WHERE user_id=?", (self.target_user,)
            )
        ]
        by_id = {str(row["id"]): row for row in target_rows}
        by_semantic = {_semantic_digest(table, row): row for row in target_rows}
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                original_id = str(source_row["id"])
                if prior is not None:
                    self._restore_map_from_prior(table, source_user, original_id, prior)
                    continue
                row = dict(source_row)
                payload = _load_payload(row["payload_json"], table=table, row_key=key)
                payload["user_id"] = self.target_user
                row["user_id"] = self.target_user
                row["payload_json"] = _canonical(payload)
                semantic = _semantic_digest(table, row)
                existing = by_semantic.get(semantic)
                if existing is not None:
                    target_id = str(existing["id"])
                    source_reaffirmed = str(row.get("reaffirmed_at") or "")
                    target_reaffirmed = str(existing.get("reaffirmed_at") or "")
                    action = "deduplicated"
                    detail = None
                    if source_reaffirmed > target_reaffirmed:
                        existing_payload = _load_payload(
                            existing["payload_json"], table=table, row_key=target_id
                        )
                        existing_payload["reaffirmed_at"] = source_reaffirmed
                        self.target.execute(
                            """UPDATE goals SET reaffirmed_at=?,payload_json=?
                            WHERE id=? AND user_id=?""",
                            (
                                source_reaffirmed,
                                _canonical(existing_payload),
                                target_id,
                                self.target_user,
                            ),
                        )
                        existing["reaffirmed_at"] = source_reaffirmed
                        existing["payload_json"] = _canonical(existing_payload)
                        action = "merged"
                        detail = {"reaffirmed_at_advanced_from": target_reaffirmed}
                    self.maps[(table, source_user, original_id)] = target_id
                    self._record(
                        table,
                        source_user,
                        key,
                        source_hash,
                        target_key=target_id,
                        action=action,
                        detail=detail,
                    )
                    continue
                target_id = original_id
                remapped = False
                if target_id in by_id or self.target.execute(
                    "SELECT 1 FROM goals WHERE user_id=? AND id=?",
                    (self.target_user, target_id),
                ).fetchone():
                    target_id = _deterministic_id(
                        self.target, table, self.target_user, self.source_label, source_user,
                        original_id, source_hash,
                    )
                    remapped = True
                row["id"] = target_id
                payload["id"] = target_id
                row["payload_json"] = _canonical(payload)
                _insert_row(self.target, table, row)
                self.maps[(table, source_user, original_id)] = target_id
                by_id[target_id] = row
                by_semantic[_semantic_digest(table, row)] = row
                self._record(
                    table,
                    source_user,
                    key,
                    source_hash,
                    target_key=target_id,
                    action="inserted",
                    detail={"id_remapped_from": original_id} if remapped else None,
                )

    def merge_events(self) -> None:
        table = "events"
        if not self._ready(table):
            return
        target_rows = [
            _row_dict(row)
            for row in self.target.execute(
                "SELECT * FROM events WHERE user_id=?", (self.target_user,)
            )
        ]
        by_id = {str(row["id"]): row for row in target_rows}
        by_evidence = {
            str(row["evidence_ref"]): row
            for row in target_rows
            if row.get("evidence_ref") is not None
        }
        by_semantic = {_semantic_digest(table, row): row for row in target_rows}
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                original_id = str(source_row["id"])
                if prior is not None:
                    self._restore_map_from_prior(table, source_user, original_id, prior)
                    continue
                row = dict(source_row)
                payload = _load_payload(row["payload_json"], table=table, row_key=key)
                row["user_id"] = self.target_user
                payload["user_id"] = self.target_user
                row["payload_json"] = _canonical(payload)
                semantic = _semantic_digest(table, row)
                existing = by_semantic.get(semantic)
                if existing is not None:
                    target_id = str(existing["id"])
                    self.maps[(table, source_user, original_id)] = target_id
                    self._record(
                        table, source_user, key, source_hash,
                        target_key=target_id, action="deduplicated",
                    )
                    continue

                evidence = row.get("evidence_ref")
                evidence_existing = (
                    by_evidence.get(str(evidence)) if evidence is not None else None
                )
                if (
                    evidence_existing is not None
                    and _event_observation_digest(row)
                    == _event_observation_digest(evidence_existing)
                ):
                    target_id = str(evidence_existing["id"])
                    self.maps[(table, source_user, original_id)] = target_id
                    self._record(
                        table,
                        source_user,
                        key,
                        source_hash,
                        target_key=target_id,
                        action="deduplicated",
                        detail={
                            "identity": "stable_evidence_ref",
                            "source_consent_scope": payload.get("consent_scope"),
                            "source_app_label": (
                                payload.get("facts", {}).get("app_label")
                                if isinstance(payload.get("facts"), dict)
                                else None
                            ),
                        },
                    )
                    continue

                target_id = original_id
                remapped = False
                if target_id in by_id or self.target.execute(
                    "SELECT 1 FROM events WHERE user_id=? AND id=?",
                    (self.target_user, target_id),
                ).fetchone():
                    target_id = _deterministic_id(
                        self.target, table, self.target_user, self.source_label, source_user,
                        original_id, source_hash,
                    )
                    remapped = True

                evidence_collision = False
                if evidence is not None and str(evidence) in by_evidence:
                    evidence_collision = True
                    original_evidence = str(evidence)
                    row["evidence_ref"] = (
                        f"{original_evidence}#import-{source_hash[:12]}"
                    )
                    payload["evidence_ref"] = row["evidence_ref"]
                row["id"] = target_id
                payload["event_id"] = target_id
                row["payload_json"] = _canonical(payload)
                _insert_row(self.target, table, row)
                self.maps[(table, source_user, original_id)] = target_id
                by_id[target_id] = row
                if row.get("evidence_ref") is not None:
                    by_evidence[str(row["evidence_ref"])] = row
                by_semantic[_semantic_digest(table, row)] = row
                detail: dict[str, Any] = {}
                if remapped:
                    detail["id_remapped_from"] = original_id
                if evidence_collision:
                    detail["evidence_ref_remapped_from"] = evidence
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_id,
                    action="conflict_preserved" if detail else "inserted",
                    detail=detail or None,
                )

    def merge_advice(self) -> None:
        table = "advice"
        if not self._ready(table):
            return
        target_rows = [
            _row_dict(row)
            for row in self.target.execute(
                "SELECT * FROM advice WHERE user_id=?", (self.target_user,)
            )
        ]
        by_id = {str(row["id"]): row for row in target_rows}
        by_semantic = {_semantic_digest(table, row): row for row in target_rows}
        live_topics = {
            str(row["topic_key"])
            for row in target_rows
            if row.get("topic_key") is not None and row.get("status") in NONTERMINAL_ADVICE
        }
        live_evidence = {
            str(row["evidence_ref"])
            for row in target_rows
            if row.get("evidence_ref") is not None and row.get("status") in NONTERMINAL_ADVICE
        }
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                original_id = str(source_row["id"])
                if prior is not None:
                    self._restore_map_from_prior(table, source_user, original_id, prior)
                    continue
                row = dict(source_row)
                payload = _load_payload(row["payload_json"], table=table, row_key=key)
                row["user_id"] = self.target_user
                payload["user_id"] = self.target_user
                old_goal = row.get("goal_id") or payload.get("goal_id")
                if old_goal is not None:
                    mapped_goal = self.maps.get(
                        ("goals", source_user, str(old_goal)), str(old_goal)
                    )
                    if "goal_id" in _columns(self.target, table):
                        row["goal_id"] = mapped_goal
                    payload["goal_id"] = mapped_goal
                evidence_items = payload.get("evidence")
                if isinstance(evidence_items, list):
                    for item in evidence_items:
                        if not isinstance(item, dict) or item.get("event_id") is None:
                            continue
                        old_event = str(item["event_id"])
                        item["event_id"] = self.maps.get(
                            ("events", source_user, old_event), old_event
                        )
                row["payload_json"] = _canonical(payload)
                semantic = _semantic_digest(table, row)
                existing = by_semantic.get(semantic)
                if existing is not None:
                    target_id = str(existing["id"])
                    self.maps[(table, source_user, original_id)] = target_id
                    self.advice_overlap_users.add(source_user)
                    source_status = str(row.get("status") or "")
                    target_status = str(existing.get("status") or "")
                    detail = (
                        {"source_status": source_status, "destination_status": target_status}
                        if source_status != target_status else None
                    )
                    if source_status in {"adopted", "dismissed", "withdrawn", "verified"}:
                        self._resolve_attention_from_imported_feedback(
                            target_id, f"imported_status:{source_status}"
                        )
                    self._record(
                        table, source_user, key, source_hash,
                        target_key=target_id, action="deduplicated", detail=detail,
                    )
                    continue

                target_id = original_id
                remapped = False
                if target_id in by_id or self.target.execute(
                    "SELECT 1 FROM advice WHERE user_id=? AND id=?",
                    (self.target_user, target_id),
                ).fetchone():
                    target_id = _deterministic_id(
                        self.target, table, self.target_user, self.source_label, source_user,
                        original_id, source_hash,
                    )
                    remapped = True
                original_status = str(row.get("status") or payload.get("status") or "active")
                topic = row.get("topic_key") or payload.get("topic_key")
                if topic is not None and "topic_key" in _columns(self.target, table):
                    row["topic_key"] = topic
                evidence = row.get("evidence_ref")
                live_collision = (
                    original_status in NONTERMINAL_ADVICE
                    and (
                        (topic is not None and str(topic) in live_topics)
                        or (evidence is not None and str(evidence) in live_evidence)
                    )
                )
                if original_status == "provisional" or live_collision:
                    row["status"] = "withdrawn"
                    payload["status"] = "withdrawn"
                row["id"] = target_id
                payload["id"] = target_id
                if (
                    "published_at" in _columns(self.target, table)
                    and row.get("published_at") is None
                ):
                    row["published_at"] = (
                        None if original_status == "provisional" else row.get("created_at")
                    )
                row["payload_json"] = _canonical(payload)
                _insert_row(self.target, table, row)
                self._insert_resolved_attention(row)
                self.maps[(table, source_user, original_id)] = target_id
                by_id[target_id] = row
                by_semantic[_semantic_digest(table, row)] = row
                if row.get("status") in NONTERMINAL_ADVICE:
                    if topic is not None:
                        live_topics.add(str(topic))
                    if evidence is not None:
                        live_evidence.add(str(evidence))
                detail: dict[str, Any] = {"attention": "legacy_no_replay"}
                if remapped:
                    detail["id_remapped_from"] = original_id
                if str(row.get("status")) != original_status:
                    detail["original_status"] = original_status
                    detail["imported_status"] = row.get("status")
                    detail["reason"] = (
                        "live_topic_or_evidence_collision"
                        if live_collision
                        else "unfinished_provisional"
                    )
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_id,
                    action="conflict_preserved" if remapped or live_collision else "inserted",
                    detail=detail,
                )

    def _insert_resolved_attention(self, advice_row: Mapping[str, Any]) -> None:
        if not _table_exists(self.target, "advice_attention"):
            return
        advice_id = str(advice_row["id"])
        topic = advice_row.get("topic_key") or advice_row.get("dedupe_key") or f"legacy:{advice_id}"
        created_at = advice_row.get("created_at") or self.now
        self.target.execute(
            """INSERT INTO advice_attention(
              advice_id,user_id,topic_key,state,delivery_count,resolved_at,
              resolved_reason,created_at,updated_at
            ) VALUES(?,?,?,'resolved',0,?,'legacy_no_replay',?,?)
            ON CONFLICT(user_id,advice_id) DO NOTHING""",
            (advice_id, self.target_user, str(topic), self.now, created_at, self.now),
        )

    def merge_feedback(self) -> None:
        table = "feedback"
        if not self._ready(table):
            return
        target_columns = set(_columns(self.target, table))
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                if prior is not None:
                    continue
                row = dict(source_row)
                old_advice = str(row["advice_id"])
                mapped_advice = self.maps.get(("advice", source_user, old_advice))
                if mapped_advice is None:
                    existing = self.target.execute(
                        "SELECT id FROM advice WHERE id=? AND user_id=?",
                        (old_advice, self.target_user),
                    ).fetchone()
                    mapped_advice = str(existing[0]) if existing else None
                if mapped_advice is None:
                    self._record(
                        table, source_user, key, source_hash,
                        target_key=None, action="skipped_missing_parent",
                        detail={"source_advice_id": old_advice, "source_row": row},
                    )
                    continue
                row["advice_id"] = mapped_advice
                row["user_id"] = self.target_user
                semantic_key = row.get("semantic_key")
                if semantic_key and "semantic_key" in target_columns:
                    existing = self.target.execute(
                        """SELECT * FROM feedback
                        WHERE user_id=? AND advice_id=? AND semantic_key=?""",
                        (self.target_user, mapped_advice, semantic_key),
                    ).fetchone()
                    if existing:
                        same = all(
                            existing[name] == row.get(name)
                            for name in ("kind", "note", "origin", "signal_weight")
                            if name in row and name in existing.keys()
                        )
                        self._resolve_attention_from_imported_feedback(
                            mapped_advice, str(row.get("kind") or "feedback")
                        )
                        self._record(
                            table, source_user, key, source_hash,
                            target_key=str(existing["id"]),
                            action="deduplicated" if same else "conflict_preserved",
                            detail=None if same else {
                                "destination_won": True,
                                "source_row": row,
                            },
                        )
                        continue
                feedback_id = row.get("feedback_id")
                feedback_remapped = False
                if feedback_id and "feedback_id" in target_columns:
                    collision = self.target.execute(
                        "SELECT * FROM feedback WHERE user_id=? AND feedback_id=?",
                        (self.target_user, feedback_id),
                    ).fetchone()
                    if collision is not None:
                        comparable = _row_dict(collision)
                        same = all(
                            comparable.get(name) == row.get(name)
                            for name in ("advice_id", "kind", "note", "origin", "signal_weight")
                            if name in comparable and name in row
                        )
                        if same:
                            self._resolve_attention_from_imported_feedback(
                                mapped_advice, str(row.get("kind") or "feedback")
                            )
                            self._record(
                                table, source_user, key, source_hash,
                                target_key=str(comparable["id"]), action="deduplicated",
                            )
                            continue
                        row["feedback_id"] = str(
                            uuid5(
                                IMPORT_NAMESPACE,
                                f"{self.source_label}:{source_user}:feedback:{feedback_id}:{source_hash}",
                            )
                        )
                        feedback_remapped = True
                _insert_row(self.target, table, row, omit={"id"})
                target_id = str(self.target.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._resolve_attention_from_imported_feedback(
                    mapped_advice, str(row.get("kind") or "feedback")
                )
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_id, action="inserted",
                    detail={"feedback_id_remapped_from": feedback_id}
                    if feedback_remapped else None,
                )

    def _resolve_attention_from_imported_feedback(
        self, advice_id: str, kind: str
    ) -> None:
        if not _table_exists(self.target, "advice_attention"):
            return
        advice = self.target.execute(
            "SELECT * FROM advice WHERE id=? AND user_id=?",
            (advice_id, self.target_user),
        ).fetchone()
        if advice is None:
            return
        self._insert_resolved_attention(_row_dict(advice))
        attention_columns = set(_columns(self.target, "advice_attention"))
        assignments = [
            "state='resolved'",
            "claim_token=NULL",
            "claim_device_id=NULL",
            "claim_expires_at=NULL",
            "next_eligible_at=NULL",
            "auto_close_at=NULL",
            "resolved_at=COALESCE(resolved_at,?)",
            "resolved_reason=COALESCE(resolved_reason,'imported_feedback')",
            "updated_at=?",
        ]
        values: list[Any] = [self.now, self.now]
        if "handled_at" in attention_columns:
            assignments.append("handled_at=COALESCE(handled_at,?)")
            values.append(self.now)
        if "handling_kind" in attention_columns:
            assignments.append("handling_kind=COALESCE(handling_kind,?)")
            values.append(kind)
        values.extend((self.target_user, advice_id))
        self.target.execute(
            f"""UPDATE advice_attention SET {','.join(assignments)}
            WHERE user_id=? AND advice_id=?""",
            tuple(values),
        )
        if _table_exists(self.target, "advice_attention_attempts"):
            self.target.execute(
                """UPDATE advice_attention_attempts
                SET state='cancelled',completed_at=COALESCE(completed_at,?)
                WHERE user_id=? AND advice_id=? AND state='claimed'""",
                (self.now, self.target_user, advice_id),
            )

    def merge_relevance(self) -> None:
        table = "relevance_ledger"
        if not self._ready(table):
            return
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                if prior is not None:
                    continue
                row = dict(source_row)
                old_advice = str(row["advice_id"])
                mapped = self.maps.get(("advice", source_user, old_advice), old_advice)
                if self.target.execute(
                    "SELECT 1 FROM advice WHERE id=? AND user_id=?",
                    (mapped, self.target_user),
                ).fetchone() is None:
                    self._record(
                        table, source_user, key, source_hash,
                        target_key=None, action="skipped_missing_parent",
                        detail={"source_row": row},
                    )
                    continue
                row["advice_id"] = mapped
                row["user_id"] = self.target_user
                existing = self.target.execute(
                    """SELECT * FROM relevance_ledger
                    WHERE user_id=? AND advice_id=? AND signal_kind=? AND origin=?""",
                    (self.target_user, mapped, row["signal_kind"], row["origin"]),
                ).fetchone()
                target_key = _canonical([mapped, row["signal_kind"], row["origin"]])
                if existing is None:
                    _insert_row(self.target, table, row)
                    action = "inserted"
                else:
                    self.target.execute(
                        """UPDATE relevance_ledger
                        SET weight=MAX(weight,?),created_at=MAX(created_at,?),
                            topic_key=CASE WHEN ?>=created_at THEN ? ELSE topic_key END
                        WHERE user_id=? AND advice_id=? AND signal_kind=? AND origin=?""",
                        (
                            row["weight"], row["created_at"], row["created_at"],
                            row["topic_key"], self.target_user, mapped,
                            row["signal_kind"], row["origin"],
                        ),
                    )
                    action = "merged"
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_key, action=action,
                    detail={"source_row": row} if action == "merged" else None,
                )

    def merge_outcomes(self) -> None:
        table = "outcomes"
        if not self._ready(table):
            return
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                if prior is not None:
                    continue
                row = dict(source_row)
                old_advice = str(row["advice_id"])
                mapped = self.maps.get(("advice", source_user, old_advice), old_advice)
                if self.target.execute(
                    "SELECT 1 FROM advice WHERE id=? AND user_id=?",
                    (mapped, self.target_user),
                ).fetchone() is None:
                    self._record(
                        table, source_user, key, source_hash,
                        target_key=None, action="skipped_missing_parent",
                        detail={"source_row": row},
                    )
                    continue
                row["advice_id"] = mapped
                row["user_id"] = self.target_user
                existing = self.target.execute(
                    "SELECT * FROM outcomes WHERE user_id=? AND advice_id=?",
                    (self.target_user, mapped),
                ).fetchone()
                if existing is None:
                    _insert_row(self.target, table, row)
                    action = "inserted"
                elif all(
                    existing[name] == row.get(name)
                    for name in ("status", "actual_result", "utility", "timing_quality")
                    if name in row
                ):
                    action = "deduplicated"
                else:
                    # One outcome is allowed per advice. Destination wins; the
                    # source hash and conflict are retained in the import ledger.
                    action = "conflict_preserved"
                self._record(
                    table, source_user, key, source_hash,
                    target_key=mapped, action=action,
                    detail={"destination_won": True, "source_row": row}
                    if action == "conflict_preserved" else None,
                )

    def merge_charters(self) -> None:
        table = "charters"
        if not self._ready(table):
            return
        for source_user in self.source_users:
            for row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, row)
                if prior is not None:
                    continue
                self.target.execute(
                    """INSERT INTO charters(
                      user_id,domain,max_level,redline_authorized,updated_at
                    ) VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id,domain) DO UPDATE SET
                      max_level=MAX(charters.max_level,excluded.max_level),
                      redline_authorized=MAX(charters.redline_authorized,excluded.redline_authorized),
                      updated_at=MAX(charters.updated_at,excluded.updated_at)""",
                    (
                        self.target_user, row["domain"], row["max_level"],
                        row["redline_authorized"], row["updated_at"],
                    ),
                )
                target_key = _canonical([self.target_user, row["domain"]])
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_key, action="merged",
                )

    def merge_trust(self) -> None:
        table = "trust_accounts"
        if not self._ready(table):
            return
        for source_user in self.source_users:
            for row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, row)
                if prior is not None:
                    continue
                conservative = (
                    source_user == self.target_user
                    or source_user in self.advice_overlap_users
                )
                if conservative:
                    self.target.execute(
                        """INSERT INTO trust_accounts(
                          user_id,domain,level,judged,correct,brier_sum,utility_sum,
                          timing_sum,catastrophic_errors,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,domain,level) DO UPDATE SET
                          judged=MAX(trust_accounts.judged,excluded.judged),
                          correct=MAX(trust_accounts.correct,excluded.correct),
                          brier_sum=MAX(trust_accounts.brier_sum,excluded.brier_sum),
                          utility_sum=MAX(trust_accounts.utility_sum,excluded.utility_sum),
                          timing_sum=MAX(trust_accounts.timing_sum,excluded.timing_sum),
                          catastrophic_errors=MAX(
                            trust_accounts.catastrophic_errors,excluded.catastrophic_errors
                          ),
                          updated_at=MAX(trust_accounts.updated_at,excluded.updated_at)""",
                        (
                            self.target_user, row["domain"], row["level"], row["judged"],
                            row["correct"], row["brier_sum"], row["utility_sum"],
                            row["timing_sum"], row["catastrophic_errors"], row["updated_at"],
                        ),
                    )
                else:
                    self.target.execute(
                        """INSERT INTO trust_accounts(
                          user_id,domain,level,judged,correct,brier_sum,utility_sum,
                          timing_sum,catastrophic_errors,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_id,domain,level) DO UPDATE SET
                          judged=trust_accounts.judged+excluded.judged,
                          correct=trust_accounts.correct+excluded.correct,
                          brier_sum=trust_accounts.brier_sum+excluded.brier_sum,
                          utility_sum=trust_accounts.utility_sum+excluded.utility_sum,
                          timing_sum=trust_accounts.timing_sum+excluded.timing_sum,
                          catastrophic_errors=trust_accounts.catastrophic_errors+excluded.catastrophic_errors,
                          updated_at=MAX(trust_accounts.updated_at,excluded.updated_at)""",
                        (
                            self.target_user, row["domain"], row["level"], row["judged"],
                            row["correct"], row["brier_sum"], row["utility_sum"],
                            row["timing_sum"], row["catastrophic_errors"], row["updated_at"],
                        ),
                    )
                target_key = _canonical([self.target_user, row["domain"], row["level"]])
                self._record(
                    table, source_user, key, source_hash,
                    target_key=target_key, action="merged",
                    detail={
                        "merge_policy": "component_max" if conservative else "sum_once"
                    },
                )

    def merge_preferences(self) -> None:
        table = "advice_preferences"
        if not self._ready(table):
            return
        target_columns = set(_columns(self.target, table))
        for source_user in self.source_users:
            for source_row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, source_row)
                if prior is not None:
                    continue
                row = dict(source_row)
                old_goal = row.get("goal_id")
                if old_goal is not None:
                    row["goal_id"] = self.maps.get(
                        ("goals", source_user, str(old_goal)), str(old_goal)
                    )
                    if str(row.get("scope_key", "")).startswith("goal:"):
                        row["scope_key"] = f"goal:{row['goal_id']}"
                row["user_id"] = self.target_user
                scope = row["scope_key"]
                existing = self.target.execute(
                    "SELECT * FROM advice_preferences WHERE user_id=? AND scope_key=?",
                    (self.target_user, scope),
                ).fetchone()
                if existing is None:
                    _insert_row(self.target, table, row)
                    action = "inserted"
                elif str(row.get("updated_at", "")) > str(existing["updated_at"]):
                    if "revision" in row and "revision" in target_columns:
                        row["revision"] = max(
                            int(row.get("revision") or 0), int(existing["revision"] or 0)
                        )
                    update_columns = [
                        name for name in row
                        if name in target_columns and name not in {"user_id", "scope_key"}
                    ]
                    assignments = ",".join(f'"{name}"=?' for name in update_columns)
                    self.target.execute(
                        f"UPDATE advice_preferences SET {assignments} "
                        "WHERE user_id=? AND scope_key=?",
                        tuple(row[name] for name in update_columns)
                        + (self.target_user, scope),
                    )
                    action = "merged"
                else:
                    same = all(
                        existing[name] == row.get(name)
                        for name in row
                        if name in target_columns and name not in {"user_id", "scope_key"}
                    )
                    action = "deduplicated" if same else "conflict_preserved"
                self._record(
                    table, source_user, key, source_hash,
                    target_key=_canonical([self.target_user, scope]), action=action,
                    detail={"destination_won": True, "source_row": row}
                    if action == "conflict_preserved" else None,
                )

    def merge_topic_suppressions(self) -> None:
        table = "topic_suppressions"
        if not self._ready(table):
            return
        for source_user in self.source_users:
            for row in self._rows(table, source_user):
                key, source_hash, prior = self._begin_row(table, source_user, row)
                if prior is not None:
                    continue
                old_advice = str(row["source_advice_id"])
                mapped = self.maps.get(("advice", source_user, old_advice), old_advice)
                self.target.execute(
                    """INSERT INTO topic_suppressions(
                      user_id,dedupe_key,source_advice_id,reason,created_at,updated_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,dedupe_key) DO UPDATE SET
                      source_advice_id=CASE
                        WHEN excluded.expires_at>=topic_suppressions.expires_at
                        THEN excluded.source_advice_id ELSE topic_suppressions.source_advice_id END,
                      reason=CASE
                        WHEN excluded.expires_at>=topic_suppressions.expires_at
                        THEN excluded.reason ELSE topic_suppressions.reason END,
                      created_at=MIN(topic_suppressions.created_at,excluded.created_at),
                      updated_at=MAX(topic_suppressions.updated_at,excluded.updated_at),
                      expires_at=MAX(topic_suppressions.expires_at,excluded.expires_at)""",
                    (
                        self.target_user, row["dedupe_key"], mapped, row["reason"],
                        row["created_at"], row["updated_at"], row["expires_at"],
                    ),
                )
                self._record(
                    table, source_user, key, source_hash,
                    target_key=_canonical([self.target_user, row["dedupe_key"]]),
                    action="merged",
                )

    def _ready(self, table: str) -> bool:
        source_has = _table_exists(self.source, table)
        target_has = _table_exists(self.target, table)
        if source_has and not target_has and table in CORE_TABLES:
            raise RuntimeError(f"target database is missing required table {table}")
        if not source_has or not target_has:
            reason = "source_table_missing" if not source_has else "target_table_missing"
            self.stats[table][reason] += 1
            return False
        return True

    def run(self) -> dict[str, Any]:
        target_before = {
            table: self.target.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?',
                (self.target_user,),
            ).fetchone()[0]
            for table in HISTORY_TABLES
            if _table_exists(self.target, table) and "user_id" in _columns(self.target, table)
        }
        self.target.execute(
            "INSERT OR IGNORE INTO users(id,created_at) VALUES(?,?)",
            (self.target_user, self.now),
        )
        self.merge_goals()
        self.merge_events()
        self.merge_advice()
        self.merge_feedback()
        self.merge_relevance()
        self.merge_outcomes()
        self.merge_charters()
        self.merge_trust()
        self.merge_preferences()
        self.merge_topic_suppressions()

        skipped: dict[str, dict[str, Any]] = {}
        for table, reason in SKIPPED_TABLE_REASONS.items():
            if not _table_exists(self.source, table):
                continue
            columns = set(_columns(self.source, table))
            count = 0
            if "user_id" in columns:
                placeholders = ",".join("?" for _ in self.source_users)
                count = self.source.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE user_id IN ({placeholders})',
                    tuple(self.source_users),
                ).fetchone()[0]
            skipped[table] = {"source_rows": count, "reason": reason}

        known = set(HISTORY_TABLES) | set(SKIPPED_TABLE_REASONS) | {"users"}
        unknown = []
        for row in self.source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            table = row[0]
            if table in known or "user_id" not in set(_columns(self.source, table)):
                continue
            placeholders = ",".join("?" for _ in self.source_users)
            count = self.source.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id IN ({placeholders})',
                tuple(self.source_users),
            ).fetchone()[0]
            unknown.append({"table": table, "source_rows": count, "reason": "unsupported_table"})

        foreign_key_errors = self.target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"target foreign key check failed with {len(foreign_key_errors)} rows"
            )
        integrity = self.target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"target integrity check failed: {integrity}")
        target_after = {
            table: self.target.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id=?',
                (self.target_user,),
            ).fetchone()[0]
            for table in target_before
        }
        lost = {
            table: {"before": count, "after": target_after[table]}
            for table, count in target_before.items()
            if target_after[table] < count
        }
        if lost:
            raise RuntimeError(f"destination row loss detected: {_canonical(lost)}")
        return {
            "source_label": self.source_label,
            "source_users": self.source_users,
            "target_user": self.target_user,
            "tables": {
                table: dict(sorted(values.items()))
                for table, values in self.stats.items()
            },
            "skipped_tables": skipped,
            "unknown_tables": unknown,
            "conflict_details": self.details,
            "target_before": target_before,
            "target_after": target_after,
            "integrity": integrity,
            "foreign_key_errors": 0,
        }


def merge_connections(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    source_label: str,
    source_users: list[str],
    target_user: str,
) -> dict[str, Any]:
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("source integrity check failed")
    available = {
        row[0] for row in source.execute("SELECT id FROM users")
    } if _table_exists(source, "users") else set()
    missing = [user for user in source_users if user not in available]
    if missing:
        raise RuntimeError(f"source users not found: {', '.join(missing)}")
    return DatabaseMerger(
        source,
        target,
        source_label=source_label,
        source_users=source_users,
        target_user=target_user,
    ).run()


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


def run_merge(
    source_path: Path,
    target_path: Path,
    *,
    source_label: str,
    source_users: list[str],
    target_user: str,
    apply: bool,
    backup_path: Path | None = None,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    target_path = target_path.resolve()
    if source_path == target_path:
        raise ValueError("source and target database paths must differ")
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if not target_path.is_file():
        raise FileNotFoundError(f"target database does not exist: {target_path}")
    if apply and backup_path is None:
        raise ValueError("apply requires a backup path")

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    try:
        if apply:
            target = sqlite3.connect(target_path, timeout=5)
            try:
                _backup_database(target, backup_path.resolve())  # type: ignore[union-attr]
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN IMMEDIATE")
                try:
                    report = merge_connections(
                        source,
                        target,
                        source_label=source_label,
                        source_users=source_users,
                        target_user=target_user,
                    )
                except Exception:
                    target.rollback()
                    raise
                target.commit()
            finally:
                target.close()
        else:
            target_source = sqlite3.connect(
                f"file:{target_path.as_posix()}?mode=ro", uri=True
            )
            target = sqlite3.connect(":memory:")
            try:
                target_source.backup(target)
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN IMMEDIATE")
                report = merge_connections(
                    source,
                    target,
                    source_label=source_label,
                    source_users=source_users,
                    target_user=target_user,
                )
                target.rollback()
            finally:
                target.close()
                target_source.close()
    finally:
        source.close()
    report["mode"] = "apply" if apply else "dry-run"
    report["source_database"] = str(source_path)
    report["target_database"] = str(target_path)
    if apply and backup_path is not None:
        report["backup_database"] = str(backup_path.resolve())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="merge selected private-alpha history into a destination DB",
        epilog=(
            "Dry-run is the default. Before --apply, stop every process using the "
            "destination database; the required backup is created before mutation."
        ),
    )
    parser.add_argument("source_database", type=Path, help="immutable Windows DB snapshot")
    parser.add_argument("target_database", type=Path, help="offline VPS DB copy")
    parser.add_argument(
        "--source-label",
        required=True,
        help="stable name for this immutable snapshot, used for idempotency",
    )
    parser.add_argument(
        "--source-user",
        action="append",
        required=True,
        dest="source_users",
        help="source partition to merge; repeat for multiple local users",
    )
    parser.add_argument("--target-user", required=True)
    parser.add_argument("--apply", action="store_true", help="commit instead of simulating")
    parser.add_argument("--backup", type=Path, help="new pre-merge backup path")
    arguments = parser.parse_args()
    if arguments.apply and arguments.backup is None:
        parser.error("--apply requires --backup")
    if not arguments.apply and arguments.backup is not None:
        parser.error("--backup is only valid with --apply")
    report = run_merge(
        arguments.source_database,
        arguments.target_database,
        source_label=arguments.source_label,
        source_users=arguments.source_users,
        target_user=arguments.target_user,
        apply=arguments.apply,
        backup_path=arguments.backup,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
