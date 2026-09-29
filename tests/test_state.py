from __future__ import annotations

import sqlite3

import pytest

from reviewrelay.errors import SchemaVersionError
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.state import SCHEMA_VERSION, StateStore
from reviewrelay.storage import PortableDataRoot


def test_sqlite_persists_typed_task_state_and_fields(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "portable").create()
    record = TaskRecord(
        project_id="gacha-v4", task_id="task-1", task_state=TaskState.WAIT_REVIEW,
        base_sha="a" * 40, candidate_sha="b" * 40, review_cycle=2,
        fix_cycle_count=1, evidence_cycle_count=3,
        worker_reported_sha_mismatch=True,
        reviewer_chat_identity="reviewer-1", worker_session_identity="worker-1",
        last_sent_review_key="gacha-v4/task-1/" + "b" * 40 + "/2",
        last_review_action="FIX_REQUIRED", pack_hash="c" * 64,
    )
    store = StateStore(root)
    store.save(record)
    store.close()
    assert root.safe_path("db/relay.db").is_file()
    with StateStore(root) as reopened:
        assert reopened.get("gacha-v4", "task-1") == record
        assert reopened.get("missing", "task") is None


def test_schema_version_is_explicit(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "portable").create()
    with StateStore(root):
        pass
    database = root.safe_path("db/relay.db")
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    connection.close()


def test_phase1_database_migrates_and_preserves_task_state(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "portable").create()
    database = root.safe_path("db/relay.db")
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    allowed_states = ", ".join(f"'{state.value}'" for state in TaskState)
    connection.execute("""
        CREATE TABLE tasks (
            project_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_state TEXT NOT NULL CHECK (task_state IN (__ALLOWED_STATES__)),
            base_sha TEXT,
            candidate_sha TEXT,
            review_cycle INTEGER NOT NULL DEFAULT 0 CHECK (review_cycle >= 0),
            worker_reported_sha_mismatch INTEGER NOT NULL DEFAULT 0 CHECK (worker_reported_sha_mismatch IN (0, 1)),
            reviewer_chat_identity TEXT,
            worker_session_identity TEXT,
            last_sent_review_key TEXT,
            last_review_action TEXT,
            pack_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, task_id)
        )
    """.replace("__ALLOWED_STATES__", allowed_states))
    connection.execute("""
        INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "gacha-v4", "legacy-task", "WAIT_REVIEW", "a" * 40, "b" * 40, 4, 1,
        "reviewer-legacy", "worker-legacy", "legacy-key", "FIX_REQUIRED", "c" * 64,
        "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z",
    ))
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with StateStore(root) as migrated:
        record = migrated.get("gacha-v4", "legacy-task")
        assert record is not None
        assert record.task_state is TaskState.WAIT_REVIEW
        assert record.base_sha == "a" * 40
        assert record.candidate_sha == "b" * 40
        assert record.review_cycle == 4
        assert record.worker_reported_sha_mismatch is True
        assert record.reviewer_chat_identity == "reviewer-legacy"
        assert record.worker_session_identity == "worker-legacy"
        assert record.last_sent_review_key == "legacy-key"
        assert record.last_review_action == "FIX_REQUIRED"
        assert record.pack_hash == "c" * 64
        assert record.fix_cycle_count == 0
        assert record.evidence_cycle_count == 0
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        updated = TaskRecord(
            **{**record.__dict__, "fix_cycle_count": 2, "evidence_cycle_count": 1}
        )
        migrated.save(updated)

    with StateStore(root) as reopened:
        assert reopened.get("gacha-v4", "legacy-task").fix_cycle_count == 2
        assert reopened.get("gacha-v4", "legacy-task").evidence_cycle_count == 1


def test_rejects_database_from_newer_schema(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "portable").create()
    database = root.safe_path("db/relay.db")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(SchemaVersionError):
        StateStore(root)
