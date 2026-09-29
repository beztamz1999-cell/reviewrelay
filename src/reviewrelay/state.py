"""Small versioned SQLite task-state store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import SchemaVersionError, StorageError
from .config import validate_identifier
from .models import TaskRecord, TaskState, utc_now_iso
from .storage import PortableDataRoot


SCHEMA_VERSION = 1


class StateStore:
    def __init__(self, data_root: PortableDataRoot) -> None:
        """Open the one supported database location under an explicit data root."""
        data_root.create()
        self.data_root = data_root
        self.path = data_root.safe_path(Path("db") / "relay.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data_root.assert_managed_path(self.path)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            self._connection.close()
            raise SchemaVersionError(f"Database version {version} is newer than supported version {SCHEMA_VERSION}")
        if version == 0:
            allowed_states = ", ".join(f"'{state.value}'" for state in TaskState)
            with self._connection:
                self._connection.execute(f"""
                    CREATE TABLE tasks (
                        project_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_state TEXT NOT NULL CHECK (task_state IN ({allowed_states})),
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
                """)
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            self._connection.close()
            raise SchemaVersionError(f"No migration path from database version {version}")

    def save(self, record: TaskRecord) -> None:
        validate_identifier(record.project_id, "project_id")
        validate_identifier(record.task_id, "task_id")
        if not isinstance(record.task_state, TaskState):
            raise StorageError("task_state must be a TaskState value")
        if isinstance(record.review_cycle, bool) or not isinstance(record.review_cycle, int) or record.review_cycle < 0:
            raise StorageError("review_cycle must be non-negative")
        if not isinstance(record.worker_reported_sha_mismatch, bool):
            raise StorageError("worker_reported_sha_mismatch must be a boolean")
        updated_at = record.updated_at or utc_now_iso()
        with self._connection:
            self._connection.execute("""
                INSERT INTO tasks (
                    project_id, task_id, task_state, base_sha, candidate_sha,
                    review_cycle, worker_reported_sha_mismatch, reviewer_chat_identity, worker_session_identity,
                    last_sent_review_key, last_review_action, pack_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, task_id) DO UPDATE SET
                    task_state=excluded.task_state,
                    base_sha=excluded.base_sha,
                    candidate_sha=excluded.candidate_sha,
                    review_cycle=excluded.review_cycle,
                    worker_reported_sha_mismatch=excluded.worker_reported_sha_mismatch,
                    reviewer_chat_identity=excluded.reviewer_chat_identity,
                    worker_session_identity=excluded.worker_session_identity,
                    last_sent_review_key=excluded.last_sent_review_key,
                    last_review_action=excluded.last_review_action,
                    pack_hash=excluded.pack_hash,
                    updated_at=excluded.updated_at
            """, (
                record.project_id, record.task_id, record.task_state.value, record.base_sha,
                record.candidate_sha, record.review_cycle, int(record.worker_reported_sha_mismatch), record.reviewer_chat_identity,
                record.worker_session_identity, record.last_sent_review_key, record.last_review_action,
                record.pack_hash, record.created_at, updated_at,
            ))

    def get(self, project_id: str, task_id: str) -> TaskRecord | None:
        validate_identifier(project_id, "project_id")
        validate_identifier(task_id, "task_id")
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?", (project_id, task_id)
        ).fetchone()
        if row is None:
            return None
        try:
            state = TaskState(row["task_state"])
        except ValueError as exc:
            raise StorageError(f"Database contains invalid task state {row['task_state']!r}") from exc
        return TaskRecord(
            project_id=row["project_id"], task_id=row["task_id"], task_state=state,
            base_sha=row["base_sha"], candidate_sha=row["candidate_sha"], review_cycle=row["review_cycle"],
            worker_reported_sha_mismatch=bool(row["worker_reported_sha_mismatch"]),
            reviewer_chat_identity=row["reviewer_chat_identity"], worker_session_identity=row["worker_session_identity"],
            last_sent_review_key=row["last_sent_review_key"], last_review_action=row["last_review_action"],
            pack_hash=row["pack_hash"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
