"""Project-scoped worker selection and durable, same-thread Owner steering."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from .config import ProjectConfig
from .controller_store import ControllerError, ControllerState as S
from .git import GitClient
from .github_publish import PublishedCandidate, task_branch_name
from .models import utc_now_iso
from .projects import ProjectRegistry, ProjectError, github_identity
from .worker.base import TurnStatus
from .worker.errors import WorkerTurnAlreadyActive


_STALE_REVIEW_CODES = {"CANDIDATE_MUTATED_DURING_REVIEW", "REMOTE_CANDIDATE_MUTATED", "STALE_REVIEW"}


class WorkerStatus(str, Enum):
    IDLE = "IDLE"
    WORKING = "WORKING"
    WAITING_REVIEW = "WAITING_REVIEW"
    PAUSE_PENDING = "PAUSE_PENDING"
    PAUSED_OWNER_STEER = "PAUSED_OWNER_STEER"
    ERROR = "ERROR"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class WorkerIdentity:
    project_id: str
    task_id: str
    worker_thread_id: str | None
    repository: str
    task_branch: str


@dataclass(frozen=True)
class WorkerView:
    identity: WorkerIdentity
    status: WorkerStatus


def same_path(left, right):
    return bool(left and right and os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve())))


def worker_view(store, project_id, task_id):
    """Only typed, persisted backend state drives the worker panel."""
    task = store.get(project_id, task_id)
    config = ProjectConfig.from_yaml(task.config_yaml)
    record = store.state.get(project_id, task_id)
    identity = WorkerIdentity(project_id, task_id, record.worker_thread_id,
        str(Path(config.repo.path).resolve()), task_branch_name(task_id))
    control = store.control(project_id, task_id)
    if task.manual_pending:
        effect = store.effect(task.manual_pending["key"])
        status = WorkerStatus.WORKING if effect and effect["status"] in {"IN_FLIGHT", "CONFIRMED"} else WorkerStatus.ERROR
    elif task.state is S.PAUSED_OWNER_STEER:
        status = WorkerStatus.PAUSED_OWNER_STEER
    elif control == "STEER_PAUSE":
        status = WorkerStatus.PAUSE_PENDING
    elif task.state in {S.PAUSED_ERROR, S.STOPPED}:
        status = WorkerStatus.ERROR
    elif task.state is S.COMPLETE:
        status = WorkerStatus.COMPLETE
    elif task.state in {S.WAITING_REVIEW, S.PROCESSING_REVIEW, S.SENDING_EVIDENCE, S.PAUSED_OWNER}:
        status = WorkerStatus.WAITING_REVIEW
    elif task.state in {S.READY, S.DRAFT, S.PAUSED_USER}:
        status = WorkerStatus.IDLE
    else:
        status = WorkerStatus.WORKING
    return WorkerView(identity, status)


class WorkerManagement:
    """Mixin: controller locks/journals remain the sole effect authority."""

    def workers(self):
        return tuple(worker_view(self.store, self.project_id, t.task_id) for t in self.store.list(self.project_id))

    def open_worker(self, task_id):
        view = worker_view(self.store, self.project_id, task_id)
        self._worker_binding(view.identity, require_thread=False)
        return view

    def _worker_binding(self, identity, *, require_thread=True):
        if not isinstance(identity, WorkerIdentity) or identity.project_id != self.project_id:
            raise ControllerError("Worker is outside the selected Project", code="WORKER_SELECTION_MISMATCH")
        task = self.store.get(self.project_id, identity.task_id)
        config = self._config(task)
        record = self.store.state.get(self.project_id, identity.task_id)
        row = self.store.db.execute("SELECT * FROM github_publications WHERE project_id=? AND task_id=?",
            (self.project_id, identity.task_id)).fetchone()
        meta = json.loads(row["metadata_json"]) if row else {}
        with ProjectRegistry(self.root) as registry:
            project = registry.get(self.project_id)
        try:
            remote_bound = github_identity(meta.get("remote_url")).key == github_identity(project.github_repo_url).key
        except ProjectError:
            remote_bound = same_path(meta.get("remote_url"), project.github_git_url)
        if (identity != worker_view(self.store, self.project_id, identity.task_id).identity
                or not same_path(identity.repository, config.repo.path)
                or not row or row["github_task_branch"] != identity.task_branch
                or not same_path(meta.get("repo_path"), identity.repository)
                or meta.get("project_repository_url") != project.github_repo_url
                or str(meta.get("repository")).lower() != f"{project.github_owner}/{project.github_repo_name}".lower()
                or not remote_bound
                or (record.worker_thread_id and (not same_path(record.worker_repo_path, identity.repository)
                    or record.worker_session_identity != record.worker_thread_id))):
            raise ControllerError("Persisted worker repository/branch/thread binding differs", code="WORKER_BINDING_MISMATCH")
        if require_thread and (not identity.worker_thread_id or task.worker_thread_id != identity.worker_thread_id):
            raise ControllerError("Task has no consistent existing worker thread", code="WORKER_THREAD_MISMATCH")
        return task, config

    def _unsafe_for_steer(self, task):
        record = self.store.state.get(self.project_id, task.task_id)
        if record.worker_thread_id and record.worker_last_turn_status != "COMPLETED":
            return True
        if self.store.db.execute("SELECT 1 FROM controller_effects WHERE project_id=? AND task_id=? "
                "AND status NOT IN ('PLANNED','COMPLETED') LIMIT 1", (self.project_id, task.task_id)).fetchone():
            return True
        row = self.store.db.execute("SELECT github_publish_status FROM github_publications WHERE project_id=? AND task_id=?",
            (self.project_id, task.task_id)).fetchone()
        publication = self.store.db.execute("SELECT metadata_json FROM github_publications WHERE project_id=? AND task_id=?",
            (self.project_id, task.task_id)).fetchone()
        return bool(row and (row[0] in {"GITHUB_PUSH_IN_FLIGHT", "GITHUB_PUSH_AMBIGUOUS", "GITHUB_PUSH_CONFIRMED"}
            or json.loads(publication[0]).get("pr_status") in {"IN_FLIGHT", "AMBIGUOUS"}))

    def _pause_for_steer(self, task):
        if task.state is S.PAUSED_OWNER_STEER:
            return task
        return self._save(task, "AUTO_RELAY_PAUSED_OWNER_STEER", state=S.PAUSED_OWNER_STEER, steer_state=task.state.value)

    def request_worker_pause(self, identity):
        task, _ = self._worker_binding(identity, require_thread=False)
        if task.state is S.STOPPED or task.review_invalidated:
            raise ControllerError("Resolve this stopped/invalidated Task first", code="OWNER_STEER_NOT_APPLICABLE")
        try:
            lock = self._task_lock(task.task_id)
        except WorkerTurnAlreadyActive:
            lock = None
        try:
            # The independent control column survives cached task saves during an effect.
            self.store.control(self.project_id, task.task_id, "STEER_PAUSE")
            with self.store.db:
                self.store._event(task, "OWNER_STEER_PAUSE_REQUESTED", asdict(identity))
            task = self.store.get(self.project_id, task.task_id)
            if lock and not self._unsafe_for_steer(task) and self.store.control(self.project_id, task.task_id) != "STOP":
                self._pause_for_steer(task)
            return worker_view(self.store, self.project_id, task.task_id)
        finally:
            if lock:
                lock.close()

    async def send_manual_instruction(self, identity, instruction):
        if not isinstance(instruction, str) or not instruction.strip() or "\x00" in instruction or len(instruction) > 16384:
            raise ControllerError("A bounded Owner instruction is required", code="OWNER_INPUT_INVALID")
        lock = self._task_lock(identity.task_id)
        project_lock = None
        try:
            project_lock = self._project_lock()
            task, config = self._worker_binding(identity)
            if (task.state is not S.PAUSED_OWNER_STEER or task.manual_pending or task.review_invalidated
                    or self.store.control(self.project_id, task.task_id) != "STEER_PAUSE" or self._unsafe_for_steer(task)):
                raise ControllerError("Pause at a proven safe boundary before steering", code="OWNER_STEER_NOT_SAFE")
            self.publisher = self.publisher_factory(config)
            before = await self._manual_git(task, config)
            if task.steer_state != S.VERIFYING_CANDIDATE.value and before.candidate_sha != (task.candidate_sha or task.base_sha):
                raise ControllerError("Candidate changed before manual steer", code="TASK_BASELINE_CHANGED")
            if task.published and task.steer_state not in {S.VERIFYING_CANDIDATE.value, S.PUBLISHING.value}:
                await self.publisher.verify_current(PublishedCandidate(**task.published))
            number = task.manual_number + 1
            key = self._key(task, "WORKER_MANUAL", number)
            payload = {**asdict(identity), "head_before": before.candidate_sha,
                "local_branch": await self.git.text(config.repo.path, "symbolic-ref", "--short", "HEAD"),
                "instruction": instruction, "ready_before": task.ready_for_owner_review}
            task = self._save(task, "OWNER_MANUAL_INSTRUCTION_RECORDED", manual_number=number,
                manual_pending={"key": key, **payload}, ready_for_owner_review=False,
                owner_inputs=task.owner_inputs + ({"kind": "WORKER_MANUAL", **payload, "created_at": utc_now_iso()},))
            self._effect(task, key, "WORKER_MANUAL", "PLANNED", payload)
            self.worker = self.worker_factory(config, task)
            resumed = await self.worker.resume_task()  # never thread/start
            self._worker_binding(identity)
            if resumed != identity.worker_thread_id or self.worker.get_session_identity() != identity.worker_thread_id:
                raise ControllerError("Resumed a different worker", code="WORKER_THREAD_MISMATCH")
            if (await self._manual_git(task, config)).candidate_sha != before.candidate_sha or (
                    await self.git.text(config.repo.path, "symbolic-ref", "--short", "HEAD")) != payload["local_branch"]:
                raise ControllerError("Repository changed before dispatch", code="WORKER_BINDING_MISMATCH")
            # STOP is honored before dispatch. A steer-pause is already active.
            if self.store.control(self.project_id, task.task_id) == "STOP":
                raise ControllerError("Task was stopped", code="OWNER_STEER_NOT_SAFE")
            task = self.store.dispatch(task, key, "WORKER_MANUAL", "worker_manual_turns")
            self._checkpoint("WORKER_MANUAL_IN_FLIGHT")
            prompt = (f"ReviewRelay Owner manual instruction\nPROJECT_ID={identity.project_id}\nTASK_ID={identity.task_id}\n"
                f"WORKER_THREAD_ID={identity.worker_thread_id}\nREPOSITORY={identity.repository}\nTASK_BRANCH={identity.task_branch}\n"
                "Continue this exact Task/thread and canonical specification. Do not change repository or branch. "
                "Do not push GitHub. Commit any candidate locally with a clean worktree. No reports are required.\n"
                + ("Specification changes are explicitly permitted.\n" if task.allow_spec_change else "Do not modify the canonical specification.\n")
                + instruction)
            turn = await self._wait(self.worker.send_instruction(prompt), task, worker=True)
            self._worker_binding(identity)
            record = self.store.state.get(self.project_id, task.task_id)
            if self.worker.get_session_identity() != identity.worker_thread_id or record.worker_last_turn_id != turn:
                raise ControllerError("Manual turn identity differs", code="WORKER_THREAD_MISMATCH")
            payload = {**payload, "turn_id": turn}
            self._effect(task, key, "WORKER_MANUAL", "CONFIRMED", payload)
            result = await self._wait(self.worker.wait_until_done(), task, worker=True)
            if result.status is not TurnStatus.COMPLETED or result.thread_id != identity.worker_thread_id or result.turn_id != turn:
                raise ControllerError("Manual completion differs", code="WORKER_COMPLETION_INVALID")
            return await self._finish_manual(task, identity, config, payload)
        except Exception as exc:
            task = self.store.get(self.project_id, identity.task_id)
            code = getattr(exc, "code", "MANUAL_STEER_FAILED")
            if task.manual_pending or code in _STALE_REVIEW_CODES:
                self._save(task, "MANUAL_STEER_ERROR", ready_for_owner_review=False,
                    error_code=code, review_invalidated=task.review_invalidated or code in _STALE_REVIEW_CODES)
            raise
        finally:
            try:
                await self._steer_cleanup(identity.task_id)
            finally:
                if project_lock:
                    project_lock.close()
                lock.close()

    async def _manual_git(self, task, config):
        actual = await asyncio.to_thread(GitClient().verify_candidate, config.repo.path,
            expected_repository=config.repo.path, strict_commit_mode=True)
        path = self._spec_path(config, task.task_id)
        from hashlib import sha256
        if not path.is_file() or (not task.allow_spec_change and sha256(path.read_text(encoding="utf-8").encode()).hexdigest() != task.spec_digest):
            raise ControllerError("Canonical specification changed", code="TASK_SPEC_MUTATED")
        row = self.store.db.execute("SELECT metadata_json FROM github_publications WHERE project_id=? AND task_id=?",
            (self.project_id, task.task_id)).fetchone()
        if not row or await self.git.text(config.repo.path, "remote", "get-url", "--push", "--all", config.github.remote) != json.loads(row[0])["remote_url"]:
            raise ControllerError("Task publish destination changed", code="WORKER_BINDING_MISMATCH")
        return actual

    async def _finish_manual(self, task, identity, config, payload):
        self._worker_binding(identity)
        if any(payload.get(key) != value for key, value in asdict(identity).items()) or (
                self.worker and self.worker.get_session_identity() != identity.worker_thread_id):
            raise ControllerError("Manual effect belongs to another worker", code="WORKER_THREAD_MISMATCH")
        record = self.store.state.get(self.project_id, task.task_id)
        if record.worker_last_turn_id != payload["turn_id"] or record.worker_last_turn_status != "COMPLETED":
            raise ControllerError("Manual turn is not durably complete", code="WORKER_COMPLETION_INVALID")
        actual = await self._manual_git(task, config)
        branch = await self.git.text(config.repo.path, "symbolic-ref", "--short", "HEAD")
        if branch != payload["local_branch"]:
            raise ControllerError("Worker changed the local branch", code="WORKER_BINDING_MISMATCH")
        await self.git.text(config.repo.path, "merge-base", "--is-ancestor", payload["head_before"], actual.candidate_sha)
        key = task.manual_pending["key"]
        self._effect(task, key, "WORKER_MANUAL", "COMPLETED", payload)
        self._record_worker_events(task, payload["turn_id"])
        task = replace(task, worker_turn_id=payload["turn_id"])
        with self.store.db:
            self.store._event(task, "MANUAL_THREAD_IDENTITY_VERIFIED", {
                "THREAD_BEFORE_MANUAL_STEER": identity.worker_thread_id,
                "THREAD_AFTER_MANUAL_STEER": record.worker_thread_id, "turn_id": payload["turn_id"],
                "candidate_created": actual.candidate_sha != payload["head_before"]})
        if actual.candidate_sha != payload["head_before"]:
            # Use the existing independent candidate gate. It authorizes no model effect.
            task = await self._verify_candidate(task, config, owner_steer=True)
        else:
            if task.published:
                self.publisher = self.publisher or self.publisher_factory(config)
                await self.publisher.verify_current(PublishedCandidate(**task.published))
            task = replace(task, ready_for_owner_review=payload["ready_before"])
        return self._save(task, "MANUAL_STEER_COMPLETED", manual_pending={}, error_code=None)

    async def resume_auto_relay(self, identity):
        lock = self._task_lock(identity.task_id)
        project_lock = None
        try:
            project_lock = self._project_lock()
            task, config = self._worker_binding(identity, require_thread=False)
            if task.state is not S.PAUSED_OWNER_STEER or task.review_invalidated:
                raise ControllerError("Worker is not safely paused for Owner steer", code="OWNER_STEER_NOT_SAFE")
            if self.store.control(self.project_id, task.task_id) != "STEER_PAUSE":
                raise ControllerError("Owner steer control changed", code="OWNER_STEER_NOT_SAFE")
            if task.manual_pending:
                effect = self.store.effect(task.manual_pending["key"])
                if not effect or effect["status"] not in {"CONFIRMED", "COMPLETED"}:
                    raise ControllerError("Unknown manual turn; no duplicate dispatch", code="WORKER_TURN_AMBIGUOUS")
                self.worker = self.worker_factory(config, task)
                if await self.worker.resume_task() != identity.worker_thread_id:
                    raise ControllerError("Recovery resumed another thread", code="WORKER_THREAD_MISMATCH")
                task = await self._finish_manual(task, identity, config, effect["payload"])
            if self._unsafe_for_steer(task):
                raise ControllerError("An external outcome is unresolved", code="OWNER_STEER_NOT_SAFE")
            actual = await self._manual_git(task, config)
            expected = task.candidate_sha or task.base_sha
            if task.steer_state != S.VERIFYING_CANDIDATE.value and actual.candidate_sha != expected:
                raise ControllerError("Candidate changed while paused", code="TASK_BASELINE_CHANGED")
            if task.published and task.steer_state != S.VERIFYING_CANDIDATE.value:
                self.publisher = self.publisher or self.publisher_factory(config)
                await self.publisher.verify_current(PublishedCandidate(**task.published))
            # State and control clear commit atomically; crash cannot leave an unpaused latch.
            task = self.store.release_steer(task)
            self._checkpoint("AUTO_RELAY_RESUMED")
            return task
        except Exception as exc:
            code = getattr(exc, "code", "OWNER_STEER_NOT_SAFE")
            if code in _STALE_REVIEW_CODES:
                task = self.store.get(self.project_id, identity.task_id)
                self._save(task, "OWNER_STEER_REVIEW_INVALIDATED", ready_for_owner_review=False,
                    review_invalidated=True, error_code=code)
            raise
        finally:
            try:
                await self._steer_cleanup(identity.task_id)
            finally:
                if project_lock:
                    project_lock.close()
                lock.close()

    async def _steer_cleanup(self, task_id):
        errors = await self._cleanup()
        if errors:
            with self.store.db:
                self.store._event(self.store.get(self.project_id, task_id), "ADAPTER_CLEANUP_FAILED",
                    {"codes": [getattr(e, "code", "ADAPTER_CLOSE_FAILED") for e in errors]})
