from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.storage import Repository


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "merge_private_alpha_databases.py"
)
SPEC = importlib.util.spec_from_file_location("merge_private_alpha_databases", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


TARGET_USER = "oneplus12-alpha-01"
DEMO_USER = "demo-user"
GOAL_SHARED = "10000000-0000-0000-0000-000000000001"
EVENT_SHARED = "20000000-0000-0000-0000-000000000001"
EVENT_TARGET_EXACT = "20000000-0000-0000-0000-000000000002"
EVENT_SOURCE_EXACT = "20000000-0000-0000-0000-000000000003"
EVENT_EVIDENCE_CONFLICT = "20000000-0000-0000-0000-000000000004"
EVENT_SECOND_USER = "20000000-0000-0000-0000-000000000005"
ADVICE_SHARED = "30000000-0000-0000-0000-000000000001"
ADVICE_UNIQUE = "30000000-0000-0000-0000-000000000002"
FEEDBACK_SHARED = "40000000-0000-0000-0000-000000000001"


def _create_database(path: Path) -> None:
    repository = Repository(path)
    repository.close()


def _insert_user(connection: sqlite3.Connection, user_id: str, created: str) -> None:
    connection.execute(
        "INSERT INTO users(id,created_at) VALUES(?,?)", (user_id, created)
    )


def _insert_goal(
    connection: sqlite3.Connection,
    *,
    goal_id: str,
    user_id: str,
    quote: str,
    created: str,
) -> None:
    payload = {
        "id": goal_id,
        "user_id": user_id,
        "domain": "work",
        "version": 1,
        "quote": quote,
        "title": quote,
        "target": {},
        "is_redline": False,
        "valid_until": None,
        "created_at": created,
        "reaffirmed_at": created,
    }
    connection.execute(
        """INSERT INTO goals(
          id,user_id,domain,version,quote,payload_json,created_at,reaffirmed_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (goal_id, user_id, "work", 1, quote, json.dumps(payload), created, created),
    )


def _event_payload(
    *,
    event_id: str,
    user_id: str,
    event_type: str,
    occurred: str,
    evidence_ref: str,
    text: str,
    consent_scope: str = "alpha.local",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "user_id": user_id,
        "source": "desktop",
        "type": event_type,
        "occurred_at": occurred,
        "facts": {"text": text},
        "entities": [],
        "confidence": 1.0,
        "sensitivity": "personal",
        "consent_scope": consent_scope,
        "evidence_ref": evidence_ref,
        "created_at": occurred,
    }


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    user_id: str,
    event_type: str,
    occurred: str,
    evidence_ref: str,
    text: str,
    consent_scope: str = "alpha.local",
) -> None:
    payload = _event_payload(
        event_id=event_id,
        user_id=user_id,
        event_type=event_type,
        occurred=occurred,
        evidence_ref=evidence_ref,
        text=text,
        consent_scope=consent_scope,
    )
    connection.execute(
        """INSERT INTO events(
          id,user_id,source,type,occurred_at,evidence_ref,payload_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            event_id,
            user_id,
            "desktop",
            event_type,
            occurred,
            evidence_ref,
            json.dumps(payload),
            occurred,
        ),
    )


def _advice_payload(
    *,
    advice_id: str,
    user_id: str,
    goal_id: str,
    event_id: str,
    topic_key: str,
    action: str,
    status: str,
    created: str,
) -> dict[str, object]:
    return {
        "id": advice_id,
        "user_id": user_id,
        "domain": "work",
        "requested_level": 2,
        "effective_level": 2,
        "goal_id": goal_id,
        "goal_quote": "finish the important work",
        "evidence": [
            {
                "event_id": event_id,
                "source": "desktop",
                "fact": "a fact",
                "observed_at": created,
                "confidence": 1.0,
            }
        ],
        "action": action,
        "first_step": "start",
        "alternative": None,
        "prediction": {
            "outcome": "work remains blocked",
            "deadline": "2027-01-01T00:00:00+00:00",
            "confidence": 0.8,
            "observable": True,
        },
        "urgency": 0.5,
        "impact": 0.6,
        "novelty": 0.7,
        "relevance": 0.8,
        "context_fit": 1.0,
        "interruption_cost": 0.1,
        "dedupe_key": topic_key,
        "topic_key": topic_key,
        "issue_subject": topic_key,
        "requires_external_action": False,
        "proactive_score": 0.8,
        "delivery": "immediate",
        "status": status,
        "snoozed_until": None,
        "created_at": created,
    }


def _insert_advice(
    connection: sqlite3.Connection,
    *,
    advice_id: str,
    user_id: str,
    goal_id: str,
    event_id: str,
    topic_key: str,
    evidence_ref: str,
    action: str,
    status: str = "active",
    created: str = "2026-02-01T00:00:00+00:00",
) -> None:
    payload = _advice_payload(
        advice_id=advice_id,
        user_id=user_id,
        goal_id=goal_id,
        event_id=event_id,
        topic_key=topic_key,
        action=action,
        status=status,
        created=created,
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(advice)")}
    base = {
        "id": advice_id,
        "user_id": user_id,
        "domain": "work",
        "level": 2,
        "dedupe_key": topic_key,
        "topic_key": topic_key,
        "evidence_ref": evidence_ref,
        "status": status,
        "delivery": "immediate",
        "prediction_confidence": 0.8,
        "prediction_deadline": "2027-01-01T00:00:00+00:00",
        "payload_json": json.dumps(payload),
        "created_at": created,
        "goal_id": goal_id,
        "published_at": created,
    }
    selected = [name for name in base if name in columns]
    connection.execute(
        f"INSERT INTO advice({','.join(selected)}) VALUES({','.join('?' for _ in selected)})",
        tuple(base[name] for name in selected),
    )


def _seed_source(path: Path) -> None:
    _create_database(path)
    connection = sqlite3.connect(path)
    _insert_user(connection, DEMO_USER, "2026-01-01T00:00:00+00:00")
    _insert_user(connection, TARGET_USER, "2026-01-02T00:00:00+00:00")
    _insert_goal(
        connection,
        goal_id=GOAL_SHARED,
        user_id=DEMO_USER,
        quote="source goal with colliding id",
        created="2026-01-03T00:00:00+00:00",
    )
    _insert_event(
        connection,
        event_id=EVENT_SHARED,
        user_id=DEMO_USER,
        event_type="thought.note",
        occurred="2026-02-02T00:00:00+00:00",
        evidence_ref="windows:id-collision",
        text="source row with colliding id",
    )
    _insert_event(
        connection,
        event_id=EVENT_SOURCE_EXACT,
        user_id=DEMO_USER,
        event_type="thought.note",
        occurred="2026-02-03T00:00:00+00:00",
        evidence_ref="shared:exact",
        text="same exact event",
        consent_scope="alpha.minimized_context",
    )
    _insert_event(
        connection,
        event_id=EVENT_EVIDENCE_CONFLICT,
        user_id=DEMO_USER,
        event_type="screen.text",
        occurred="2026-02-04T00:00:00+00:00",
        evidence_ref="remote:evidence",
        text="different content under the same evidence ref",
    )
    _insert_event(
        connection,
        event_id=EVENT_SECOND_USER,
        user_id=TARGET_USER,
        event_type="thought.note",
        occurred="2026-02-05T00:00:00+00:00",
        evidence_ref="local-oneplus:event",
        text="history already stored under the final user on Windows",
    )
    _insert_advice(
        connection,
        advice_id=ADVICE_SHARED,
        user_id=DEMO_USER,
        goal_id=GOAL_SHARED,
        event_id=EVENT_SHARED,
        topic_key="same-topic",
        evidence_ref="windows:advice-collision",
        action="source advice must be retained as withdrawn history",
    )
    _insert_advice(
        connection,
        advice_id=ADVICE_UNIQUE,
        user_id=DEMO_USER,
        goal_id=GOAL_SHARED,
        event_id=EVENT_EVIDENCE_CONFLICT,
        topic_key="unique-source-topic",
        evidence_ref="windows:advice-unique",
        action="unique source advice",
    )
    feedback_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(feedback)")
    }
    feedback = {
        "advice_id": ADVICE_SHARED,
        "user_id": DEMO_USER,
        "feedback_id": FEEDBACK_SHARED,
        "semantic_key": "source-guidance",
        "kind": "guidance",
        "note": "focus on execution",
        "origin": "user",
        "signal_weight": 1.0,
        "created_at": "2026-02-06T00:00:00+00:00",
    }
    names = [name for name in feedback if name in feedback_columns]
    connection.execute(
        f"INSERT INTO feedback({','.join(names)}) VALUES({','.join('?' for _ in names)})",
        tuple(feedback[name] for name in names),
    )
    connection.execute(
        """INSERT INTO relevance_ledger(
          advice_id,user_id,topic_key,signal_kind,origin,weight,created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (
            ADVICE_SHARED,
            DEMO_USER,
            "same-topic",
            "guidance",
            "user",
            1.0,
            "2026-02-06T00:00:00+00:00",
        ),
    )
    connection.execute(
        """INSERT INTO outcomes(
          advice_id,user_id,status,actual_result,utility,timing_quality,created_at
        ) VALUES(?,?,?,?,?,?,?)""",
        (
            ADVICE_UNIQUE,
            DEMO_USER,
            "correct",
            "the issue was resolved",
            0.9,
            0.8,
            "2026-02-07T00:00:00+00:00",
        ),
    )
    connection.execute(
        """INSERT INTO trust_accounts(
          user_id,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
          catastrophic_errors,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (DEMO_USER, "work", 2, 3, 2, 0.4, 1.5, 1.2, 0, "2026-03-01"),
    )
    connection.execute(
        "INSERT INTO charters VALUES(?,?,?,?,?)",
        (DEMO_USER, "work", 4, 1, "2026-03-01"),
    )
    if "advice_preferences" in {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        connection.execute(
            """INSERT INTO advice_preferences(
              user_id,scope_key,goal_id,direction,direction_mode,frequency_mode,
              event_types_json,revision,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                DEMO_USER,
                "global",
                None,
                "prioritize valuable information",
                "replace",
                "active",
                "[]",
                3,
                "2026-03-01",
            ),
        )
    connection.execute(
        """INSERT INTO devices(
          user_id,device_id,platform,app_version,created_at,last_seen_at
        ) VALUES(?,?,?,?,?,?)""",
        (DEMO_USER, "windows-old", "windows", "1", "2026-01-01", "2026-03-01"),
    )
    connection.execute(
        """INSERT INTO advice_attention(
          advice_id,user_id,topic_key,state,delivery_count,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,?,?)""",
        (
            ADVICE_SHARED,
            DEMO_USER,
            "same-topic",
            "2026-02-01T00:00:00+00:00",
            "2026-02-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        """INSERT INTO cloud_slices(
          user_id,provider,model,purpose,redacted_context_json,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (DEMO_USER, "test", "test", "test", "{}", "2026-02-01"),
    )
    connection.commit()
    connection.close()


def _seed_target(path: Path) -> None:
    _create_database(path)
    connection = sqlite3.connect(path)
    _insert_user(connection, TARGET_USER, "2026-01-01T00:00:00+00:00")
    _insert_goal(
        connection,
        goal_id=GOAL_SHARED,
        user_id=TARGET_USER,
        quote="remote Android goal must remain",
        created="2026-01-01T00:00:00+00:00",
    )
    _insert_event(
        connection,
        event_id=EVENT_SHARED,
        user_id=TARGET_USER,
        event_type="notification.posted",
        occurred="2026-02-01T00:00:00+00:00",
        evidence_ref="remote:evidence",
        text="remote Android event must remain",
    )
    _insert_event(
        connection,
        event_id=EVENT_TARGET_EXACT,
        user_id=TARGET_USER,
        event_type="thought.note",
        occurred="2026-02-03T00:00:00+00:00",
        evidence_ref="shared:exact",
        text="same exact event",
        consent_scope="owner_full_context",
    )
    _insert_advice(
        connection,
        advice_id=ADVICE_SHARED,
        user_id=TARGET_USER,
        goal_id=GOAL_SHARED,
        event_id=EVENT_SHARED,
        topic_key="same-topic",
        evidence_ref="remote:advice",
        action="remote advice must remain active",
    )
    feedback_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(feedback)")
    }
    feedback = {
        "advice_id": ADVICE_SHARED,
        "user_id": TARGET_USER,
        "feedback_id": FEEDBACK_SHARED,
        "semantic_key": "remote-useful",
        "kind": "useful",
        "note": None,
        "origin": "user",
        "signal_weight": 1.0,
        "created_at": "2026-02-02T00:00:00+00:00",
    }
    names = [name for name in feedback if name in feedback_columns]
    connection.execute(
        f"INSERT INTO feedback({','.join(names)}) VALUES({','.join('?' for _ in names)})",
        tuple(feedback[name] for name in names),
    )
    connection.execute(
        """INSERT INTO trust_accounts(
          user_id,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
          catastrophic_errors,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (TARGET_USER, "work", 2, 5, 4, 0.6, 2.5, 2.2, 0, "2026-02-01"),
    )
    if "advice_preferences" in {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        connection.execute(
            """INSERT INTO advice_preferences(
              user_id,scope_key,goal_id,direction,direction_mode,frequency_mode,
              event_types_json,revision,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                TARGET_USER,
                "global",
                None,
                "old remote preference",
                "replace",
                "balanced",
                "[]",
                1,
                "2026-02-01",
            ),
        )
    connection.execute(
        """INSERT INTO devices(
          user_id,device_id,platform,app_version,created_at,last_seen_at
        ) VALUES(?,?,?,?,?,?)""",
        (TARGET_USER, "android-live", "android", "2", "2026-01-01", "2026-03-01"),
    )
    connection.commit()
    connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_is_read_only_and_reports_runtime_tables(tmp_path: Path):
    source = tmp_path / "windows.db"
    target = tmp_path / "vps.db"
    _seed_source(source)
    _seed_target(target)
    source_connection = sqlite3.connect(source)
    source_advice = source_connection.execute(
        "SELECT user_id,id FROM advice ORDER BY user_id,id LIMIT 1"
    ).fetchone()
    source_connection.execute(
        """INSERT INTO advice_localizations(
          user_id,advice_id,locale,source_hash,status,attempts,next_attempt_at,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            source_advice[0],
            source_advice[1],
            "en-US",
            "0" * 64,
            "pending",
            0,
            "2026-03-01T00:00:00+00:00",
            "2026-03-01T00:00:00+00:00",
            "2026-03-01T00:00:00+00:00",
        ),
    )
    source_connection.commit()
    source_connection.close()
    source_before = _sha256(source)
    target_before = _sha256(target)

    report = MODULE.run_merge(
        source,
        target,
        source_label="windows-local",
        source_users=[DEMO_USER, TARGET_USER],
        target_user=TARGET_USER,
        apply=False,
    )

    assert report["mode"] == "dry-run"
    assert report["integrity"] == "ok"
    assert report["tables"]["events"]["source_rows"] == 4
    assert report["target_before"]["events"] == 2
    assert report["target_after"]["events"] == 5
    assert report["skipped_tables"]["devices"] == {
        "source_rows": 1,
        "reason": "device registration is destination-local",
    }
    assert report["skipped_tables"]["advice_attention"]["source_rows"] == 1
    assert report["skipped_tables"]["cloud_slices"]["source_rows"] == 1
    assert report["skipped_tables"]["advice_localizations"] == {
        "source_rows": 1,
        "reason": "derived display translations are rebuilt at destination",
    }
    assert not any(
        item["table"] == "advice_localizations"
        for item in report["unknown_tables"]
    )
    assert _sha256(source) == source_before
    assert _sha256(target) == target_before
    connection = sqlite3.connect(target)
    assert not MODULE._table_exists(connection, "data_import_ledger")
    connection.close()


def test_apply_preserves_remote_history_remaps_conflicts_and_never_replays_attention(
    tmp_path: Path,
):
    source = tmp_path / "windows.db"
    target = tmp_path / "vps.db"
    backup = tmp_path / "vps-before-merge.db"
    _seed_source(source)
    _seed_target(target)
    source_before = _sha256(source)

    report = MODULE.run_merge(
        source,
        target,
        source_label="windows-local",
        source_users=[DEMO_USER, TARGET_USER],
        target_user=TARGET_USER,
        apply=True,
        backup_path=backup,
    )

    assert report["mode"] == "apply"
    assert backup.is_file()
    assert _sha256(source) == source_before
    backup_connection = sqlite3.connect(backup)
    assert backup_connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert not MODULE._table_exists(backup_connection, "data_import_ledger")
    backup_connection.close()

    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT COUNT(*) FROM events WHERE user_id=?", (TARGET_USER,)
    ).fetchone()[0] == 5
    remote = connection.execute(
        "SELECT payload_json FROM events WHERE id=?", (EVENT_SHARED,)
    ).fetchone()
    assert json.loads(remote[0])["facts"]["text"] == "remote Android event must remain"
    assert connection.execute(
        "SELECT COUNT(*) FROM events WHERE evidence_ref='shared:exact'"
    ).fetchone()[0] == 1
    evidence_collision = connection.execute(
        "SELECT id,evidence_ref,payload_json FROM events WHERE evidence_ref LIKE 'remote:evidence#import-%'"
    ).fetchone()
    assert evidence_collision is not None
    assert json.loads(evidence_collision["payload_json"])["evidence_ref"] == evidence_collision["evidence_ref"]
    remapped_event = connection.execute(
        """SELECT target_key FROM data_import_ledger
        WHERE source_label='windows-local' AND source_user_id=?
          AND table_name='events' AND source_key=?""",
        (DEMO_USER, json.dumps([EVENT_SHARED], separators=(",", ":"))),
    ).fetchone()[0]
    assert remapped_event != EVENT_SHARED
    assert connection.execute("SELECT 1 FROM events WHERE id=?", (remapped_event,)).fetchone()

    assert connection.execute(
        "SELECT quote FROM goals WHERE id=?", (GOAL_SHARED,)
    ).fetchone()[0] == "remote Android goal must remain"
    goal_rows = connection.execute(
        "SELECT id,quote FROM goals WHERE user_id=? ORDER BY quote", (TARGET_USER,)
    ).fetchall()
    assert len(goal_rows) == 2
    remapped_goal = next(row["id"] for row in goal_rows if row["quote"].startswith("source"))
    assert remapped_goal != GOAL_SHARED

    assert connection.execute(
        "SELECT status FROM advice WHERE id=?", (ADVICE_SHARED,)
    ).fetchone()[0] == "active"
    imported_collision = connection.execute(
        """SELECT a.id,a.status,a.goal_id,a.payload_json,aa.state,aa.delivery_count,
                  aa.resolved_reason
        FROM advice a JOIN advice_attention aa ON aa.advice_id=a.id
        WHERE a.user_id=? AND json_extract(a.payload_json,'$.action')=?""",
        (TARGET_USER, "source advice must be retained as withdrawn history"),
    ).fetchone()
    assert imported_collision["id"] != ADVICE_SHARED
    assert imported_collision["status"] == "withdrawn"
    assert imported_collision["goal_id"] == remapped_goal
    imported_payload = json.loads(imported_collision["payload_json"])
    assert imported_payload["status"] == "withdrawn"
    assert imported_payload["goal_id"] == remapped_goal
    assert imported_payload["evidence"][0]["event_id"] == remapped_event
    assert imported_collision["state"] == "resolved"
    assert imported_collision["delivery_count"] == 0
    assert imported_collision["resolved_reason"] == "legacy_no_replay"
    unique_attention = connection.execute(
        """SELECT a.status,aa.state,aa.resolved_reason
        FROM advice a JOIN advice_attention aa ON aa.advice_id=a.id
        WHERE a.id=?""",
        (ADVICE_UNIQUE,),
    ).fetchone()
    assert tuple(unique_attention) == ("active", "resolved", "legacy_no_replay")
    assert connection.execute(
        "SELECT COUNT(*) FROM advice_attention WHERE state<>'resolved'"
    ).fetchone()[0] == 0

    feedback_rows = connection.execute(
        "SELECT feedback_id,kind,note FROM feedback WHERE user_id=? ORDER BY kind",
        (TARGET_USER,),
    ).fetchall()
    assert len(feedback_rows) == 2
    imported_feedback = next(row for row in feedback_rows if row["kind"] == "guidance")
    assert imported_feedback["feedback_id"] != FEEDBACK_SHARED
    assert imported_feedback["note"] == "focus on execution"
    assert connection.execute(
        "SELECT COUNT(*) FROM outcomes WHERE user_id=?", (TARGET_USER,)
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM relevance_ledger WHERE user_id=?", (TARGET_USER,)
    ).fetchone()[0] == 1
    assert tuple(
        connection.execute(
            """SELECT judged,correct,brier_sum FROM trust_accounts
            WHERE user_id=? AND domain='work' AND level=2""",
            (TARGET_USER,),
        ).fetchone()
    ) == (8, 6, 1.0)
    if MODULE._table_exists(connection, "advice_preferences"):
        assert tuple(
            connection.execute(
                """SELECT direction,frequency_mode,revision FROM advice_preferences
                WHERE user_id=? AND scope_key='global'""",
                (TARGET_USER,),
            ).fetchone()
        ) == ("prioritize valuable information", "active", 3)
    assert [tuple(row) for row in connection.execute("SELECT device_id FROM devices")] == [
        ("android-live",)
    ]
    assert connection.execute("SELECT COUNT(*) FROM cloud_slices").fetchone()[0] == 0
    connection.close()


def test_repeated_apply_is_idempotent_and_source_drift_rolls_back(tmp_path: Path):
    source = tmp_path / "windows.db"
    target = tmp_path / "vps.db"
    _seed_source(source)
    _seed_target(target)
    MODULE.run_merge(
        source,
        target,
        source_label="windows-local",
        source_users=[DEMO_USER, TARGET_USER],
        target_user=TARGET_USER,
        apply=True,
        backup_path=tmp_path / "before-first.db",
    )
    first = sqlite3.connect(target)
    logical_before = {
        table: first.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("goals", "events", "advice", "feedback", "outcomes", "relevance_ledger")
    }
    trust_before = first.execute(
        "SELECT judged,correct,brier_sum FROM trust_accounts WHERE user_id=?",
        (TARGET_USER,),
    ).fetchone()
    first.close()

    second_report = MODULE.run_merge(
        source,
        target,
        source_label="windows-local",
        source_users=[DEMO_USER, TARGET_USER],
        target_user=TARGET_USER,
        apply=True,
        backup_path=tmp_path / "before-second.db",
    )
    assert second_report["tables"]["events"]["already_imported"] == 4
    assert second_report["tables"]["trust_accounts"]["already_imported"] == 1
    second = sqlite3.connect(target)
    assert {
        table: second.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in logical_before
    } == logical_before
    assert second.execute(
        "SELECT judged,correct,brier_sum FROM trust_accounts WHERE user_id=?",
        (TARGET_USER,),
    ).fetchone() == trust_before
    second.close()

    source_connection = sqlite3.connect(source)
    source_connection.execute(
        "UPDATE events SET payload_json=? WHERE id=?",
        (json.dumps({"changed": True}), EVENT_SECOND_USER),
    )
    source_connection.commit()
    source_connection.close()
    before_failure = sqlite3.connect(target)
    count_before_failure = before_failure.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_failure.close()
    with pytest.raises(RuntimeError, match="source drift detected"):
        MODULE.run_merge(
            source,
            target,
            source_label="windows-local",
            source_users=[DEMO_USER, TARGET_USER],
            target_user=TARGET_USER,
            apply=True,
            backup_path=tmp_path / "before-drift-attempt.db",
        )
    after_failure = sqlite3.connect(target)
    assert after_failure.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count_before_failure
    assert after_failure.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    after_failure.close()


def test_deduplicated_advice_feedback_resolves_remote_pending_delivery_without_overwrite(
    tmp_path: Path,
):
    source = tmp_path / "windows.db"
    target = tmp_path / "vps.db"
    _create_database(source)
    _create_database(target)
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(target)
    _insert_user(source_connection, DEMO_USER, "2026-01-01T00:00:00+00:00")
    _insert_user(target_connection, TARGET_USER, "2026-01-01T00:00:00+00:00")
    for connection, user_id in (
        (source_connection, DEMO_USER),
        (target_connection, TARGET_USER),
    ):
        _insert_goal(
            connection,
            goal_id=GOAL_SHARED,
            user_id=user_id,
            quote="the same goal",
            created="2026-01-01T00:00:00+00:00",
        )
    _insert_event(
        source_connection,
        event_id=EVENT_SOURCE_EXACT,
        user_id=DEMO_USER,
        event_type="thought.note",
        occurred="2026-02-01T00:00:00+00:00",
        evidence_ref="same:event",
        text="the same event",
    )
    _insert_event(
        target_connection,
        event_id=EVENT_SHARED,
        user_id=TARGET_USER,
        event_type="thought.note",
        occurred="2026-02-01T00:00:00+00:00",
        evidence_ref="same:event",
        text="the same event",
    )
    _insert_advice(
        source_connection,
        advice_id=ADVICE_UNIQUE,
        user_id=DEMO_USER,
        goal_id=GOAL_SHARED,
        event_id=EVENT_SOURCE_EXACT,
        topic_key="same-advice",
        evidence_ref="same:advice",
        action="the same advice",
    )
    _insert_advice(
        target_connection,
        advice_id=ADVICE_SHARED,
        user_id=TARGET_USER,
        goal_id=GOAL_SHARED,
        event_id=EVENT_SHARED,
        topic_key="same-advice",
        evidence_ref="same:advice",
        action="the same advice",
    )
    source_connection.execute(
        """INSERT INTO feedback(
          advice_id,user_id,feedback_id,semantic_key,kind,note,origin,
          signal_weight,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            ADVICE_UNIQUE,
            DEMO_USER,
            "50000000-0000-0000-0000-000000000001",
            "acknowledged|",
            "acknowledged",
            None,
            "user",
            1.0,
            "2026-02-02T00:00:00+00:00",
        ),
    )
    target_connection.execute(
        """INSERT INTO advice_attention(
          advice_id,user_id,topic_key,state,delivery_count,claim_token,
          claim_device_id,claim_expires_at,created_at,updated_at
        ) VALUES(?,?,?,'claimed',1,?,?,?, ?,?)""",
        (
            ADVICE_SHARED,
            TARGET_USER,
            "same-advice",
            "claim-token",
            "android-live",
            "2026-02-02T01:00:00+00:00",
            "2026-02-01T00:00:00+00:00",
            "2026-02-02T00:00:00+00:00",
        ),
    )
    target_connection.execute(
        """INSERT INTO advice_attention_attempts(
          claim_token,advice_id,user_id,device_id,delivery_number,state,
          claimed_at,lease_expires_at
        ) VALUES(?,?,?,?,1,'claimed',?,?)""",
        (
            "claim-token",
            ADVICE_SHARED,
            TARGET_USER,
            "android-live",
            "2026-02-02T00:00:00+00:00",
            "2026-02-02T01:00:00+00:00",
        ),
    )
    source_connection.execute(
        """INSERT INTO trust_accounts(
          user_id,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
          catastrophic_errors,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (DEMO_USER, "work", 2, 3, 2, 0.4, 1.5, 1.2, 0, "2026-02-02"),
    )
    target_connection.execute(
        """INSERT INTO trust_accounts(
          user_id,domain,level,judged,correct,brier_sum,utility_sum,timing_sum,
          catastrophic_errors,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (TARGET_USER, "work", 2, 5, 4, 0.6, 2.5, 2.2, 0, "2026-02-01"),
    )
    source_connection.commit()
    target_connection.commit()
    source_connection.close()
    target_connection.close()

    MODULE.run_merge(
        source,
        target,
        source_label="windows-feedback-snapshot",
        source_users=[DEMO_USER],
        target_user=TARGET_USER,
        apply=True,
        backup_path=tmp_path / "before-feedback-merge.db",
    )

    connection = sqlite3.connect(target)
    assert connection.execute("SELECT COUNT(*) FROM advice").fetchone()[0] == 1
    assert connection.execute(
        "SELECT status,payload_json FROM advice WHERE id=?", (ADVICE_SHARED,)
    ).fetchone()[0] == "active"
    assert connection.execute(
        "SELECT advice_id,kind FROM feedback"
    ).fetchone() == (ADVICE_SHARED, "acknowledged")
    assert connection.execute(
        """SELECT state,claim_token,claim_device_id,resolved_reason,handling_kind
        FROM advice_attention WHERE advice_id=?""",
        (ADVICE_SHARED,),
    ).fetchone() == (
        "resolved",
        None,
        None,
        "imported_feedback",
        "acknowledged",
    )
    assert connection.execute(
        "SELECT state FROM advice_attention_attempts WHERE claim_token='claim-token'"
    ).fetchone()[0] == "cancelled"
    # The same advice existed on both sides, so aggregate trust uses a
    # conservative component-wise maximum instead of double-counting it.
    assert connection.execute(
        "SELECT judged,correct,brier_sum FROM trust_accounts WHERE user_id=?",
        (TARGET_USER,),
    ).fetchone() == (5, 4, 0.6)
    connection.close()


def test_apply_requires_new_backup_and_distinct_databases(tmp_path: Path):
    source = tmp_path / "windows.db"
    target = tmp_path / "vps.db"
    _seed_source(source)
    _seed_target(target)
    with pytest.raises(ValueError, match="backup"):
        MODULE.run_merge(
            source,
            target,
            source_label="windows-local",
            source_users=[DEMO_USER],
            target_user=TARGET_USER,
            apply=True,
        )
    with pytest.raises(ValueError, match="must differ"):
        MODULE.run_merge(
            source,
            source,
            source_label="windows-local",
            source_users=[DEMO_USER],
            target_user=TARGET_USER,
            apply=False,
        )
    existing_backup = tmp_path / "existing.db"
    existing_backup.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        MODULE.run_merge(
            source,
            target,
            source_label="windows-local",
            source_users=[DEMO_USER],
            target_user=TARGET_USER,
            apply=True,
            backup_path=existing_backup,
        )
    assert existing_backup.read_bytes() == b"do not overwrite"
