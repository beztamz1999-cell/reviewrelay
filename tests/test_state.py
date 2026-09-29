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


def test_rejects_database_from_newer_schema(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "portable").create()
    database = root.safe_path("db/relay.db")
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(SchemaVersionError):
        StateStore(root)
