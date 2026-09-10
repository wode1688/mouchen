from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from app.storage import Repository


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "consolidate_private_alpha_user.py"
)
SPEC = importlib.util.spec_from_file_location("consolidate_private_alpha_user", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE users(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE events(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,evidence_ref TEXT,
          payload_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_events_evidence
          ON events(user_id,evidence_ref) WHERE evidence_ref IS NOT NULL;
        CREATE TABLE goals(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,payload_json TEXT NOT NULL
        );
        CREATE TABLE advice(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,evidence_ref TEXT,
          payload_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_advice_evidence
          ON advice(user_id,evidence_ref) WHERE evidence_ref IS NOT NULL;
        CREATE TABLE charters(
          user_id TEXT NOT NULL,domain TEXT NOT NULL,max_level INTEGER NOT NULL,
          redline_authorized INTEGER NOT NULL,updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id,domain)
        );
        CREATE TABLE trust_accounts(
          user_id TEXT NOT NULL,domain TEXT NOT NULL,level INTEGER NOT NULL,
          judged INTEGER NOT NULL,correct INTEGER NOT NULL,brier_sum REAL NOT NULL,
          utility_sum REAL NOT NULL,timing_sum REAL NOT NULL,
          catastrophic_errors INTEGER NOT NULL,updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id,domain,level)
        );
        CREATE TABLE speaking_freezes(
          user_id TEXT NOT NULL,level INTEGER NOT NULL,error_count INTEGER NOT NULL,
          allowed_errors INTEGER NOT NULL,window_started_at TEXT NOT NULL,
          frozen_at TEXT NOT NULL,expires_at TEXT NOT NULL,reason TEXT NOT NULL,
          PRIMARY KEY(user_id,level)
        );
        CREATE TABLE topic_suppressions(
          user_id TEXT NOT NULL,dedupe_key TEXT NOT NULL,source_advice_id TEXT NOT NULL,
          reason TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,PRIMARY KEY(user_id,dedupe_key)
        );
        """
    )


def test_consolidate_preserves_history_and_merges_owner_scoped_state():
    connection = sqlite3.connect(":memory:")
    _schema(connection)
    connection.executemany(
        "INSERT INTO users VALUES(?,?)",
        [("source", "2026-01-01"), ("target", "2026-01-02"), ("test-user", "2026-01-03")],
    )
    connection.execute(
        "INSERT INTO events VALUES(?,?,?,?)",
        ("event-source", "source", "windows:1", json.dumps({"user_id": "source"})),
    )
    connection.execute(
        "INSERT INTO events VALUES(?,?,?,?)",
        ("event-target", "target", "android:1", json.dumps({"user_id": "target"})),
    )
    connection.execute(
        "INSERT INTO goals VALUES(?,?,?)",
        ("goal-source", "source", json.dumps({"user_id": "source"})),
    )
    connection.execute(
        "INSERT INTO advice VALUES(?,?,?,?)",
        ("advice-source", "source", "windows:1", json.dumps({"user_id": "source"})),
    )
    connection.executemany(
        "INSERT INTO charters VALUES(?,?,?,?,?)",
        [
            ("source", "work", 4, 1, "2026-02-01"),
            ("target", "work", 2, 0, "2026-01-01"),
        ],
    )
    connection.executemany(
        "INSERT INTO trust_accounts VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            ("source", "work", 2, 3, 2, 0.4, 1.5, 1.2, 1, "2026-02-01"),
            ("target", "work", 2, 5, 4, 0.6, 2.5, 2.2, 0, "2026-01-01"),
        ],
    )
    connection.execute(
        "INSERT INTO users VALUES(?,?) ON CONFLICT(id) DO NOTHING",
        ("unused", "2026-01-04"),
    )

    report = MODULE.consolidate(
        connection,
        "source",
        "target",
        prune_other_users=True,
    )

    assert report["integrity"] == "ok"
    assert connection.execute("SELECT id FROM users").fetchall() == [("target",)]
    assert connection.execute("SELECT COUNT(*) FROM events WHERE user_id='target'").fetchone()[0] == 2
    event_payload = json.loads(
        connection.execute("SELECT payload_json FROM events WHERE id='event-source'").fetchone()[0]
    )
    assert event_payload["user_id"] == "target"
    advice_payload = json.loads(
        connection.execute("SELECT payload_json FROM advice WHERE id='advice-source'").fetchone()[0]
    )
    assert advice_payload["user_id"] == "target"
    assert connection.execute(
        "SELECT max_level,redline_authorized FROM charters WHERE user_id='target'"
    ).fetchone() == (4, 1)
    assert connection.execute(
        "SELECT judged,correct,catastrophic_errors FROM trust_accounts WHERE user_id='target'"
    ).fetchone() == (8, 6, 1)


def test_consolidate_resolves_real_owner_scoped_unique_collisions(tmp_path: Path):
    database = tmp_path / "real-schema.db"
    repository = Repository(database)
    repository.close()
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS advice_preferences(
          user_id TEXT NOT NULL,scope_key TEXT NOT NULL,goal_id TEXT,
          direction TEXT NOT NULL DEFAULT '',direction_mode TEXT NOT NULL DEFAULT 'inherit',
          frequency_mode TEXT,event_types_json TEXT,revision INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL,PRIMARY KEY(user_id,scope_key)
        )
        """
    )
    connection.executemany(
        "INSERT INTO users(id,created_at) VALUES(?,?)",
        [("source", "2026-01-01"), ("target", "2026-01-02")],
    )
    for event_id, owner in (("event-source", "source"), ("event-target", "target")):
        connection.execute(
            """INSERT INTO events(
              id,user_id,source,type,occurred_at,evidence_ref,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                event_id,
                owner,
                "phone",
                "notification.posted",
                "2026-02-01",
                "same-evidence",
                json.dumps({"user_id": owner}),
                "2026-02-01",
            ),
        )

    def insert_advice(advice_id: str, owner: str, status: str, created_at: str) -> None:
        connection.execute(
            """INSERT INTO advice(
              id,user_id,domain,level,dedupe_key,topic_key,evidence_ref,status,
              delivery,prediction_confidence,prediction_deadline,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                advice_id,
                owner,
                "work",
                2,
                f"dedupe-{advice_id}",
                "same-topic",
                f"evidence-{advice_id}",
                status,
                "immediate",
                0.8,
                "2026-12-01",
                json.dumps({"user_id": owner, "status": status}),
                created_at,
            ),
        )

    insert_advice("advice-source", "source", "adopted", "2026-02-02")
    insert_advice("advice-target", "target", "active", "2026-02-01")
    connection.executemany(
        "INSERT INTO devices VALUES(?,?,?,?,?,?)",
        [
            ("source", "same-device", "windows", "2", "2026-01-01", "2026-03-01"),
            ("target", "same-device", "android", "1", "2026-02-01", "2026-02-01"),
        ],
    )
    connection.executemany(
        "INSERT INTO advice_preferences VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (
                "source",
                "global",
                None,
                "focus source",
                "replace",
                "active",
                "[]",
                3,
                "2026-03-01",
            ),
            (
                "target",
                "global",
                None,
                "old target",
                "replace",
                "balanced",
                "[]",
                1,
                "2026-02-01",
            ),
        ],
    )

    report = MODULE.consolidate(connection, "source", "target", prune_other_users=True)

    assert report["integrity"] == "ok"
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert connection.execute(
        "SELECT status FROM advice WHERE id='advice-source'"
    ).fetchone()[0] == "adopted"
    assert connection.execute(
        "SELECT status FROM advice WHERE id='advice-target'"
    ).fetchone()[0] == "withdrawn"
    assert connection.execute(
        "SELECT platform,app_version,last_seen_at FROM devices"
    ).fetchone() == ("windows", "2", "2026-03-01")
    assert connection.execute(
        "SELECT direction,frequency_mode,revision FROM advice_preferences"
    ).fetchone() == ("focus source", "active", 3)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
