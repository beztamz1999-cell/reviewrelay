"""Small versioned SQLite task-state store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import SchemaVersionError, StorageError
from .config import validate_identifier
from .models import TaskRecord, TaskState, utc_now_iso
from .storage import PortableDataRoot


SCHEMA_VERSION = 6


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
            self._create_current_schema()
        elif version == 1:
            self._migrate_v1_to_v2()
            self._migrate_v2_to_v3()
            self._migrate_v3_to_v4()
        elif version == 2:
            self._migrate_v2_to_v3()
            self._migrate_v3_to_v4()
        elif version == 3:
            self._migrate_v3_to_v4()
        elif version == 4:
            pass
        elif version == 5:
            pass
        elif version != SCHEMA_VERSION:
            self._connection.close()
            raise SchemaVersionError(f"No migration path from database version {version}")
        if version in {1, 2, 3, 4}:
            self._migrate_v4_to_v5()
        if version in {1, 2, 3, 4, 5}:
            self._migrate_v5_to_v6()

    def _create_current_schema(self) -> None:
        allowed_states = ", ".join(f"'{state.value}'" for state in TaskState)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(f"""
                CREATE TABLE tasks (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_state TEXT NOT NULL CHECK (task_state IN ({allowed_states})),
                    base_sha TEXT,
                    candidate_sha TEXT,
                    review_cycle INTEGER NOT NULL DEFAULT 0 CHECK (review_cycle >= 0),
                    fix_cycle_count INTEGER NOT NULL DEFAULT 0 CHECK (fix_cycle_count >= 0),
                    evidence_cycle_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_cycle_count >= 0),
                    worker_reported_sha_mismatch INTEGER NOT NULL DEFAULT 0 CHECK (worker_reported_sha_mismatch IN (0, 1)),
                    reviewer_chat_identity TEXT,
                    worker_session_identity TEXT,
                    worker_thread_id TEXT,
                    worker_repo_path TEXT,
                    worker_last_turn_id TEXT,
                    worker_last_turn_status TEXT,
                    worker_last_event_at TEXT,
                    last_sent_review_key TEXT,
                    last_review_action TEXT,
                    pack_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, task_id)
                )
            """)
            self._connection.execute("CREATE UNIQUE INDEX worker_thread_identity ON tasks(worker_thread_id) WHERE worker_thread_id IS NOT NULL")
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._github_tables()
            self._project_tables()
            self._controller_tables()

    def _migrate_v1_to_v2(self) -> None:
        """Add the two Phase 2 counters without rewriting Phase 1 task state."""
        with self._connection:
            self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN fix_cycle_count INTEGER NOT NULL DEFAULT 0 CHECK (fix_cycle_count >= 0)"
            )
            self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN evidence_cycle_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_cycle_count >= 0)"
            )
            self._connection.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3(self) -> None:
        """Retain all earlier state and add worker thread/repository binding."""
        with self._connection:
            for column in ("worker_thread_id", "worker_repo_path", "worker_last_turn_id",
                           "worker_last_turn_status", "worker_last_event_at"):
                self._connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            self._connection.execute("CREATE UNIQUE INDEX worker_thread_identity ON tasks(worker_thread_id) WHERE worker_thread_id IS NOT NULL")
            self._connection.execute("PRAGMA user_version = 3")

    def _github_tables(self) -> None:
        self._connection.execute("""CREATE TABLE github_publications (
            project_id TEXT NOT NULL, task_id TEXT NOT NULL, github_remote TEXT NOT NULL,
            github_base_branch TEXT NOT NULL, github_task_branch TEXT NOT NULL,
            github_pr_url TEXT, github_pr_number INTEGER, github_last_local_sha TEXT,
            github_last_remote_sha TEXT, github_publish_status TEXT NOT NULL,
            github_published_at TEXT, metadata_json TEXT NOT NULL,
            PRIMARY KEY(project_id, task_id))""")
        self._connection.execute("""CREATE TABLE github_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
            task_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE github_reviews (
            review_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
            candidate_sha TEXT NOT NULL, review_cycle INTEGER NOT NULL, status TEXT NOT NULL,
            metadata_json TEXT NOT NULL, raw_text TEXT, decision_json TEXT, updated_at TEXT NOT NULL)""")

    def _migrate_v3_to_v4(self) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._github_tables()
            self._connection.execute("PRAGMA user_version = 4")

    def _project_tables(self) -> None:
        self._connection.execute("""CREATE TABLE projects (
            project_id TEXT PRIMARY KEY, local_identity TEXT NOT NULL UNIQUE,
            github_identity TEXT UNIQUE, record_json TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE project_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def _migrate_v4_to_v5(self) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._project_tables()
            self._connection.execute("PRAGMA user_version = 5")

    def _controller_tables(self) -> None:
        self._connection.execute("""CREATE TABLE controller_tasks (
            project_id TEXT NOT NULL, task_id TEXT NOT NULL, record_json TEXT NOT NULL,
            control TEXT, PRIMARY KEY(project_id,task_id))""")
        self._connection.execute("""CREATE TABLE controller_effects (
            effect_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE controller_reviews (
            effect_key TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
            candidate_sha TEXT NOT NULL, review_cycle INTEGER NOT NULL,
            raw_text TEXT NOT NULL, response_json TEXT NOT NULL, decision_json TEXT, created_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE controller_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def _migrate_v5_to_v6(self) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._controller_tables()
            self._connection.execute("PRAGMA user_version = 6")

    def save(self, record: TaskRecord) -> None:
        validate_identifier(record.project_id, "project_id")
        validate_identifier(record.task_id, "task_id")
        if not isinstance(record.task_state, TaskState):
            raise StorageError("task_state must be a TaskState value")
        if isinstance(record.review_cycle, bool) or not isinstance(record.review_cycle, int) or record.review_cycle < 0:
            raise StorageError("review_cycle must be non-negative")
        for name, value in (
            ("fix_cycle_count", record.fix_cycle_count),
            ("evidence_cycle_count", record.evidence_cycle_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StorageError(f"{name} must be a non-negative integer")
        if not isinstance(record.worker_reported_sha_mismatch, bool):
            raise StorageError("worker_reported_sha_mismatch must be a boolean")
        updated_at = record.updated_at or utc_now_iso()
        with self._connection:
            self._connection.execute("""
                INSERT INTO tasks (
                    project_id, task_id, task_state, base_sha, candidate_sha,
                    review_cycle, fix_cycle_count, evidence_cycle_count, worker_reported_sha_mismatch,
                    reviewer_chat_identity, worker_session_identity,
                    worker_thread_id, worker_repo_path, worker_last_turn_id, worker_last_turn_status, worker_last_event_at,
                    last_sent_review_key, last_review_action, pack_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, task_id) DO UPDATE SET
                    task_state=excluded.task_state,
                    base_sha=excluded.base_sha,
                    candidate_sha=excluded.candidate_sha,
                    review_cycle=excluded.review_cycle,
                    fix_cycle_count=excluded.fix_cycle_count,
                    evidence_cycle_count=excluded.evidence_cycle_count,
                    worker_reported_sha_mismatch=excluded.worker_reported_sha_mismatch,
                    reviewer_chat_identity=excluded.reviewer_chat_identity,
                    worker_session_identity=excluded.worker_session_identity,
                    worker_thread_id=excluded.worker_thread_id,
                    worker_repo_path=excluded.worker_repo_path,
                    worker_last_turn_id=excluded.worker_last_turn_id,
                    worker_last_turn_status=excluded.worker_last_turn_status,
                    worker_last_event_at=excluded.worker_last_event_at,
                    last_sent_review_key=excluded.last_sent_review_key,
                    last_review_action=excluded.last_review_action,
                    pack_hash=excluded.pack_hash,
                    updated_at=excluded.updated_at
            """, (
                record.project_id, record.task_id, record.task_state.value, record.base_sha,
                record.candidate_sha, record.review_cycle, record.fix_cycle_count, record.evidence_cycle_count,
                int(record.worker_reported_sha_mismatch), record.reviewer_chat_identity,
                record.worker_session_identity, record.worker_thread_id, record.worker_repo_path,
                record.worker_last_turn_id, record.worker_last_turn_status, record.worker_last_event_at,
                record.last_sent_review_key, record.last_review_action,
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
            fix_cycle_count=row["fix_cycle_count"], evidence_cycle_count=row["evidence_cycle_count"],
            worker_reported_sha_mismatch=bool(row["worker_reported_sha_mismatch"]),
            reviewer_chat_identity=row["reviewer_chat_identity"], worker_session_identity=row["worker_session_identity"],
            worker_thread_id=row["worker_thread_id"], worker_repo_path=row["worker_repo_path"],
            worker_last_turn_id=row["worker_last_turn_id"], worker_last_turn_status=row["worker_last_turn_status"],
            worker_last_event_at=row["worker_last_event_at"],
            last_sent_review_key=row["last_sent_review_key"], last_review_action=row["last_review_action"],
            pack_hash=row["pack_hash"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
