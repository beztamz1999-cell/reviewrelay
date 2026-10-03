"""Durable controller lifecycle, effect intents and operational history."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum

from .config import validate_identifier
from .errors import ReviewRelayError
from .models import TaskState, utc_now_iso
from .state import StateStore
from .storage import TaskStorage


class ControllerError(ReviewRelayError):
    code = "CONTROLLER_ERROR"


class ControllerState(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    WORKER_RUNNING = "WORKER_RUNNING"
    VERIFYING_CANDIDATE = "VERIFYING_CANDIDATE"
    PUBLISHING = "PUBLISHING"
    WAITING_REVIEW = "WAITING_REVIEW"
    PROCESSING_REVIEW = "PROCESSING_REVIEW"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    SENDING_EVIDENCE = "SENDING_EVIDENCE"
    PAUSED_USER = "PAUSED_USER"
    PAUSED_OWNER_STEER = "PAUSED_OWNER_STEER"
    PAUSED_OWNER = "PAUSED_OWNER"
    PAUSED_ERROR = "PAUSED_ERROR"
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class ControllerTask:
    project_id: str
    task_id: str
    title: str
    spec: str
    config_yaml: str
    initial_sha: str
    spec_digest: str
    state: ControllerState = ControllerState.DRAFT
    base_sha: str | None = None
    candidate_sha: str | None = None
    previous_candidate_sha: str | None = None
    worker_thread_id: str | None = None
    worker_turn_id: str | None = None
    candidate_created_at: str | None = None
    review_cycle: int = 0
    fix_cycles: int = 0
    evidence_cycles: int = 0
    require_changes: bool = True
    allow_spec_change: bool = False
    ready_for_owner_review: bool = False
    review_invalidated: bool = False
    counters: dict = field(default_factory=lambda: dict(worker_initial_turns=0, worker_fix_turns=0,
        review_messages=0, evidence_messages=0, local_evidence_batches=0))
    pending: dict = field(default_factory=dict)
    published: dict | None = None
    message_number: int = 0
    last_review_key: str | None = None
    resume_state: str | None = None
    error_code: str | None = None
    reason: str | None = None
    context: str | None = None
    owner_inputs: tuple[dict, ...] = ()
    steer_state: str | None = None
    manual_number: int = 0
    manual_pending: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def load(cls, raw):
        data = json.loads(raw)
        data["state"] = ControllerState(data["state"])
        data["owner_inputs"] = tuple(data["owner_inputs"])
        return cls(**data)


_LEGACY_STATE = {ControllerState.DRAFT: TaskState.IDLE, ControllerState.READY: TaskState.PRECHECK,
    ControllerState.WORKER_RUNNING: TaskState.WORKER_RUNNING,
    ControllerState.VERIFYING_CANDIDATE: TaskState.VERIFY_CANDIDATE,
    ControllerState.PUBLISHING: TaskState.VERIFY_CANDIDATE,
    ControllerState.WAITING_REVIEW: TaskState.WAIT_REVIEW,
    ControllerState.PROCESSING_REVIEW: TaskState.PARSE_REVIEW,
    ControllerState.COLLECTING_EVIDENCE: TaskState.COLLECT_EVIDENCE,
    ControllerState.SENDING_EVIDENCE: TaskState.SEND_EVIDENCE,
    ControllerState.PAUSED_USER: TaskState.PAUSED_OWNER,
    ControllerState.PAUSED_OWNER_STEER: TaskState.PAUSED_OWNER,
    ControllerState.PAUSED_OWNER: TaskState.PAUSED_OWNER,
    ControllerState.PAUSED_ERROR: TaskState.PAUSED_ERROR,
    ControllerState.COMPLETE: TaskState.COMPLETE, ControllerState.STOPPED: TaskState.ABORTED}


class ControllerStore:
    def __init__(self, root):
        self.state, self.storage = StateStore(root), TaskStorage(root)
        self.db = self.state._connection

    def get(self, project_id, task_id):
        validate_identifier(project_id)
        validate_identifier(task_id)
        row = self.db.execute("SELECT record_json FROM controller_tasks WHERE project_id=? AND task_id=?", (project_id, task_id)).fetchone()
        if row is None:
            raise ControllerError("Task does not exist", code="CONTROLLER_TASK_NOT_FOUND")
        return ControllerTask.load(row[0])

    def list(self, project_id):
        return tuple(ControllerTask.load(r[0]) for r in self.db.execute(
            "SELECT record_json FROM controller_tasks WHERE project_id=? ORDER BY task_id", (validate_identifier(project_id),)))

    def save(self, task, event):
        task = replace(task, updated_at=utc_now_iso())
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            if task.state not in {ControllerState.COMPLETE, ControllerState.STOPPED}:
                self.state.assert_project_available(task.project_id, task.task_id)
            self.db.execute("""INSERT INTO controller_tasks(project_id,task_id,record_json) VALUES(?,?,?)
                ON CONFLICT(project_id,task_id) DO UPDATE SET record_json=excluded.record_json""",
                (task.project_id, task.task_id, json.dumps(asdict(task), sort_keys=True)))
            # Never overwrite Phase 4's independently persisted thread/turn fields.
            self.db.execute("""UPDATE tasks SET task_state=?,base_sha=?,candidate_sha=?,review_cycle=?,
                fix_cycle_count=?,evidence_cycle_count=?,updated_at=? WHERE project_id=? AND task_id=?""",
                (_LEGACY_STATE[task.state].value, task.base_sha, task.candidate_sha, task.review_cycle,
                 task.fix_cycles, task.evidence_cycles, task.updated_at, task.project_id, task.task_id))
            self._event(task, event, {"state": task.state.value, "candidate_sha": task.candidate_sha,
                "cycle": task.review_cycle, "error_code": task.error_code, "counters": task.counters})
        record = self.state.get(task.project_id, task.task_id)
        if record:
            self.storage.persist_task_record(record)
        return task

    def _event(self, task, kind, payload):
        self.db.execute("INSERT INTO controller_events(project_id,task_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
            (task.project_id, task.task_id, kind, json.dumps(payload, sort_keys=True), utc_now_iso()))

    def events(self, project_id, task_id):
        # Phase 6 already journals publication effects in this same database.
        return tuple(dict(r) for r in self.db.execute("""SELECT * FROM (
            SELECT 'CONTROLLER' AS source, * FROM controller_events WHERE project_id=? AND task_id=?
            UNION ALL
            SELECT 'GITHUB' AS source, * FROM github_events WHERE project_id=? AND task_id=?
            ) ORDER BY created_at, source, sequence""", (project_id, task_id, project_id, task_id)))

    def effect(self, key):
        row = self.db.execute("SELECT * FROM controller_effects WHERE effect_key=?", (key,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload_json"])} if row else None

    def put_effect(self, task, key, kind, status, payload):
        with self.db:
            self.db.execute("""INSERT INTO controller_effects VALUES(?,?,?,?,?,?,?) ON CONFLICT(effect_key) DO UPDATE SET
                status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (key, task.project_id, task.task_id, kind, status, json.dumps(payload, sort_keys=True), utc_now_iso()))
            self._event(task, kind + "_" + status, {"effect_key": key, "status": status})

    def dispatch(self, task, key, kind, counter):
        """Commit the intent, conservative dispatch counter and event together."""
        task = replace(task, counters={**task.counters, counter: task.counters.get(counter, 0) + 1}, updated_at=utc_now_iso())
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            self.state.assert_project_available(task.project_id, task.task_id)
            self.db.execute("UPDATE controller_effects SET status='IN_FLIGHT',updated_at=? WHERE effect_key=?",
                (task.updated_at, key))
            self.db.execute("UPDATE controller_tasks SET record_json=? WHERE project_id=? AND task_id=?",
                (json.dumps(asdict(task), sort_keys=True), task.project_id, task.task_id))
            self._event(task, kind + "_IN_FLIGHT", {"effect_key": key, "status": "IN_FLIGHT", "counters": task.counters})
        return task

    def review(self, key):
        row = self.db.execute("SELECT * FROM controller_reviews WHERE effect_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def put_review(self, task, key, raw, response, decision=None):
        with self.db:
            self.db.execute("""INSERT INTO controller_reviews VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(effect_key) DO UPDATE SET
                decision_json=COALESCE(excluded.decision_json,controller_reviews.decision_json)""",
                (key, task.project_id, task.task_id, task.candidate_sha, task.review_cycle, raw,
                 json.dumps(response, sort_keys=True), json.dumps(asdict(decision), sort_keys=True) if decision else None, utc_now_iso()))
            self._event(task, "REVIEW_VALIDATED" if decision else "REVIEW_RECEIVED", {"effect_key": key, "candidate_sha": task.candidate_sha})

    def control(self, project_id, task_id, action=None):
        if action is not None:
            if action not in {"PAUSE", "STOP", "CLEAR", "STEER_PAUSE"}:
                raise ValueError("Unknown controller control")
            with self.db:
                self.db.execute("UPDATE controller_tasks SET control=? WHERE project_id=? AND task_id=? "
                    "AND (control IS NULL OR control!='STOP' OR ? IN ('STOP','CLEAR')) "
                    "AND (control IS NULL OR control!='STEER_PAUSE' OR ?!='PAUSE')",
                    (None if action == "CLEAR" else action, project_id, task_id, action, action))
        row = self.db.execute("SELECT control FROM controller_tasks WHERE project_id=? AND task_id=?", (project_id, task_id)).fetchone()
        return row[0] if row else None

    def close(self):
        self.state.close()

    def release_steer(self, task):
        task = replace(task, state=ControllerState(task.steer_state), steer_state=None, updated_at=utc_now_iso())
        with self.db:
            self.db.execute("UPDATE controller_tasks SET record_json=?,control=NULL WHERE project_id=? AND task_id=? AND control='STEER_PAUSE'",
                (json.dumps(asdict(task), sort_keys=True), task.project_id, task.task_id))
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                raise ControllerError("Owner control changed", code="OWNER_STEER_NOT_SAFE")
            self.db.execute("UPDATE tasks SET task_state=?,updated_at=? WHERE project_id=? AND task_id=?",
                (_LEGACY_STATE[task.state].value, task.updated_at, task.project_id, task.task_id))
            self._event(task, "AUTO_RELAY_RESUMED", {})
        record = self.state.get(task.project_id, task.task_id)
        if record:
            self.storage.persist_task_record(record)
        return task

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
