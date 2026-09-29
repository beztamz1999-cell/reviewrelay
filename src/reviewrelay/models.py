"""Typed state and result models for the Phase 1 foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskState(str, Enum):
    IDLE = "IDLE"
    PRECHECK = "PRECHECK"
    WORKER_RUNNING = "WORKER_RUNNING"
    VERIFY_CANDIDATE = "VERIFY_CANDIDATE"
    BUILD_REVIEW_PACK = "BUILD_REVIEW_PACK"
    SEND_REVIEW = "SEND_REVIEW"
    WAIT_REVIEW = "WAIT_REVIEW"
    PARSE_REVIEW = "PARSE_REVIEW"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    SEND_EVIDENCE = "SEND_EVIDENCE"
    COMPLETE = "COMPLETE"
    PAUSED_OWNER = "PAUSED_OWNER"
    PAUSED_ERROR = "PAUSED_ERROR"
    BLOCKED_DIRTY_BASELINE = "BLOCKED_DIRTY_BASELINE"
    CANDIDATE_INVALID_DIRTY_WORKTREE = "CANDIDATE_INVALID_DIRTY_WORKTREE"
    BLOCKED_SECRET_DETECTED = "BLOCKED_SECRET_DETECTED"
    STALE_REVIEW = "STALE_REVIEW"
    CANDIDATE_MUTATED_DURING_REVIEW = "CANDIDATE_MUTATED_DURING_REVIEW"
    OWNER_ESCALATION_REQUIRED = "OWNER_ESCALATION_REQUIRED"
    ABORTED = "ABORTED"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class TaskRecord:
    project_id: str
    task_id: str
    task_state: TaskState = TaskState.IDLE
    base_sha: str | None = None
    candidate_sha: str | None = None
    review_cycle: int = 0
    fix_cycle_count: int = 0
    evidence_cycle_count: int = 0
    worker_reported_sha_mismatch: bool = False
    reviewer_chat_identity: str | None = None
    worker_session_identity: str | None = None
    last_sent_review_key: str | None = None
    last_review_action: str | None = None
    pack_hash: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class GitInspection:
    repository_root: str
    head_sha: str
    status_porcelain: str

    @property
    def worktree_clean(self) -> bool:
        return self.status_porcelain == ""


@dataclass(frozen=True)
class CandidateVerification:
    repository_root: str
    candidate_sha: str
    worktree_clean: bool
    worker_reported_sha_mismatch: bool


@dataclass(frozen=True)
class PrecheckResult:
    inspection: GitInspection
    base_sha: str
